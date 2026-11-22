"""Ratings: eligibility, latest-wins, the solver export, privacy of the summary, the private rating UI."""
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from bot import handlers
from bot.posting import post_rating_prompts
from bot.snapshots import take_due_snapshots
from bot.solver_io import build_signups, fingerprint, import_draft
from bot.views import FeedbackModal, RateListView, RatePartyButton, RatePromptView, RateView, open_rating
from bot.votes import (cast_party_rating, check_rater, feedback_counts, inferred_scores, party_rating_counts,
                       rateable_parties, submit_feedback, summary_counts, votes_for_export)
from conftest import NOW, S2, add_party_players, at, confirm, draft_for_day, organizer, player, run_async
from test_approve import FakeParties, attach

UTC = timezone.utc
D2, D3 = "2026-11-02", "2026-11-03"
S2B = "2026-11-02T20:00"
SOLVER_DIR = Path(__file__).resolve().parent.parent.parent / "raid-solver"
# S2 = Mon 17:00 EST = 22:00Z, 90 minutes: it ends 23:30Z. S2B ends 02:30Z the next day.
ENDED = "2026-11-02T23:31"


def two_parties(ctx, make_run):
    """Run with two confirmed parties on D2: players 1-8 in S2, players 9-16 in S2B."""
    run = make_run(per_day=True)
    a = add_party_players(ctx, run, [S2], first_id=1)
    b = add_party_players(ctx, run, [S2B], first_id=9)
    roles = ["main_tank", "off_tank", "regen", "shield", "melee", "melee", "ranged", "caster"]
    parties = [{"slot_key": slot, "party_index": 0, "target_idx": 0,
                "members": [{"discord_id": i, "role": r, "helper": False, "placed_down": False, "distance": 0}
                            for i, r in zip(ids, roles)]} for slot, ids in ((S2, a), (S2B, b))]
    ctx.db.replace_draft(run["id"], parties, [], D2)
    confirm(ctx, run)
    pid = {p["slot_key"]: p["id"] for p in ctx.db.parties(run["id"])}
    return run, pid[S2], pid[S2B]


# ---------------------------------------------------------------- eligibility

def test_a_rating_needs_the_same_confirmed_party_of_a_slot_that_has_ended(ctx, make_run, clock):
    run, pa, pb = two_parties(ctx, make_run)
    at(clock, ENDED)
    ok = lambda voter, party: check_rater(ctx.db, ctx.cfg, clock["now"], voter, party)[1]    # noqa: E731
    assert ok(1, pa) == ""
    assert "not in that party" in ok(9, pa)                       # voter was not in the party at all
    assert "not ended" in ok(9, pb)                               # S2B is still running
    assert "not confirmed" in ok(1, 9999)


def test_a_draft_party_is_not_rateable(ctx, make_run, clock):
    run, pa, pb = two_parties(ctx, make_run)
    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE parties SET status = 'draft'")
    at(clock, ENDED)
    assert "not confirmed" in check_rater(ctx.db, ctx.cfg, clock["now"], 1, pa)[1]


