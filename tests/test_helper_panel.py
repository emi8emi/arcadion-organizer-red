"""/helper: the panel for helpers, the helper role and players who need support."""
from types import SimpleNamespace

from bot import handlers, helper_panel
from bot.solver_io import build_signups, fingerprint
from conftest import organizer, player, run_async
from test_handlers import SLOT, fill_one_party

CY = SimpleNamespace(id=3, display_name="Cy", bot=False)


def open_panel(ctx, user=None):
    it = user or organizer(user_id=500)
    run_async(helper_panel.start(ctx, it))
    view = it.response.sent[0]["view"]
    return it, view


def pick(view, item, values):
    item._values = values
    it = organizer(user_id=500)
    run_async(item.callback(it))
    return it


def press(view, label):
    it = organizer(user_id=500)
    run_async(next(c for c in view.children if getattr(c, "label", None) == label).callback(it))
    return it


def exported(ctx, run, extra=()):
    return {p["id"]: p for p in build_signups(ctx.db, run, ctx.cfg, None, None, extra)[0]["players"]}


def test_only_organizers_get_the_panel(ctx):
    it = player(4)
    run_async(helper_panel.start(ctx, it))
    assert "Only organizers" in it.text and not it.response.sent[0].get("view")


def test_granting_and_removing_helpers_feeds_the_export(ctx, make_run):
    run = make_run(status="closed")
    fill_one_party(ctx, run, SLOT)
    _, view = open_panel(ctx)
    assert "Granted helpers: nobody" in view.text() and view.remove.disabled
    done = pick(view, view.add_users, [CY, SimpleNamespace(id=9, display_name="Bot", bot=True)])
    assert "<@3>" in done.text and "<@9>" not in done.text.split("Granted helpers:")[1].split("\n")[0]
    assert exported(ctx, run)["3"]["helper"] is True
    assert [o.value for o in view.remove.options] == ["h:3"]
    gone = pick(view, view.remove, ["h:3"])
    assert "no longer a granted helper" in gone.text and exported(ctx, run)["3"]["helper"] is False


def test_the_helper_role_makes_its_holders_helpers_in_the_export(ctx, make_run):
    run = make_run(status="closed")
    fill_one_party(ctx, run, SLOT)
    role = SimpleNamespace(id=777, mention="<@&777>")
    _, view = open_panel(ctx)
    done = pick(view, view.role_select, [role])
    assert "<@&777>" in done.text and "cannot read role members" in done.text        # no intent in the tests
    ctx.role_members = lambda guild, rid: [3, 5] if (str(guild), rid) == (str(run["guild_id"]), 777) else None
    assert "2 member(s)" in view.text()
    holders = handlers.role_helper_ids(ctx, run)
    assert holders == {"3", "5"}
    data = exported(ctx, run, holders)
    assert data["3"]["helper"] and data["5"]["helper"] and not data["1"]["helper"]
    assert fingerprint({"p": list(exported(ctx, run))}) is not None
    assert "cleared" in press(view, "Clear helper role").text
    assert handlers.role_helper_ids(ctx, run) == set() and view.clear_role_button.disabled


def test_the_role_is_per_fight_and_per_server(ctx, make_run):
    run = make_run(status="closed")
    ctx.db.set_helper_role(1, run["guild_id"], 777, 500, ctx.now())
    ctx.role_members = lambda g, r: [3]
    assert ctx.db.helper_role(1, run["guild_id"]) == 777 and ctx.db.helper_role(1, 12345) is None
    assert handlers.role_helper_ids(ctx, run) == {"3"}
    other = dict(run); other["guild_id"] = 12345
    assert handlers.role_helper_ids(ctx, other) == set()


def test_flagging_a_player_who_needs_support(ctx, make_run):
    run = make_run(status="closed")
    fill_one_party(ctx, run, SLOT)
    before = build_signups(ctx.db, run, ctx.cfg)[0]
    _, view = open_panel(ctx)
    press(view, "Needs support…")                                                   # opens the modal
    modal = helper_panel.SupportModal(view)
    modal.who._values, modal.count._values, modal.mode._values = [CY], ["2"], ["soft"]
    it = organizer(user_id=500)
    run_async(modal.on_submit(it))
    assert "2 helper(s)" in it.text and "preferred" in it.text and "<@3> ×2" in it.text
    after = build_signups(ctx.db, run, ctx.cfg)[0]
    assert exported(ctx, run)["3"]["needs_support"] == {"helpers": 2, "required": False}
    assert fingerprint(after) != fingerprint(before)
    modal = helper_panel.SupportModal(view)
    modal.who._values, modal.count._values, modal.mode._values = [CY], ["3"], ["hard"]
    run_async(modal.on_submit(organizer(user_id=500)))
    assert ctx.db.support_needs(1)["3"] == {"helpers": 3, "required": True} and "(required)" in view.text()
    assert [o.value for o in view.remove.options] == ["s:3"]
    assert "no longer needs support" in pick(view, view.remove, ["s:3"]).text
    assert ctx.db.support_needs(1) == {}
    assert "between 1 and 7" in helper_panel.flag(ctx, ctx.db.fight(1), CY, 9, False, 500)


def test_the_panel_belongs_to_the_organizer_who_opened_it(ctx):
    _, view = open_panel(ctx)
    assert run_async(view.interaction_check(organizer(user_id=500))) is True
    other = organizer(user_id=501)
    assert run_async(view.interaction_check(other)) is False and "not yours" in other.text
