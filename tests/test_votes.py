"""Ratings: eligibility, latest-wins, the solver export, privacy of the summary, the private rating UI."""
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot import handlers
from bot.db import DB
from bot.posting import post_rating_prompts
from bot.snapshots import take_due_snapshots
from bot.solver_io import build_signups, fingerprint, import_draft
from bot.views import RateListView, RatePartyButton, RatePromptView, RateView, open_rating
from bot.votes import cast_vote, check_vote, current_rating, rateable_parties, summary_counts, votes_for_export
from conftest import S2, add_party_players, at, confirm, draft_for_day, organizer, player, run_async
from test_approve import FakeParties, attach

UTC = timezone.utc
D2, D3 = "2026-11-02", "2026-11-03"
S2B = "2026-11-02T20:00"
OLD = Path(__file__).resolve().parent / "fixtures" / "schema_before_votes.sql"
SOLVER_DIR = Path(__file__).resolve().parent.parent.parent / "raid-solver"
# S2 = Mon 17:00 EST = 22:00Z, 90 minutes: it ends 23:30Z. S2B ends 02:30Z the next day.
ENDED = "2026-11-02T23:31"


def two_parties(ctx, make_run):
    """Run with two confirmed parties on D2: players 1-8 in S2, players 9-16 in S2B."""
    run = make_run(per_day=True)
    a = add_party_players(ctx, run, [S2], first_id=1)
    b = add_party_players(ctx, run, [S2B], first_id=9)
    roles = ["tank"] * 2 + ["healer"] * 2 + ["dps"] * 4
    parties = [{"slot_key": slot, "party_index": 0, "target_idx": 0,
                "members": [{"discord_id": i, "role": r, "helper": False, "placed_down": False, "distance": 0}
                            for i, r in zip(ids, roles)]} for slot, ids in ((S2, a), (S2B, b))]
    ctx.db.replace_draft(run["id"], parties, [], D2)
    confirm(ctx, run)
    pid = {p["slot_key"]: p["id"] for p in ctx.db.parties(run["id"])}
    return run, pid[S2], pid[S2B]


# ---------------------------------------------------------------- eligibility

def test_a_vote_needs_the_same_confirmed_party_of_a_slot_that_has_ended(ctx, make_run, clock):
    run, pa, pb = two_parties(ctx, make_run)
    at(clock, ENDED)
    ok = lambda voter, target, party: check_vote(ctx.db, ctx.cfg, clock["now"], voter, target, party)    # noqa: E731
    assert ok(1, 2, pa) == (True, "")
    assert ok(1, 1, pa)[1] == "You cannot rate yourself."
    assert "not in your party" in ok(1, 9, pa)[1]                 # a player from the other party
    assert "not in that party" in ok(9, 2, pa)[1]                 # voter was not in the party at all
    assert "not ended" in ok(9, 10, pb)[1]                        # S2B is still running
    assert "not confirmed" in ok(1, 2, 9999)[1]


def test_a_draft_party_is_not_rateable(ctx, make_run, clock):
    run, pa, pb = two_parties(ctx, make_run)
    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE parties SET status = 'draft'")
    at(clock, ENDED)
    assert "not confirmed" in check_vote(ctx.db, ctx.cfg, clock["now"], 1, 2, pa)[1]


def test_the_window_opens_at_the_end_and_closes_after_vote_window_hours(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    end = datetime(2026, 11, 2, 23, 30, tzinfo=UTC)
    check = lambda t: check_vote(ctx.db, ctx.cfg, t, 1, 2, pa)[0]            # noqa: E731
    assert not check(end - timedelta(seconds=1)) and check(end)
    assert check(end + timedelta(hours=72)) and not check(end + timedelta(hours=72, seconds=1))
    ctx.cfg["vote_window_hours"] = 24
    assert not check(end + timedelta(hours=25))


def test_a_slot_that_ended_early_opens_ratings_early(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, "2026-11-02T23:10")
    assert not check_vote(ctx.db, ctx.cfg, clock["now"], 1, 2, pa)[0]
    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE slots SET actual_minutes = 60 WHERE slot_key = ?", (S2,))    # ended 23:00Z
    assert check_vote(ctx.db, ctx.cfg, clock["now"], 1, 2, pa)[0]


def test_cast_vote_stores_only_allowed_votes(ctx, make_run, clock):
    run, pa, pb = two_parties(ctx, make_run)
    at(clock, ENDED)
    assert cast_vote(ctx, 1, 2, "like", pa) == (True, "")
    assert cast_vote(ctx, 1, 1, "like", pa)[0] is False and cast_vote(ctx, 1, 9, "like", pa)[0] is False
    assert cast_vote(ctx, 1, 2, "love", pa)[0] is False
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0] == 1


