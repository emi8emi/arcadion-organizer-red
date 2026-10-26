from datetime import date
from zoneinfo import ZoneInfo

import pytest

from bot.grid import (GridConfig, day_slots, parse_hm, parse_local_datetime, run_slots, short_label)

G = GridConfig()
NY = ZoneInfo("America/New_York")


def test_default_day_has_eleven_slots_every_90_minutes():
    slots = day_slots(date(2026, 11, 2), G)
    assert [s.key[11:] for s in slots] == ["08:00", "09:30", "11:00", "12:30", "14:00", "15:30",
                                           "17:00", "18:30", "20:00", "21:30", "23:00"]
    assert all(s.minutes == 90 for s in slots)


def test_fall_back_day_keeps_wall_clock_times_but_moves_utc():
    """US clocks go back on 2026-11-01: same local times, one hour later in UTC."""
    before, change, after = (day_slots(date(2026, 10, 31), G), day_slots(date(2026, 11, 1), G),
                             day_slots(date(2026, 11, 2), G))
    assert [s.key[11:] for s in before] == [s.key[11:] for s in change] == [s.key[11:] for s in after]
    assert before[0].start_utc.hour == 12      # 08:00 EDT = 12:00Z
    assert change[0].start_utc.hour == 13      # 08:00 EST = 13:00Z
    assert after[0].start_utc.hour == 13


def test_spring_forward_day_moves_utc_the_other_way():
    before, change = day_slots(date(2026, 3, 7), G), day_slots(date(2026, 3, 8), G)
    assert before[0].start_utc.hour == 13      # 08:00 EST
    assert change[0].start_utc.hour == 12      # 08:00 EDT
    assert [s.key[11:] for s in before] == [s.key[11:] for s in change]


def test_slot_key_always_matches_local_start_time():
    """Across both DST changes, the id is the real local wall-clock time of the slot."""
    slots = run_slots(date(2026, 3, 1), date(2026, 11, 10), G)
    assert len(slots) == 11 * ((date(2026, 11, 10) - date(2026, 3, 1)).days + 1)
    for s in slots:
        assert s.key == s.start_utc.astimezone(NY).strftime("%Y-%m-%dT%H:%M")


def _gaps(slots):
    return {int((b.start_utc - a.start_utc).total_seconds() // 60) for a, b in zip(slots, slots[1:])}


def test_window_crossing_the_fall_back_hour_has_equal_real_length_and_no_overlap():
    g = GridConfig(day_start="00:00", last_start="23:30")
    normal, change = day_slots(date(2026, 10, 31), g), day_slots(date(2026, 11, 1), g)
    assert _gaps(change) == {90} and _gaps(normal) == {90}
    assert all(s.minutes == 90 for s in change)
    assert len({s.key for s in change}) == len(change)
    assert len(change) > len(normal)            # the 25-hour day fits one more slot


def test_window_crossing_the_spring_forward_hour_has_no_overlap_and_unique_ids():
    g = GridConfig(day_start="00:00", last_start="23:30")
    change = day_slots(date(2026, 3, 8), g)
    assert _gaps(change) == {90}
    assert len({s.key for s in change}) == len(change)
    # 90-minute steps skip the missing hour, so the wall-clock jumps from 01:30 to 04:00.
    assert [s.key[11:] for s in change[:3]] == ["00:00", "01:30", "04:00"]


def test_repeated_wall_clock_hour_gets_a_b_suffix():
    g = GridConfig(slot_minutes=60, day_start="00:00", last_start="03:00")
    keys = [s.key for s in day_slots(date(2026, 11, 1), g)]
    assert "2026-11-01T01:00" in keys and "2026-11-01T01:00b" in keys
    assert len(set(keys)) == len(keys)


def test_missing_hour_is_never_a_slot_on_spring_forward():
    g = GridConfig(slot_minutes=60, day_start="00:00", last_start="05:00")
    hours = [s.start_utc.astimezone(NY).hour for s in day_slots(date(2026, 3, 8), g)]
    assert 2 not in hours                       # 02:xx does not exist that day


def test_run_slots_filters_start_times():
    slots = run_slots(date(2026, 11, 2), date(2026, 11, 4), G, earliest="17:00", latest="20:00")
    assert [s.key[11:] for s in slots] == ["17:00", "18:30", "20:00"] * 3


def test_run_slots_spans_days_in_order():
    slots = run_slots(date(2026, 11, 1), date(2026, 11, 3), G)
    assert len(slots) == 33
    assert slots == sorted(slots, key=lambda s: s.start_utc)


def test_parse_local_datetime_follows_the_zone_rules():
    assert parse_local_datetime("2026-10-30 18:00", "America/New_York").hour == 22   # EDT, UTC-4
    assert parse_local_datetime("2026-11-02 18:00", "America/New_York").hour == 23   # EST, UTC-5


@pytest.mark.parametrize("bad", ["", "17", "25:00", "17:60", "ab:cd"])
def test_parse_hm_rejects_bad_times(bad):
    with pytest.raises(ValueError):
        parse_hm(bad)


def test_short_label():
    s = day_slots(date(2026, 11, 2), G)[6]
    assert short_label(s, "America/New_York") == "Mon 02 Nov 17:00"
