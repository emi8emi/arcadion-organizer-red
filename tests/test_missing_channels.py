"""A stored channel that was deleted (or that the bot lost access to) must not crash anything."""
import logging
from types import SimpleNamespace

import discord

from bot import handlers, parties, reminders
from bot.posting import post_rating_prompts
from bot.services import find_channel
from conftest import run_async
from test_approve import D2, S2, attach, approve, setup_day, FakeParties
from test_reminders import world  # noqa: F401  (fixture)
from test_party_actions import approved, party_views, Channel


def gone(_id=None):
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Channel")


async def raising(channel_id):
    raise gone()


def test_find_channel_returns_none_for_missing_deleted_or_forbidden(ctx):
    assert run_async(find_channel(ctx, None)) is None                      # not set
    ctx.get_channel = None
    assert run_async(find_channel(ctx, 5)) is None                         # no lookup available
    ctx.get_channel = raising
    assert run_async(find_channel(ctx, 5)) is None                         # deleted

    async def forbidden(_):
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "Missing Access")
    ctx.get_channel = forbidden
    assert run_async(find_channel(ctx, "5")) is None

    async def ok(channel_id):
        return SimpleNamespace(id=channel_id)
    ctx.get_channel = ok
    assert run_async(find_channel(ctx, "5")).id == 5


def test_approve_with_a_deleted_parties_channel_still_approves_and_explains(ctx, make_run):
    run, _ = setup_day(ctx, make_run)
    ctx.cfg["parties_channel_id"] = 4242
    ctx.get_channel = raising
    it = approve(ctx, None, D2)
    assert "Approved 1 parties" in it.text and "deleted, or I lost access" in it.text
    assert ctx.db.parties(run["id"])[0]["status"] == "confirmed"


def test_confirm_with_a_deleted_parties_channel_explains_and_changes_nothing(ctx, make_run):
    run, _ = setup_day(ctx, make_run)
    ctx.cfg["parties_channel_id"] = 4242
    ctx.get_channel = raising
    it = __import__("conftest").organizer()
    run_async(handlers.draft_confirm(ctx, it, None, D2))
    assert "deleted, or I lost access" in it.text or "cannot find it any more" in it.text
    assert ctx.db.parties(run["id"])[0]["status"] != "confirmed"


def test_the_swap_notice_survives_a_deleted_parties_channel(ctx, make_run, clock):
    run, pid, thread, chan = approved(ctx, make_run)
    from conftest import add_player, organizer
    add_player(ctx, run, 31, ["melee"], 0, ["2026-11-02T17:00"])
    ctx.get_channel = raising
    from test_party_actions import editor, press_apply, stage_swap
    view = editor(ctx, pid)
    stage_swap(view, "8", "31", "cant_make_it")
    sub = press_apply(view)
    force = sub.response.sent[0]["view"] if sub.response.sent else None
    if force is not None:                                      # the "did not tick" warning asks first
        go = organizer(user_id=500)
        run_async(force.force.callback(go))
        sub = go
    assert "31" in {m["discord_id"] for m in ctx.db.party_members(pid)}
    assert "No parties channel found" in sub.text


def test_a_dead_reminder_channel_is_reported_once_not_every_minute(ctx, world, clock, caplog):  # noqa: F811
    from conftest import at
    ctx.cfg["reminder_channel_id"] = 555
    reminders._unreachable.clear()
    at(clock, "2026-11-02T05:00")
    with caplog.at_level(logging.WARNING, logger="raidbot"):
        for minute in range(3):
            at(clock, f"2026-11-02T05:0{minute}")
            assert run_async(reminders.send_due_reminders(ctx, raising)) == 0
    assert len([r for r in caplog.records if "reminder channel" in r.getMessage()]) == 1
    assert ctx.db.run_day(world["id"], "2026-11-02")["last_reminded_utc"] is None   # still due once fixed


def test_a_deleted_slot_thread_stops_the_rating_prompt_retries(ctx, make_run, clock, caplog):
    from test_votes import prompt_world, ENDED
    from conftest import at
    run, pa, pb, chan = prompt_world(ctx, make_run)
    at(clock, ENDED)
    ctx.get_channel = raising
    with caplog.at_level(logging.WARNING, logger="raidbot"):
        assert run_async(post_rating_prompts(ctx)) == 0
        assert run_async(post_rating_prompts(ctx)) == 0
    assert len([r for r in caplog.records if "is gone" in r.getMessage()]) == 1


def test_settings_show_warns_about_a_deleted_channel(ctx):
    from conftest import organizer
    ctx.db.set_guild_setting(100, "parties_channel_id", 4242)
    ctx.get_channel = raising
    it = organizer()
    run_async(handlers.settings_show(ctx, it))
    assert "⚠" in it.text and "deleted or that I cannot see" in it.text

    async def ok(channel_id):
        return SimpleNamespace(id=channel_id)
    ctx.get_channel = ok
    it = organizer()
    run_async(handlers.settings_show(ctx, it))
    assert "⚠" not in it.text