def test_rateable_parties_lists_only_open_windows(ctx, make_run, clock):
    run, pa, pb = two_parties(ctx, make_run)
    at(clock, "2026-11-02T12:00")
    assert rateable_parties(ctx, 1) == []
    at(clock, ENDED)
    assert [p["party_id"] for p in rateable_parties(ctx, 1)] == [pa] and rateable_parties(ctx, 9) == []
    at(clock, "2026-11-03T03:00")
    assert [p["party_id"] for p in rateable_parties(ctx, 9)] == [pb]
    at(clock, "2026-11-10T00:00")
    assert rateable_parties(ctx, 1) == []


# ---------------------------------------------------------------- latest wins, neutral, expiry

def test_the_latest_vote_wins_and_neutral_clears_an_earlier_one(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)
    cast_vote(ctx, 1, 2, "like", pa)
    cast_vote(ctx, 3, 2, "like", pa)
    at(clock, "2026-11-03T00:00")
    cast_vote(ctx, 1, 2, "dislike", pa)
    assert current_rating(ctx.db, 1, 2, clock["now"]) == "dislike" and current_rating(ctx.db, 4, 2, clock["now"]) == "neutral"
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0] == 3        # append-only
    at(clock, "2026-11-03T01:00")
    cast_vote(ctx, 1, 2, "neutral", pa)
    ids = [str(i) for i in range(1, 17)]
    got = votes_for_export(ctx.db, ctx.cfg, ids, clock["now"])
    assert got == [{"from": "3", "to": "2", "vote": "like"}]                         # 1's neutral removed both


