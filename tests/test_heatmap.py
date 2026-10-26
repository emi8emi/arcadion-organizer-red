from datetime import date

from bot import heatmap
from bot.heatmap import estimate_parties, match_players, slot_table
from conftest import add_player, fill_one_party

PARTY = {"tank": 2, "healer": 2, "dps": 4}
SLOT = "2026-11-02T17:00"


def roles(t, h, d):
    return [{"tank"}] * t + [{"healer"}] * h + [{"dps"}] * d


def test_a_full_comp_matches_and_a_missing_tank_does_not():
    assert len(match_players(roles(2, 2, 4), PARTY)) == 8
    assert len(match_players(roles(1, 2, 5), PARTY)) == 7        # only 7 seats can be filled


def test_flex_players_can_cover_a_missing_role():
    flex = roles(1, 2, 4) + [{"tank", "dps"}]
    assert len(match_players(flex, PARTY)) == 8


def test_estimate_counts_whole_parties_from_the_role_mix():
    assert estimate_parties(roles(4, 4, 8), PARTY) == 2
    assert estimate_parties(roles(2, 2, 11), PARTY) == 1          # three spare dps are not a party
    assert estimate_parties(roles(0, 9, 9), PARTY) == 0           # no tanks, no party
    assert estimate_parties([{"tank", "dps"}] * 16, PARTY) == 0   # no healers at all


def test_slot_table_splits_ticks_by_role(ctx, make_run):
    run = make_run()
    fill_one_party(ctx, run, SLOT)
    add_player(ctx, run, 20, ["tank", "dps"], 0, [SLOT, "2026-11-02T18:30"])       # flex counts in both columns
    rows = {r["key"]: r for r in slot_table(ctx.db, run, ctx.cfg)}
    r = rows[SLOT]
    assert (r["tank"], r["healer"], r["dps"], r["total"], r["parties"]) == (3, 2, 5, 9, 1)
    assert rows["2026-11-02T18:30"]["parties"] == 0 and rows["2026-11-02T18:30"]["total"] == 1
    assert "2026-11-02T20:00" not in rows                           # slots without ticks are not listed


def test_render_shows_counts_marker_and_legend(ctx, make_run):
    run = make_run()
    fill_one_party(ctx, run, SLOT)
    add_player(ctx, run, 21, ["healer"], 0, ["2026-11-02T18:30"])
    (msg,) = heatmap.render(ctx.db, run, ctx.cfg)
    assert "9 players have ticked slots" in msg and "America/New_York" in msg
    assert "17:00   2  2  4    8  ✓ ~1" in msg
    assert "18:30   0  1  0    1  --" in msg
    assert "Mon 02 Nov" in msg


def test_render_flags_players_without_a_profile(ctx, make_run):
    run = make_run()
    ctx.db.upsert_player(40, "Newbie")
    ctx.db.set_day_availability(run["id"], 40, "2026-11-02", [SLOT])
    assert "1 still need roles or prog" in heatmap.render(ctx.db, run, ctx.cfg)[0]


def test_render_day_filter_and_empty_run(ctx, make_run):
    run = make_run()
    assert "No one has ticked" in heatmap.render(ctx.db, run, ctx.cfg)[0]
    add_player(ctx, run, 1, ["dps"], 0, [SLOT, "2026-11-03T17:00"])
    only = heatmap.render(ctx.db, run, ctx.cfg, day="2026-11-03")[0]
    assert "Tue 03 Nov" in only and "Mon 02 Nov" not in only
    assert "No ticks on that day." in heatmap.render(ctx.db, run, ctx.cfg, day="2026-11-01")[0]


def test_long_heatmaps_are_split_under_the_message_limit(ctx, make_run):
    run = make_run("Long", start=date(2026, 11, 1), end=date(2026, 11, 14))
    keys = [s["slot_key"] for s in ctx.db.run_slots(run["id"])]
    for uid in range(1, 4):
        add_player(ctx, run, uid, ["dps"], 0, keys)
    msgs = heatmap.render(ctx.db, run, ctx.cfg)
    assert len(msgs) > 1
    assert all(len(m) <= heatmap.MESSAGE_LIMIT for m in msgs)
    assert sum(m.count("```") for m in msgs) % 2 == 0               # no code block cut in half
