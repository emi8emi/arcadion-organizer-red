from datetime import date

from bot import heatmap
from bot.heatmap import estimate_parties, match_players, slot_table
from conftest import add_player, fill_one_party

PARTY = {"main_tank": 1, "off_tank": 1, "regen": 1, "shield": 1, "melee": 2, "ranged": 1, "caster": 1}
FULL = [{"main_tank"}, {"off_tank"}, {"regen"}, {"shield"}, {"melee"}, {"melee"}, {"ranged"}, {"caster"}]
SLOT = "2026-11-02T17:00"


def test_a_full_comp_matches_and_a_missing_role_does_not():
    assert len(match_players(FULL, PARTY)) == 8
    no_caster = FULL[:-1] + [{"melee"}]                              # a third melee cannot take the caster seat
    assert len(match_players(no_caster, PARTY)) == 7


def test_flex_players_can_cover_a_missing_role():
    flex = FULL[:-1] + [{"melee", "caster"}]
    assert len(match_players(flex, PARTY)) == 8
    two_tanks = [{"main_tank", "off_tank"}] * 2                      # both tanks flex: still only two tank seats
    assert len(match_players(two_tanks + FULL[2:], PARTY)) == 8


def test_estimate_counts_whole_parties_from_the_role_mix():
    assert estimate_parties(FULL * 2, PARTY) == 2
    assert estimate_parties(FULL + [{"melee"}] * 3, PARTY) == 1      # spare melee are not a party
    assert estimate_parties([r for r in FULL if r != {"shield"}] * 2, PARTY) == 0     # no shield healer, no party
    assert estimate_parties([{"main_tank", "melee"}] * 16, PARTY) == 0                # nobody can heal


def test_slot_table_splits_ticks_by_role(ctx, make_run):
    run = make_run()
    fill_one_party(ctx, run, SLOT)
    add_player(ctx, run, 20, ["main_tank", "melee"], 0, [SLOT, "2026-11-02T18:30"])    # flex counts in both columns
    rows = {r["key"]: r for r in slot_table(ctx.db, run, ctx.cfg)}
    r = rows[SLOT]
    assert r["roles"] == {"main_tank": 2, "off_tank": 1, "regen": 1, "shield": 1, "melee": 3, "ranged": 1, "caster": 1}
    assert (r["total"], r["parties"]) == (9, 1)
    assert rows["2026-11-02T18:30"]["parties"] == 0 and rows["2026-11-02T18:30"]["total"] == 1
    assert "2026-11-02T20:00" not in rows                           # slots without ticks are not listed


def test_render_shows_counts_marker_and_legend(ctx, make_run):
    run = make_run()
    fill_one_party(ctx, run, SLOT)
    add_player(ctx, run, 21, ["regen"], 0, ["2026-11-02T18:30"])
    (msg,) = heatmap.render(ctx.db, run, ctx.cfg)
    assert "9 players have ticked slots" in msg and "America/New_York" in msg
    assert "MT OT RG SH ML RN CS" in msg
    assert "17:00   1  1  1  1  2  1  1    8  ✓ ~1" in msg
    assert "18:30   0  0  1  0  0  0  0    1  --" in msg
    assert "Mon 02 Nov" in msg


def test_render_flags_players_without_a_profile(ctx, make_run):
    run = make_run()
    ctx.db.upsert_player(40, "Newbie")
    ctx.db.set_day_availability(run["id"], 40, "2026-11-02", [SLOT])
    assert "1 still need roles or prog" in heatmap.render(ctx.db, run, ctx.cfg)[0]


def test_render_day_filter_and_empty_run(ctx, make_run):
    run = make_run()
    assert "No one has ticked" in heatmap.render(ctx.db, run, ctx.cfg)[0]
    add_player(ctx, run, 1, ["melee"], 0, [SLOT, "2026-11-03T17:00"])
    only = heatmap.render(ctx.db, run, ctx.cfg, day="2026-11-03")[0]
    assert "Tue 03 Nov" in only and "Mon 02 Nov" not in only
    assert "No ticks on that day." in heatmap.render(ctx.db, run, ctx.cfg, day="2026-11-01")[0]


def test_long_heatmaps_are_split_under_the_message_limit(ctx, make_run):
    run = make_run("Long", start=date(2026, 11, 1), end=date(2026, 11, 14))
    keys = [s["slot_key"] for s in ctx.db.run_slots(run["id"])]
    for uid in range(1, 4):
        add_player(ctx, run, uid, ["melee"], 0, keys)
    msgs = heatmap.render(ctx.db, run, ctx.cfg)
    assert len(msgs) > 1
    assert all(len(m) <= heatmap.MESSAGE_LIMIT for m in msgs)
    assert sum(m.count("```") for m in msgs) % 2 == 0               # no code block cut in half