def test_votes_older_than_the_expiry_are_dropped(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    slot = ctx.db.party(pa)["slot_id"]
    now = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
    ctx.db.add_vote(run["id"], slot, 1, 2, "like", now - timedelta(days=91))
    ctx.db.add_vote(run["id"], slot, 3, 2, "like", now - timedelta(days=89))
    ctx.db.add_vote(run["id"], slot, 4, 2, "dislike", now - timedelta(days=95))
    ctx.db.add_vote(run["id"], slot, 4, 2, "like", now - timedelta(days=10))          # a newer vote replaces it
    ids = [str(i) for i in range(1, 9)]
    assert votes_for_export(ctx.db, ctx.cfg, ids, now) == [
        {"from": "3", "to": "2", "vote": "like"}, {"from": "4", "to": "2", "vote": "like"}]
    ctx.cfg["vote_expiry_days"] = 5
    assert votes_for_export(ctx.db, ctx.cfg, ids, now) == []


def test_export_votes_only_cover_pairs_in_the_export_and_come_out_sorted(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    slot = ctx.db.party(pa)["slot_id"]
    t = datetime(2026, 11, 1, tzinfo=UTC)
    for voter, target, vote in ((10, 2, "like"), (2, 1, "dislike"), (1, 2, "like"), (3, 12, "like"), (2, 10, "dislike")):
        ctx.db.add_vote(run["id"], slot, voter, target, vote, t)
    assert votes_for_export(ctx.db, ctx.cfg, ["1", "2", "10"], t + timedelta(days=1)) == [
        {"from": "1", "to": "2", "vote": "like"}, {"from": "2", "to": "1", "vote": "dislike"},
        {"from": "2", "to": "10", "vote": "dislike"}, {"from": "10", "to": "2", "vote": "like"}]


# ---------------------------------------------------------------- the day export and the snapshot time

def signup_world(ctx, make_run, clock):
    run = make_run(per_day=True)
    ids = add_party_players(ctx, run, [S2, "2026-11-03T17:00"])
    slot = ctx.db.run_slots(run["id"])[0]["id"]
    ctx.db.add_vote(run["id"], slot, 1, 2, "like", datetime(2026, 10, 20, tzinfo=UTC))
    ctx.db.add_vote(run["id"], slot, 3, 4, "dislike", datetime(2026, 10, 20, tzinfo=UTC))
    at(clock, "2026-11-02T05:00")
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    return run, ids, slot


def test_the_day_export_carries_the_votes_in_the_solvers_shape(ctx, make_run, clock):
    run, ids, _ = signup_world(ctx, make_run, clock)
    data, _ = build_signups(ctx.db, run, ctx.cfg, D2, clock["now"])
    assert data["votes"] == [{"from": "1", "to": "2", "vote": "like"}, {"from": "3", "to": "4", "vote": "dislike"}]


def test_a_vote_cast_after_the_cutoff_does_not_change_that_days_export_fingerprint_or_import(ctx, make_run, clock):
    run, ids, slot = signup_world(ctx, make_run, clock)
    data, _ = build_signups(ctx.db, run, ctx.cfg, D2, clock["now"])
    draft = draft_for_day(ctx, run, D2, S2, ids)
    before = fingerprint(data)
    at(clock, "2026-11-02T23:45")
    ctx.db.add_vote(run["id"], slot, 5, 6, "dislike", clock["now"])               # after the snapshot
    ctx.db.add_vote(run["id"], slot, 1, 2, "dislike", clock["now"])               # a changed mind, also after
    again, _ = build_signups(ctx.db, run, ctx.cfg, D2, clock["now"])
    assert again == data and fingerprint(again) == before
    assert import_draft(ctx.db, run, draft, ctx.cfg, D2, clock["now"])["seated"] == 8
    # a day whose snapshot comes later does see them
    at(clock, "2026-11-03T05:00")
    take_due_snapshots(ctx.db, clock["now"], ctx.cfg)
    later, _ = build_signups(ctx.db, run, ctx.cfg, D3, clock["now"])
    assert {"from": "5", "to": "6", "vote": "dislike"} in later["votes"]
    assert {"from": "1", "to": "2", "vote": "dislike"} in later["votes"]


def test_a_forced_export_of_an_open_day_reads_votes_as_of_now(ctx, make_run, clock):
    run, ids, slot = signup_world(ctx, make_run, clock)
    first, _ = build_signups(ctx.db, run, ctx.cfg, D3, clock["now"])
    ctx.db.add_vote(run["id"], slot, 5, 6, "like", clock["now"] + timedelta(minutes=5))
    later, _ = build_signups(ctx.db, run, ctx.cfg, D3, clock["now"] + timedelta(minutes=10))
    assert len(later["votes"]) == len(first["votes"]) + 1


def test_the_whole_run_export_has_votes_too(ctx, make_run, clock):
    run, ids, _ = signup_world(ctx, make_run, clock)
    data, _ = build_signups(ctx.db, run, ctx.cfg, None, clock["now"])
    assert len(data["votes"]) == 2


@pytest.mark.skipif(not (SOLVER_DIR / "solve.py").exists(), reason="raid-solver not next to raid-bot")
def test_the_real_solver_accepts_the_votes_and_counts_them(ctx, make_run, clock, tmp_path):
    run, ids, _ = signup_world(ctx, make_run, clock)
    data, _ = build_signups(ctx.db, run, ctx.cfg, D2, clock["now"])
    assert len(data["votes"]) == 2
    signups = tmp_path / "signups.json"
    signups.write_text(json.dumps(data, indent=1))
    solver_cfg = json.loads((SOLVER_DIR / "config.json").read_text())
    solver_cfg["pass1"]["time_limit_s"] = 30
    solver_cfg["pass2"]["time_limit_s"] = 15
    cfg_path = tmp_path / "solver_config.json"
    cfg_path.write_text(json.dumps(solver_cfg))
    out = tmp_path / "draft.json"
    done = subprocess.run([sys.executable, str(SOLVER_DIR / "solve.py"), str(signups), "-o", str(out),
                           "--config", str(cfg_path)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    draft = json.loads(out.read_text())
    assert draft["stats"]["votes"] == len(data["votes"]) == 2
    assert draft["input_fingerprint"] == fingerprint(data)
    at(clock, "2026-11-02T23:45")                                                   # a late vote must not matter
    ctx.db.add_vote(run["id"], 1, 1, 2, "dislike", clock["now"])
    assert import_draft(ctx.db, run, draft, ctx.cfg, D2, clock["now"])["seated"] == 8


# ---------------------------------------------------------------- /votes summary

def test_summary_shows_totals_only_hides_small_ones_and_names_no_voters(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    slot = ctx.db.party(pa)["slot_id"]
    now = datetime(2026, 11, 3, tzinfo=UTC)
    at(clock, "2026-11-03T00:00")
    for voter in (1, 3, 4):
        ctx.db.add_vote(run["id"], slot, voter, 2, "like", now - timedelta(hours=2))
    ctx.db.add_vote(run["id"], slot, 5, 2, "dislike", now - timedelta(hours=1))
    ctx.db.add_vote(run["id"], slot, 1, 6, "like", now - timedelta(hours=2))            # only 1 vote for player 6
    ctx.db.add_vote(run["id"], slot, 3, 7, "neutral", now - timedelta(hours=2))
    assert summary_counts(ctx.db, ctx.cfg, now) == {"2": (3, 1), "6": (1, 0)}
    it = organizer()
    run_async(handlers.votes_summary(ctx, it))
    assert "Player 2: 3 / 1" in it.text and "Player 6" not in it.text and "1 more with fewer than 3 votes" in it.text
    for voter in ("Player 1:", "Player 3:", "Player 5:"):
        assert voter not in it.text
    ctx.cfg["vote_floor"] = 1
    it = organizer()
    run_async(handlers.votes_summary(ctx, it))
    assert "Player 6: 1 / 0" in it.text and "Player 7" not in it.text                   # neutral counts for nothing


def test_summary_is_for_organizers_and_expires_old_votes(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    slot = ctx.db.party(pa)["slot_id"]
    at(clock, "2026-11-03T00:00")
    for voter in (1, 3, 4):
        ctx.db.add_vote(run["id"], slot, voter, 2, "like", clock["now"] - timedelta(days=100))
    it = organizer()
    run_async(handlers.votes_summary(ctx, it))
    assert "Nobody has enough votes" in it.text
    other = player(5)
    run_async(handlers.votes_summary(ctx, other))
    assert "Only organizers" in other.text


# ---------------------------------------------------------------- the rating UI (fake interactions)

def pressed(ctx, user, party_id):
    it = player(user)
    run_async(open_rating(ctx, it, party_id))
    return it


def test_pressing_the_thread_button_opens_a_private_rating_view(ctx, make_run, clock):
    run, pa, pb = two_parties(ctx, make_run)
    at(clock, ENDED)
    it = pressed(ctx, 1, pa)
    sent = it.response.sent[0]
    assert sent["ephemeral"] is True and isinstance(sent["view"], RateView)
    assert "Rate your party" in sent["content"] and "Only you can see this" in sent["content"]
    assert "You were not in that party." in pressed(ctx, 9, pa).text
    assert "not ended" in pressed(ctx, 9, pb).text
    at(clock, "2026-11-20T00:00")
    assert "window closed" in pressed(ctx, 1, pa).text


def test_the_rating_view_saves_each_click_and_shows_the_current_rating(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)

    async def scenario():
        it = player(1)
        await open_rating(ctx, it, pa)
        view = it.response.sent[0]["view"]
        emoji = {"like": "👍", "dislike": "👎", "neutral": "😐"}
        assert len(view.buttons) == 7 and "1" not in [b.target_id for b in view.buttons]      # everyone but me
        assert all(view.ratings[b.target_id] == "neutral" for b in view.buttons)               # nobody rated yet
        button = next(b for b in view.buttons if b.target_id == "2")
        for vote in ("like", "dislike", "neutral"):                                            # one tap = the next rating
            click = player(1)
            await button.callback(click)
            assert view.ratings["2"] == vote and str(button.emoji) == emoji[vote]
            assert "Rate your party" in click.response.edits[-1]["content"]
            assert current_rating(ctx.db, "1", "2", ctx.now()) == vote
        assert ctx.db.conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0] == 3
        done = player(1)
        await next(c for c in view.children if getattr(c, "label", "") == "Done").callback(done)
        assert done.response.edits[-1]["view"] is None
        stranger = player(2)
        assert await view.interaction_check(stranger) is False
    run_async(scenario())


def test_the_select_shows_my_current_rating_per_member(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)
    cast_vote(ctx, 1, 3, "dislike", pa)

    async def scenario():
        it = player(1)
        await open_rating(ctx, it, pa)
        view = it.response.sent[0]["view"]
        import discord
        assert view.ratings["3"] == "dislike" and view.ratings["2"] == "neutral"
        b3 = next(b for b in view.buttons if b.target_id == "3")
        assert b3.label == "Player 3 · healer" and str(b3.emoji) == "👎" and b3.style == discord.ButtonStyle.danger
    run_async(scenario())


def test_a_click_after_the_window_closed_saves_nothing(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)

    async def scenario():
        it = player(1)
        await open_rating(ctx, it, pa)
        view = it.response.sent[0]["view"]
        at(clock, "2026-11-20T00:00")                                               # the window closes while it is open
        click = player(1)
        await next(b for b in view.buttons if b.target_id == "2").callback(click)
        assert "Nothing was saved" in click.response.edits[-1]["content"]
    run_async(scenario())
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM votes").fetchone()[0] == 0


def test_the_rate_command_lists_rateable_parties_and_opens_the_same_view(ctx, make_run, clock):
    run, pa, pb = two_parties(ctx, make_run)
    at(clock, "2026-11-02T12:00")
    it = player(1)
    run_async(handlers.rate(ctx, it))
    assert "no finished party" in it.text and "72 hours" in it.text
    at(clock, ENDED)

    async def scenario():
        it = player(1)
        await handlers.rate(ctx, it)
        sent = it.response.sent[0]
        assert sent["ephemeral"] is True and isinstance(sent["view"], RateListView)
        [button] = sent["view"].children
        assert button.label == "Mon 02 Nov 17:00 · party 1"
        click = player(1)
        await button.callback(click)
        assert isinstance(click.response.sent[0]["view"], RateView) and click.response.sent[0]["ephemeral"] is True
    run_async(scenario())


# ---------------------------------------------------------------- the prompt in the slot's thread

def prompt_world(ctx, make_run):
    run, pa, pb = two_parties(ctx, make_run)
    chan = FakeParties()
    attach(ctx, chan)
    run_async(__import__("bot.posting", fromlist=["post_day_parties"]).post_day_parties(ctx, run, D2, chan))
    return run, pa, pb, chan


def test_the_loop_posts_the_rate_buttons_once_when_a_slot_ends(ctx, make_run, clock):
    run, pa, pb, chan = prompt_world(ctx, make_run)
    at(clock, "2026-11-02T23:00")
    assert run_async(post_rating_prompts(ctx)) == 0                                  # still running
    at(clock, ENDED)
    assert run_async(post_rating_prompts(ctx)) == 1                                  # S2 only
    thread = chan.threads[7000]
    msg = thread.sent[-1]
    assert isinstance(msg["view"], RatePromptView) and "Rate your party" in msg["content"]
    assert [c.item.label for c in msg["view"].children] == ["Rate party 1"]
    assert [c.item.custom_id for c in msg["view"].children] == [f"rate:{pa}"]
    assert run_async(post_rating_prompts(ctx)) == 0                                  # once only
    assert len(chan.threads[7001].sent) == 1                                         # S2B had no prompt yet
    at(clock, "2026-11-03T03:00")
    assert run_async(post_rating_prompts(ctx)) == 1


def test_the_prompt_says_nothing_about_votes_and_a_failed_post_is_retried(ctx, make_run, clock):
    run, pa, pb, chan = prompt_world(ctx, make_run)
    at(clock, ENDED)
    chan.threads[7000].fail = 1
    assert run_async(post_rating_prompts(ctx)) == 0
    assert ctx.db.slots_awaiting_rating()[0]["rating_posted_utc"] is None
    ctx.db.add_vote(run["id"], 1, 1, 2, "like", clock["now"])
    assert run_async(post_rating_prompts(ctx)) == 1
    text = chan.threads[7000].sent[-1]["content"]
    assert "like" not in text.lower().replace("rate your party", "") and "<@" not in text and "0" not in text


def test_no_prompt_after_the_window_and_none_without_a_thread(ctx, make_run, clock):
    run, pa, pb, chan = prompt_world(ctx, make_run)
    at(clock, "2026-11-20T00:00")
    assert run_async(post_rating_prompts(ctx)) == 0
    ctx.get_channel = None
    at(clock, ENDED)
    assert run_async(post_rating_prompts(ctx)) == 0


def test_the_dynamic_button_is_built_from_its_custom_id(ctx, make_run):
    import re

    async def scenario():
        match = re.fullmatch(RatePartyButton.__discord_ui_compiled_template__, "rate:123")
        item = await RatePartyButton.from_custom_id(None, None, match)
        assert item.party_id == 123 and item.item.custom_id == "rate:123"
    run_async(scenario())


# ---------------------------------------------------------------- Discord's limits

def test_rating_strings_and_components_respect_discords_limits(ctx, make_run, clock):
    run = make_run(per_day=True)
    long = "N" * 32
    ids = add_party_players(ctx, run, [S2])
    for i in ids:
        ctx.db.upsert_player(i, long[:-len(i)] + i)                  # 32-character display names
    roles = ["tank"] * 2 + ["healer"] * 2 + ["dps"] * 4
    ctx.db.replace_draft(run["id"], [{"slot_key": S2, "party_index": 0, "target_idx": 18, "members": [
        {"discord_id": i, "role": r, "helper": False, "placed_down": False, "distance": 0} for i, r in zip(ids, roles)]}],
        [], D2)
    confirm(ctx, run)
    pid = ctx.db.parties(run["id"])[0]["id"]
    at(clock, ENDED)

    async def scenario():
        it = player(1)
        await open_rating(ctx, it, pid)
        view = it.response.sent[0]["view"]
        assert len(it.response.sent[0]["content"]) <= 2000
        step = player(1)
        await next(b for b in view.buttons if b.target_id == ids[1]).callback(step)
        assert len(step.response.edits[-1]["content"]) <= 2000
        assert {c.row for c in view.children} <= {0, 1, 2, 3, 4} and len(view.children) <= 5 * 5
        for b in view.buttons:
            assert 1 <= len(b.label) <= 80
        many = [{"id": n, "party_index": n} for n in range(1, 40)]
        prompt = RatePromptView(many)
        assert len(prompt.children) == 25                           # never more than Discord's 25 buttons
        assert all(len(c.item.label) <= 80 and len(c.item.custom_id) <= 100 for c in prompt.children)
        parties = [{"party_id": n, "slot_key": "2026-11-02T17:00", "party_index": n} for n in range(30)]
        listing = RateListView(ctx, parties)
        assert len(listing.children) == 20 and all(len(c.label) <= 80 for c in listing.children)
    run_async(scenario())


# ---------------------------------------------------------------- migration from the previous schema

def test_migration_from_the_previous_schema_adds_votes_and_the_thread_columns(tmp_path):
    path = str(tmp_path / "old.sqlite3")
    conn = sqlite3.connect(path)
    conn.executescript(OLD.read_text())                  # schema exactly as the second commit had it
    conn.execute("INSERT INTO runs (guild_id, name, fight_id, start_date, end_date, signup_deadline_utc, status, "
                 "created_by, created_utc) VALUES ('1', 'Old', 1, '2026-11-02', '2026-11-02', "
                 "'2026-11-01T04:59:00+00:00', 'open', '1', '2026-10-25T12:00:00+00:00')")
    conn.execute("INSERT INTO slots (run_id, slot_key, start_utc, minutes) VALUES (1, '2026-11-02T17:00', "
                 "'2026-11-02T22:00:00+00:00', 90)")
    conn.execute("INSERT INTO parties (run_id, slot_id, party_index, status, target_idx) VALUES (1, 1, 0, 'confirmed', 0)")
    conn.commit()
    names = lambda t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")}       # noqa: E731
    assert "thread_id" not in names("slots") and "message_id" not in names("parties")
    assert "answer" in names("votes") and "vote" not in names("votes")           # the old, unused yes/no table
    conn.execute("INSERT INTO votes (slot_id, voter_id, target_id, answer, created_utc) "
                 "VALUES (1, '5', '6', 'yes', '2026-11-03T00:00:00+00:00'), (1, '6', '5', 'no', '2026-11-03T00:00:00+00:00')")
    conn.commit()
    conn.close()

    db = DB(path)
    cols = lambda t: {r["name"] for r in db.conn.execute(f"PRAGMA table_info({t})")}    # noqa: E731
    assert {"thread_id", "rating_posted_utc"} <= cols("slots") and "message_id" in cols("parties")
    assert {"voter_id", "target_id", "vote", "created_utc"} <= cols("votes")
    assert db.party(1)["status"] == "confirmed" and db.slots_awaiting_rating() == []
    assert [(r["voter_id"], r["target_id"], r["vote"], r["run_id"]) for r in db.conn.execute("SELECT * FROM votes ORDER BY id")] == [
        ("5", "6", "like", 1), ("6", "5", "dislike", 1)]                      # old rows were converted, not lost
    assert not db.conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'votes_old'").fetchone()
    db.add_vote(1, 1, 5, 6, "like", datetime(2026, 11, 3, tzinfo=UTC))
    with pytest.raises(sqlite3.IntegrityError):
        db.add_vote(1, 1, 5, 6, "love", datetime(2026, 11, 3, tzinfo=UTC))
    DB(path).close()                                      # starting again changes nothing
