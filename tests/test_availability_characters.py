"""The /availability menu's role toggles and character dropdown (real services, fake interactions)."""
from datetime import timedelta

import discord

from bot import handlers
from conftest import NOW, player, run_async


async def open_menu(ctx, uid=5):
    it = player(uid, f"P{uid}")
    await handlers.availability(ctx, it)
    return it.response.sent[0]["view"]


def add_char(ctx, uid, name, lid):
    ctx.db.upsert_player(uid, f"P{uid}", now=NOW)
    with ctx.db.conn:
        ctx.db.conn.execute("INSERT INTO characters (discord_id, lodestone_id, name, world, verified_utc) "
                            "VALUES (?, ?, ?, 'Exodus', 'x')", (str(uid), lid, name))
    return ctx.db.conn.execute("SELECT id FROM characters WHERE lodestone_id = ?", (lid,)).fetchone()["id"]


def test_the_menu_fits_discords_five_rows(ctx, make_run):
    make_run()
    view = run_async(open_menu(ctx))
    rows = {}
    for c in view.children:
        rows.setdefault(c.row, []).append(c)
    assert sorted(rows) == [0, 1, 2, 3, 4] and len(rows[0]) == 5
    assert all(len(rows[i]) == 1 for i in (1, 2, 3, 4))
    view.to_components()                                   # Discord's own layout check


def test_role_buttons_toggle_and_keep_one_on(ctx, make_run):
    make_run()

    async def scenario():
        it = player(5)
        await handlers.availability(ctx, it)
        view = it.response.sent[0]["view"]
        tank, healer, dps = view.role_buttons
        for b in (dps, tank):
            await b.callback(player(5))
        assert ctx.db.get_player(5)["roles"] == "tank,dps"
        assert [b.style for b in view.role_buttons] == [discord.ButtonStyle.success, discord.ButtonStyle.secondary,
                                                        discord.ButtonStyle.success]
        await tank.callback(player(5))
        last = player(5)
        await dps.callback(last)                           # the last role cannot be switched off
        assert ctx.db.get_player(5)["roles"] == "dps" and "at least one role" in last.text
    run_async(scenario())


def test_dropdown_without_characters_is_disabled_and_explains(ctx, make_run):
    make_run()
    view = run_async(open_menu(ctx))
    assert view.character_select.disabled and "/character add" in view.render()


def test_dropdown_saves_the_character_per_day(ctx, make_run):
    run = make_run()
    a, b = add_char(ctx, 5, "Ann Tank", "111"), add_char(ctx, 5, "Ann Alt", "112")

    async def scenario():
        view = await open_menu(ctx)
        d1, d2 = view.days[0], view.days[1]
        assert view.day == d1
        assert [o.label for o in view.character_select.options] == ["Ann Tank @ Exodus", "Ann Alt @ Exodus"]
        assert not any(o.default for o in view.character_select.options) and "Pick which character" in view.render()
        view.character_select._values = [str(a)]
        await view.character_select.callback(player(5))
        assert ctx.db.chosen_character(run["id"], d1, 5)["id"] == a
        assert ctx.db.chosen_character(run["id"], d2, 5) is None           # the other day is untouched
        view.day = d2
        view.character_select._values = [str(b)]
        await view.character_select.callback(player(5))
        assert ctx.db.chosen_character(run["id"], d2, 5)["id"] == b
        assert [o.default for o in view.character_select.options] == [False, True]
        assert "**Ann Alt @ Exodus**" in view.render()
    run_async(scenario())


def test_one_character_is_preselected_and_someone_elses_is_refused(ctx, make_run):
    run = make_run()
    mine = add_char(ctx, 5, "Ann Tank", "111")
    other = add_char(ctx, 6, "Bob Heal", "222")

    async def scenario():
        view = await open_menu(ctx)
        assert [o.default for o in view.character_select.options] == [True]
        view.character_select._values = [str(other)]
        it = player(5)
        await view.character_select.callback(it)
        assert "not one of your verified" in it.text
        assert ctx.db.chosen_character(run["id"], view.day, 5)["id"] == mine
    run_async(scenario())


def test_a_closed_day_locks_the_dropdown(ctx, make_run, clock):
    make_run()
    add_char(ctx, 5, "Ann Tank", "111")
    add_char(ctx, 5, "Ann Alt", "112")
    view = run_async(open_menu(ctx))
    assert not view.character_select.disabled
    clock["now"] = NOW + timedelta(days=10)
    view.sync()
    assert view.character_select.disabled and "closed" in view.character_select.placeholder