def test_the_window_opens_at_the_end_and_closes_after_vote_window_hours(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    end = datetime(2026, 11, 2, 23, 30, tzinfo=UTC)
    check = lambda t: check_rater(ctx.db, ctx.cfg, t, 1, pa)[0]            # noqa: E731
    assert not check(end - timedelta(seconds=1)) and check(end)
    assert check(end + timedelta(hours=72)) and not check(end + timedelta(hours=72, seconds=1))
    ctx.cfg["vote_window_hours"] = 24
    assert not check(end + timedelta(hours=25))


def test_a_slot_that_ended_early_opens_ratings_early(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, "2026-11-02T23:10")
    assert not check_rater(ctx.db, ctx.cfg, clock["now"], 1, pa)[0]
    with ctx.db.conn:
        ctx.db.conn.execute("UPDATE slots SET actual_minutes = 60 WHERE slot_key = ?", (S2,))    # ended 23:00Z
    assert check_rater(ctx.db, ctx.cfg, clock["now"], 1, pa)[0]


def test_cast_party_rating_stores_only_allowed_ratings(ctx, make_run, clock):
    run, pa, pb = two_parties(ctx, make_run)
    at(clock, ENDED)
    assert cast_party_rating(ctx, 1, pa, "like") == (True, "")
    assert cast_party_rating(ctx, 9, pa, "like")[0] is False          # not in that party
    assert cast_party_rating(ctx, 1, pa, "neutral")[0] is False       # only like and dislike
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM party_ratings").fetchone()[0] == 1


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

def test_the_latest_rating_wins_and_the_table_is_append_only(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)
    cast_party_rating(ctx, 1, pa, "like")
    cast_party_rating(ctx, 3, pa, "like")
    at(clock, "2026-11-03T00:00")
    cast_party_rating(ctx, 1, pa, "dislike")
    assert ctx.db.party_rating(pa, 1) == "dislike" and ctx.db.party_rating(pa, 3) == "like" and ctx.db.party_rating(pa, 4) is None
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM party_ratings").fetchone()[0] == 3
    assert {r["voter_id"]: r["vote"] for r in ctx.db.latest_party_ratings(clock["now"])} == {"1": "dislike", "3": "like"}


def test_old_per_player_votes_still_export_as_before(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    slot = ctx.db.party(pa)["slot_id"]
    t0 = datetime(2026, 11, 3, 0, 0, tzinfo=UTC)
    ctx.db.add_vote(run["id"], slot, 1, 2, "like", t0)
    ctx.db.add_vote(run["id"], slot, 3, 2, "like", t0)
    ctx.db.add_vote(run["id"], slot, 1, 2, "neutral", t0 + timedelta(hours=1))
    ids = [str(i) for i in range(1, 17)]
    assert votes_for_export(ctx.db, ctx.cfg, ids, t0 + timedelta(hours=2)) == [{"from": "3", "to": "2", "vote": "like"}]


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
    assert "How was your party overall" in sent["content"]
    assert [c.label for c in sent["view"].children] == ["Liked it", "Didn't enjoy it", "Give feedback"]
    assert "You were not in that party." in pressed(ctx, 9, pa).text
    assert "not ended" in pressed(ctx, 9, pb).text
    at(clock, "2026-11-20T00:00")
    assert "window closed" in pressed(ctx, 1, pa).text


def test_the_buttons_save_the_party_rating_and_show_the_current_one(ctx, make_run, clock):
    import discord
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)

    async def scenario():
        it = player(1)
        await open_rating(ctx, it, pa)
        view = it.response.sent[0]["view"]
        like, dislike, _ = view.children
        assert like.style == dislike.style == discord.ButtonStyle.secondary and "Your rating" not in view.render()
        click = player(1)
        await like.callback(click)
        assert ctx.db.party_rating(pa, 1) == "like" and like.style == discord.ButtonStyle.success
        assert "Your rating: 👍" in click.response.edits[-1]["content"]
        await dislike.callback(player(1))
        assert ctx.db.party_rating(pa, 1) == "dislike" and dislike.style == discord.ButtonStyle.danger
        assert like.style == discord.ButtonStyle.secondary
        assert await view.interaction_check(player(2)) is False
        again = player(1)                                                 # reopening shows the saved rating
        await open_rating(ctx, again, pa)
        assert again.response.sent[0]["view"].rating == "dislike"
    run_async(scenario())
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM party_ratings").fetchone()[0] == 2


def test_a_click_after_the_window_closed_saves_nothing(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)

    async def scenario():
        it = player(1)
        await open_rating(ctx, it, pa)
        view = it.response.sent[0]["view"]
        at(clock, "2026-11-20T00:00")                                               # the window closes while it is open
        click = player(1)
        await view.children[0].callback(click)
        assert "Nothing was saved" in click.response.edits[-1]["content"]
        fb = player(1)
        await view.children[2].callback(fb)                                         # no form once it is closed
        assert not fb.response.modals and "window closed" in fb.response.edits[-1]["content"]
    run_async(scenario())
    assert ctx.db.conn.execute("SELECT COUNT(*) FROM party_ratings").fetchone()[0] == 0


# ---------------------------------------------------------------- feedback

def send(ctx, reporter, party, targets, category, note=""):
    return submit_feedback(ctx, reporter, party, targets, category, note)


def test_feedback_names_players_of_the_same_party_and_replaces_the_earlier_one(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)
    assert send(ctx, 1, pa, [2, 3], "toxic", "  shouted  ") == (True, "")
    rows = ctx.db.party_feedback_by(pa, 1)
    assert [(r["target_id"], r["category"], r["note"]) for r in rows] == [("2", "toxic", "shouted"), ("3", "toxic", "shouted")]
    assert send(ctx, 1, pa, [4], "late_no_show") == (True, "")                  # another category is added ...
    assert [(r["target_id"], r["category"]) for r in ctx.db.party_feedback_by(pa, 1)] == [
        ("2", "toxic"), ("3", "toxic"), ("4", "late_no_show")]
    assert send(ctx, 1, pa, [5], "toxic", "again") == (True, "")                 # ... the same category replaces
    assert [(r["target_id"], r["category"], r["note"]) for r in ctx.db.party_feedback_by(pa, 1)] == [
        ("4", "late_no_show", ""), ("5", "toxic", "again")]
    assert send(ctx, 1, pa, [], "other", "great run") == (True, "")              # naming nobody is fine
    assert [r["target_id"] for r in ctx.db.party_feedback_by(pa, 1) if r["category"] == "other"] == [None]
    assert "yourself" in send(ctx, 1, pa, [1], "toxic")[1]
    assert "not in your party" in send(ctx, 1, pa, [9], "toxic")[1]
    assert "not in that party" in send(ctx, 9, pa, [2], "toxic")[1]
    assert "Pick what happened" in send(ctx, 1, pa, [2], "rude")[1]
    at(clock, "2026-11-20T00:00")
    assert "window closed" in send(ctx, 1, pa, [2], "toxic")[1]
    assert len(ctx.db.party_feedback_by(pa, 1)) == 3                              # nothing changed


def test_the_feedback_modal_collects_players_category_and_details(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)

    async def scenario():
        it = player(1)
        await open_rating(ctx, it, pa)
        view = it.response.sent[0]["view"]
        click = player(1)
        await view.children[2].callback(click)
        [modal] = click.response.modals
        assert isinstance(modal, FeedbackModal) and len(modal.children) == 3
        assert "1" not in [o.value for o in modal.players.options] and len(modal.players.options) == 7
        assert modal.players.required is False and modal.note.required is False
        modal.players._values, modal.category._values, modal.note._value = ["2"], ["communication"], "never spoke"
        sub = player(1)
        await modal.on_submit(sub)
        assert "without your name" in sub.text
        again = player(1)                                                        # reopening starts from what was sent
        await view.children[2].callback(again)
        [modal2] = again.response.modals
        assert [o.value for o in modal2.players.options if o.default] == ["2"]
        assert [o.value for o in modal2.category.options if o.default] == ["communication"]
        assert modal2.note.default == "never spoke"
        modal2.players._values, modal2.category._values, modal2.note._value = ["3"], ["late_no_show"], ""
        await modal2.on_submit(player(1))                                         # a second category sits next to it
        third = player(1)
        await view.children[2].callback(third)
        assert [o.value for o in third.response.modals[0].category.options if o.default] == ["late_no_show"]
    run_async(scenario())
    assert [(r["target_id"], r["category"], r["note"]) for r in ctx.db.party_feedback_by(pa, 1)] == [
        ("2", "communication", "never spoke"), ("3", "late_no_show", "")]


def test_a_swapped_out_players_ratings_and_feedback_go_with_them(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)
    cast_party_rating(ctx, 2, pa, "like")
    cast_party_rating(ctx, 3, pa, "like")
    send(ctx, 2, pa, [3], "toxic")
    send(ctx, 4, pa, [2], "toxic")
    send(ctx, 4, pa, [5], "late_no_show")
    ctx.db.upsert_player(20, "Sub", now=NOW)
    ctx.db.swap_member(pa, 2, 20, "regen", 99, clock["now"], reason_code="no_show", reason_text="")
    assert [r["voter_id"] for r in ctx.db.latest_party_ratings(clock["now"])] == ["3"]
    left = ctx.db.conn.execute("SELECT reporter_id, target_id FROM party_feedback").fetchall()
    assert [(r["reporter_id"], r["target_id"]) for r in left] == [("4", "5")]


# ---------------------------------------------------------------- inferred compatibility and weighted tags

ROLE_CYCLE = ["main_tank", "off_tank", "regen", "shield", "melee", "melee", "ranged", "caster"]
RATED_AT = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
AS_OF = RATED_AT + timedelta(days=1)


def history(ctx, make_run, groups, voter=1):
    """Confirmed parties in slot S2, one per group: `voter` plus the group's other players. `groups` is a list of
    (other player ids, vote); the voter's rating for each is stored. Returns the party ids."""
    run = make_run(per_day=True)
    parties = [{"slot_key": S2, "party_index": k, "target_idx": 0,
                "members": [{"discord_id": i, "role": ROLE_CYCLE[n % 8], "helper": False, "placed_down": False,
                             "distance": 0} for n, i in enumerate([voter, *others])]}
               for k, (others, _) in enumerate(groups)]
    for i in {voter, *(o for others, _ in groups for o in others)}:
        ctx.db.upsert_player(i, f"P{i}", now=NOW)
    ctx.db.replace_draft(run["id"], parties, [], D2)
    confirm(ctx, run)
    pids = [p["id"] for p in sorted(ctx.db.parties(run["id"]), key=lambda p: p["party_index"])]
    for pid, (_, vote) in zip(pids, groups):
        ctx.db.add_party_rating(ctx.db.party(pid), voter, vote, RATED_AT)
    return pids


def export(ctx, ids=range(1, 17), as_of=AS_OF):
    return votes_for_export(ctx.db, ctx.cfg, [str(i) for i in ids], as_of)


def test_a_player_who_always_comes_with_good_parties_is_inferred_as_liked(ctx, make_run):
    history(ctx, make_run, [([2, 3], "like")] * 4 + [([4, 5], "dislike")] * 4)
    got = {(e["from"], e["to"]): e for e in export(ctx)}
    # 4 parties with, 4 without: diff = (1 - -1) / 2 = 1, shrunk by n / (n + 2) with n = 4*4/8 = 2 -> 0.5
    assert got[("1", "2")] == {"from": "1", "to": "2", "vote": "like", "weight": 0.5}
    assert got[("1", "3")]["vote"] == "like"
    assert got[("1", "4")] == {"from": "1", "to": "4", "vote": "dislike", "weight": 0.5}
    assert all(k[0] == "1" for k in got)                                       # only the voter's own pairs


def test_inference_ignores_thin_data_and_indiscriminate_voters(ctx, make_run):
    history(ctx, make_run, [([2], "like")] * 2 + [([3], "dislike")] * 4)         # player 2 only came twice
    assert [e for e in export(ctx) if e["to"] == "2"] == []                    # below compat_min_parties (3)
    assert [e["to"] for e in export(ctx)] == ["3"]                              # 4 parties with: enough


def test_someone_who_rates_everything_alike_says_nothing_about_anyone(ctx, make_run):
    history(ctx, make_run, [([2], "like")] * 3 + [([3], "like")] * 3)
    assert export(ctx) == []


def test_a_fixed_group_cannot_be_told_apart(ctx, make_run):
    history(ctx, make_run, [([2, 3], "like")] * 5)                              # nothing without them to compare with
    assert export(ctx) == []


def test_more_data_makes_the_inferred_score_stronger(ctx, make_run):
    few = history(ctx, make_run, [([2], "like")] * 3 + [([3], "dislike")] * 3)
    s_few = inferred_scores(ctx.db, ctx.cfg, AS_OF, AS_OF - timedelta(days=90))[("1", "2")]
    assert 0 < s_few < 0.5
    with ctx.db.conn:                                                           # same voter, many more parties
        ctx.db.conn.execute("DELETE FROM party_ratings")
    many = [([2], "like")] * 12 + [([3], "dislike")] * 12
    ctx.db.conn.execute("DELETE FROM runs")
    history(ctx, make_run, many)
    assert inferred_scores(ctx.db, ctx.cfg, AS_OF, AS_OF - timedelta(days=90))[("1", "2")] > s_few


def test_ratings_inside_the_window_and_before_as_of_only(ctx, make_run):
    history(ctx, make_run, [([2], "like")] * 4 + [([3], "dislike")] * 4)
    assert export(ctx, as_of=RATED_AT - timedelta(minutes=1)) == []              # not rated yet at that time
    assert export(ctx, as_of=RATED_AT + timedelta(days=91)) == []                # expired
    assert export(ctx, ids=[1, 3]) != [] and [e["to"] for e in export(ctx, ids=[1, 3])] == ["3"]   # 2 is not exported


def test_tagged_feedback_is_a_weighted_dislike_that_fades(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)
    send(ctx, 1, pa, [2], "toxic")
    send(ctx, 3, pa, [2], "communication")
    send(ctx, 4, pa, [2], "late_no_show")
    send(ctx, 5, pa, [2], "performance")
    send(ctx, 6, pa, [], "toxic")                                               # names nobody: no effect
    now = clock["now"]
    got = {(e["from"], e["to"]): e for e in votes_for_export(ctx.db, ctx.cfg, [str(i) for i in range(1, 9)], now)}
    assert got[("1", "2")] == {"from": "1", "to": "2", "vote": "dislike"}                           # 1.0 -> no weight key
    assert got[("3", "2")]["weight"] == 0.5 and got[("4", "2")]["weight"] == 0.2
    assert ("5", "2") not in got and ("6", "2") not in got                       # weight 0 and nobody named
    later = votes_for_export(ctx.db, ctx.cfg, [str(i) for i in range(1, 9)], now + timedelta(days=45))
    assert {(e["from"]): e["weight"] for e in later if e["to"] == "2"} == {"1": 0.5, "3": 0.25, "4": 0.1}   # half faded
    assert [e for e in votes_for_export(ctx.db, ctx.cfg, [str(i) for i in range(1, 9)], now + timedelta(days=91))] == []
    ctx.cfg["feedback_weights"] = {"toxic": 2.0, "late_no_show": 0}
    tuned = {e["from"]: e for e in votes_for_export(ctx.db, ctx.cfg, [str(i) for i in range(1, 9)], now) if e["to"] == "2"}
    assert tuned["1"]["weight"] == 2.0 and "4" not in tuned and tuned["3"]["weight"] == 0.5


def test_old_votes_inferred_scores_and_tags_add_up_per_pair(ctx, make_run):
    pids = history(ctx, make_run, [([2], "like")] * 4 + [([3], "dislike")] * 4)
    slot = ctx.db.party(pids[0])["slot_id"]
    ctx.db.add_vote(ctx.db.party(pids[0])["run_id"], slot, 1, 2, "like", RATED_AT)           # +1 old like, +0.5 inferred
    got = {(e["from"], e["to"]): e for e in export(ctx)}
    assert got[("1", "2")] == {"from": "1", "to": "2", "vote": "like"}                        # 1.5 is capped at 1.0
    ctx.db.replace_party_feedback(ctx.db.party(pids[0]), 1, "toxic", [(2, "")], RATED_AT)    # -1 -> net 0.5 like
    assert {(e["from"], e["to"]): e for e in export(ctx, as_of=RATED_AT)}[("1", "2")]["weight"] == 0.5     # no fading yet


def test_the_solver_entries_are_sorted_and_repeatable(ctx, make_run):
    history(ctx, make_run, [([2], "like")] * 4 + [([3], "dislike")] * 4)
    assert export(ctx) == export(ctx)
    ids = [(int(e["from"]), int(e["to"])) for e in export(ctx)]
    assert ids == sorted(ids)


def test_organizer_summaries_never_show_reporters(ctx, make_run, clock):
    run, pa, _ = two_parties(ctx, make_run)
    at(clock, ENDED)
    for voter in (1, 3, 4):
        cast_party_rating(ctx, voter, pa, "like")
    cast_party_rating(ctx, 5, pa, "dislike")
    send(ctx, 1, pa, [2], "toxic", "screamed at the healer")
    send(ctx, 3, pa, [2], "toxic")
    send(ctx, 4, pa, [2], "late_no_show", "ten minutes late")
    send(ctx, 5, pa, [], "other", "voice chat was broken")
    now = clock["now"]
    assert party_rating_counts(ctx.db, ctx.cfg, now)["2"] == (3, 1)                # the player's own rating is not counted
    assert party_rating_counts(ctx.db, ctx.cfg, now)["1"] == (2, 1)
    reports, general = feedback_counts(ctx.db, ctx.cfg, now)
    assert reports == {"2": {"toxic": 2, "late_no_show": 1}} and general == 1
    for who in (1, 2, 5):
        ctx.db.upsert_player(who, f"P{who}", now=NOW)

    it = organizer()
    run_async(handlers.feedback_summary(ctx, it))
    text = it.text
    assert "P2" in text and "toxic or hostile behaviour ×2" in text and "late or no-show ×1" in text and "parties 👍 3 / 👎 1" in text
    assert "**P1**: parties 👍 2 / 👎 1" in text and "1 report(s) named nobody" in text
    ctx.cfg["vote_floor"] = 4                                                    # P1 has 3 ratings: hidden, P2 has 4: shown
    floor = organizer()
    run_async(handlers.feedback_summary(ctx, floor))
    assert "**P1**" not in floor.text and "parties 👍 3 / 👎 1" in floor.text and "fewer than 4 ratings not shown" in floor.text
    ctx.cfg["vote_floor"] = 3
    note = organizer()
    run_async(handlers.feedback_player(ctx, note, type("M", (), {"id": 2})()))
    assert "screamed at the healer" in note.text and "ten minutes late" in note.text
    assert "P1" not in note.text and "P3" not in note.text and "P4" not in note.text      # nobody who reported
    general_notes = organizer()
    run_async(handlers.feedback_player(ctx, general_notes, None))
    assert "voice chat was broken" in general_notes.text and "P5" not in general_notes.text
    stranger = player(9)
    run_async(handlers.feedback_summary(ctx, stranger))
    assert "organizer" in stranger.text.lower() and "screamed" not in stranger.text


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
    assert not [m for m in chan.threads[7001].sent if "Rate your party" in (m["content"] or "")]   # S2B had no prompt yet
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
    roles = ["main_tank", "off_tank", "regen", "shield", "melee", "melee", "ranged", "caster"]
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
        await view.children[0].callback(step)
        assert len(step.response.edits[-1]["content"]) <= 2000
        assert {c.row for c in view.children if c.row is not None} <= {0, 1, 2, 3, 4} and len(view.children) <= 5 * 5
        for b in view.children:
            assert 1 <= len(b.label) <= 80
        fb = player(1)                                              # the feedback form: 32-character names, 8 players
        await view.children[2].callback(fb)
        [modal] = fb.response.modals
        assert len(modal.title) <= 45 and len(modal.children) <= 5
        for o in [*modal.players.options, *modal.category.options]:
            assert 1 <= len(o.label) <= 100 and len(o.value) <= 100
        assert len(modal.players.options) <= 25 and len(modal.note.placeholder) <= 100
        for label in modal.children:
            assert len(label.text) <= 45 and len(label.description or "") <= 100
        many = [{"id": n, "party_index": n} for n in range(1, 40)]
        prompt = RatePromptView(many)
        assert len(prompt.children) == 25                           # never more than Discord's 25 buttons
        assert all(len(c.item.label) <= 80 and len(c.item.custom_id) <= 100 for c in prompt.children)
        parties = [{"party_id": n, "slot_key": "2026-11-02T17:00", "party_index": n} for n in range(30)]
        listing = RateListView(ctx, parties)
        assert len(listing.children) == 20 and all(len(c.label) <= 80 for c in listing.children)
    run_async(scenario())
