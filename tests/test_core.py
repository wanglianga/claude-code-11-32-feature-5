"""Unit tests for interval algebra, day accounting and raking."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.analytics import diary as D
from app.analytics.raking import RakingConfig, effective_strata, rake
from app.timeutil import (
    day_window,
    local_instant,
    merge,
    overlap_length,
    parse_hhmm,
    split_by_survey_day,
    slot_interval,
    subtract,
)

TZ = ZoneInfo("Asia/Shanghai")
UTC = timezone.utc


def _entry(start, end, station="RADIO-1", serial="s1", entry_id=1):
    return {
        "order": 0,
        "entry_id": entry_id,
        "start": start,
        "end": end,
        "start_raw": start.isoformat(),
        "end_raw": end.isoformat(),
        "station": station,
        "source_serial": serial,
        "revision": 1,
    }


def _sub(seq, revision, entries, coverage, scope=None):
    return D.Submission(
        submission_seq=seq,
        revision=revision,
        submitted_at=datetime(2026, 9, 4, tzinfo=UTC),
        revision_note=None,
        replacement_scope=scope or {"type": "append"},
        coverage=coverage,
        entries=entries,
    )


# ---------------------------------------------------------------------------
# time helpers
# ---------------------------------------------------------------------------
def test_half_open_interval_intersection():
    lo = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    hi = datetime(2026, 9, 1, 1, 0, tzinfo=UTC)
    # Touching at the boundary contributes zero, never one minute.
    assert overlap_length(hi, hi + timedelta(hours=1), lo, hi) == 0.0
    assert overlap_length(lo - timedelta(hours=1), lo, lo, hi) == 0.0
    assert overlap_length(lo - timedelta(minutes=1), lo + timedelta(minutes=1), lo, hi) == 1.0
    assert overlap_length(lo, hi, lo, hi) == 60.0


def test_split_by_survey_day_for_cross_midnight_entry():
    start = local_instant(date(2026, 9, 1), 23 * 60 + 30, TZ)
    end = local_instant(date(2026, 9, 2), 20, TZ)
    split = split_by_survey_day(start, end, TZ, [date(2026, 9, 1), date(2026, 9, 2)])
    assert split[date(2026, 9, 1)] == 30.0
    assert split[date(2026, 9, 2)] == 20.0
    assert sum(split.values()) == 50.0


def test_program_slot_allows_24_plus_end():
    start, end = slot_interval(date(2026, 9, 1), "23:30", "24:30", TZ)
    assert (end - start).total_seconds() / 60 == 60.0
    assert parse_hhmm("29:15") == 29 * 60 + 15


def test_merge_and_subtract():
    base = [(datetime(2026, 9, 1, 0, tzinfo=UTC), datetime(2026, 9, 1, 3, tzinfo=UTC))]
    cut = [(datetime(2026, 9, 1, 1, tzinfo=UTC), datetime(2026, 9, 1, 2, tzinfo=UTC))]
    pieces = subtract(base, cut)
    assert pieces == [
        (datetime(2026, 9, 1, 0, tzinfo=UTC), datetime(2026, 9, 1, 1, tzinfo=UTC)),
        (datetime(2026, 9, 1, 2, tzinfo=UTC), datetime(2026, 9, 1, 3, tzinfo=UTC)),
    ]
    # Adjacent pieces merge; a removed hole must not be filled back in.
    assert merge(
        [
            (datetime(2026, 9, 1, 0, tzinfo=UTC), datetime(2026, 9, 1, 1, tzinfo=UTC)),
            (datetime(2026, 9, 1, 1, tzinfo=UTC), datetime(2026, 9, 1, 2, tzinfo=UTC)),
        ]
    ) == [(datetime(2026, 9, 1, 0, tzinfo=UTC), datetime(2026, 9, 1, 2, tzinfo=UTC))]
    assert merge(pieces) == pieces
    assert pipe_clip(pieces) == 120.0


def pipe_clip(pieces):
    from app.timeutil import length_minutes

    return length_minutes(pieces)


# ---------------------------------------------------------------------------
# diary accounting
# ---------------------------------------------------------------------------
def test_filled_no_listening_is_distinct_from_missing():
    day = date(2026, 9, 1)
    window = day_window(day, 0, 24 * 60, TZ)
    filled = [(window)]
    # A person who filled the whole day and listened to nothing.
    rep_filled = D.day_accounting([], filled, [], [], day, *window, TZ)
    assert rep_filled["no_listening_minutes"] == 1440.0
    assert rep_filled["uncovered_minutes"] == 0.0
    assert rep_filled["missing_minutes"] == 0.0
    # A person who returned nothing: the minutes are unknown, not silent.
    rep_missing = D.day_accounting([], [], [], [], day, *window, TZ)
    assert rep_missing["uncovered_minutes"] == 1440.0
    assert rep_missing["no_listening_minutes"] == 0.0
    assert rep_missing["listening_minutes"] == 0.0


def test_one_unfilled_hour_is_reported_as_uncovered_only():
    day = date(2026, 9, 1)
    window = day_window(day, 0, 24 * 60, TZ)
    coverage = [
        (window[0], local_instant(day, 13 * 60, TZ)),
        (local_instant(day, 14 * 60, TZ), window[1]),
    ]
    rep = D.day_accounting([], coverage, [], [], day, *window, TZ)
    assert rep["uncovered_minutes"] == 60.0
    assert rep["no_listening_minutes"] == 1380.0
    assert rep["partition_check"] == 0.0


def test_same_station_overlap_counts_once():
    day = date(2026, 9, 2)
    window = day_window(day, 0, 24 * 60, TZ)
    a = _entry(local_instant(day, 14 * 60, TZ), local_instant(day, 15 * 60, TZ), "RADIO-3", "a", 1)
    b = _entry(local_instant(day, 14 * 60, TZ), local_instant(day, 14 * 60 + 30, TZ), "RADIO-3", "b", 2)
    rep = D.day_accounting([a, b], [window], [], [], day, *window, TZ)
    assert rep["listening_minutes"] == 60.0
    assert rep["no_listening_minutes"] == 1380.0


def test_cross_station_overlap_is_unknown_not_guessed():
    day = date(2026, 9, 2)
    window = day_window(day, 0, 24 * 60, TZ)
    a = _entry(local_instant(day, 14 * 60, TZ), local_instant(day, 14 * 60 + 45, TZ), "RADIO-1", "a", 1)
    b = _entry(local_instant(day, 14 * 60 + 15, TZ), local_instant(day, 15 * 60, TZ), "RADIO-3", "b", 2)
    conflicts = D.conflict_ranges([a, b])
    assert len(conflicts) == 1
    rep = D.day_accounting([a, b], [window], conflicts, [], day, *window, TZ)
    # 14:00-14:15 from A and 14:45-15:00 from B are unambiguous (30 total);
    # 14:15-14:45 is unknown because both stations are reported.
    assert rep["listening_minutes"] == 30.0
    assert rep["unknown_minutes"] == 30.0
    assert rep["partition_check"] == 0.0
    # The conflict is never silently assigned to the later upload.
    assert rep["segments"]["unknown"]


def test_cross_midnight_entry_accounted_on_both_days():
    sub = _sub(
        1,
        1,
        [_entry(local_instant(date(2026, 9, 1), 23 * 60 + 30, TZ), local_instant(date(2026, 9, 2), 20, TZ))],
        [(local_instant(date(2026, 9, 1), 0, TZ), local_instant(date(2026, 9, 3), 0, TZ))],
    )
    rebuilt = D.reconstruct([sub], TZ)
    day1 = D.day_accounting(rebuilt["entries"], rebuilt["coverage"], [], [], date(2026, 9, 1), *day_window(date(2026, 9, 1), 0, 1440, TZ), TZ)
    day2 = D.day_accounting(rebuilt["entries"], rebuilt["coverage"], [], [], date(2026, 9, 2), *day_window(date(2026, 9, 2), 0, 1440, TZ), TZ)
    assert day1["listening_minutes"] == 30.0
    assert day2["listening_minutes"] == 20.0


def test_replacement_retires_rows_but_keeps_history():
    day1 = date(2026, 9, 1)
    day2 = date(2026, 9, 2)
    old_entry = _entry(local_instant(day2, 14 * 60, TZ), local_instant(day2, 15 * 60, TZ), "RADIO-3", "old", 1)
    subs = [
        _sub(1, 1, [old_entry], [(local_instant(day1, 0, TZ), local_instant(day2, 0, TZ))]),
        _sub(
            2,
            2,
            [_entry(local_instant(day2, 9 * 60, TZ), local_instant(day2, 10 * 60, TZ), "RADIO-1", "new", 2)],
            [(local_instant(day2, 0, TZ), local_instant(day2 + timedelta(days=1), 0, TZ))],
            scope={
                "type": "replace",
                "start": local_instant(day2, 0, TZ).isoformat(),
                "end": local_instant(day2 + timedelta(days=1), 0, TZ).isoformat(),
            },
        ),
    ]
    rebuilt = D.reconstruct(subs, TZ)
    active_stations = {e["station"] for e in rebuilt["entries"]}
    assert active_stations == {"RADIO-1"}
    retired = [r for r in rebuilt["retired"] if r.get("entry_id") == 1]
    assert retired and retired[0]["retired_by_submission_seq"] == 2
    # Coverage for day 1 survives the day-2-only replacement.
    day1_rep = D.day_accounting(rebuilt["entries"], rebuilt["coverage"], [], [], day1, *day_window(day1, 0, 1440, TZ), TZ)
    assert day1_rep["uncovered_minutes"] == 0.0


def test_partial_day_cross_day_replacement_is_rejected():
    start = local_instant(date(2026, 9, 2), 22 * 60, TZ)
    end = local_instant(date(2026, 9, 3), 6 * 60, TZ)
    subs = [
        _sub(1, 1, [], [(local_instant(date(2026, 9, 2), 0, TZ), local_instant(date(2026, 9, 3), 0, TZ))]),
        _sub(
            2,
            2,
            [],
            [],
            scope={"type": "replace", "start": start.isoformat(), "end": end.isoformat()},
        ),
    ]
    with pytest.raises(Exception) as exc:
        D.reconstruct(subs, TZ)
    assert "diary_partial_day_replacement" in str(getattr(exc.value, "code", exc.value))


def test_revision_must_increase():
    subs = [
        _sub(1, 2, [], []),
        _sub(2, 2, [], []),
    ]
    with pytest.raises(Exception) as exc:
        D.reconstruct(subs, TZ)
    assert "diary_revision_conflict" in str(getattr(exc.value, "code", exc.value))


# ---------------------------------------------------------------------------
# raking
# ---------------------------------------------------------------------------
def _persons(rows):
    return [{"age_band": a, "region": r, "sex": s} for a, r, s in rows]


def test_raking_converges_and_hits_margins():
    persons = _persons(
        [("a", "n", "F")] * 6 + [("b", "n", "M")] * 4 + [("a", "s", "M")] * 3 + [("b", "s", "F")] * 3
    )
    base = [1.0] * len(persons)
    margins = {
        "age_band": {"a": 11.0, "b": 5.0},
        "region": {"n": 9.0, "s": 7.0},
        "sex": {"F": 8.0, "M": 8.0},
    }
    result = rake(persons, base, margins, RakingConfig(lower_factor=0.3, upper_factor=4.0))
    assert result.success, (result.status, result.unreachable_cells)
    assert result.max_rel_residual < 1e-4
    # Every margin is met within tolerance, not just approximately.
    for row in result.cells:
        assert abs(row["relative_residual"]) < 1e-4
    # Total calibrated weight equals the target population total.
    assert abs(sum(result.weights) - 16.0) < 1e-6


def test_raking_is_deterministic():
    persons = _persons([("a", "n", "F")] * 3 + [("b", "s", "M")] * 3 + [("a", "s", "F")] * 2)
    base = [1.0] * len(persons)
    margins = {
        "age_band": {"a": 5.0, "b": 3.0},
        "region": {"n": 3.0, "s": 5.0},
        "sex": {"F": 5.0, "M": 3.0},
    }
    first = rake(persons, base, margins, RakingConfig())
    second = rake(persons, base, margins, RakingConfig())
    assert first.weights == second.weights
    assert first.rounds == second.rounds


def test_raking_reports_empty_cell_without_inventing_sample():
    persons = _persons([("a", "n", "F"), ("b", "n", "M")])
    margins = {
        "age_band": {"a": 50.0, "b": 50.0, "under-18": 10.0},
        "region": {"n": 100.0},
        "sex": {"F": 50.0, "M": 60.0},
    }
    result = rake(persons, [1.0, 1.0], margins, RakingConfig())
    assert not result.success
    assert result.status == "empty_cell"
    assert result.empty_cells[0]["cell"] == "age_band=under-18"
    assert len(result.weights) == 2  # no fabricated rows


def test_raking_detects_unreachable_targets_under_bounds():
    persons = _persons([("a", "n", "F"), ("b", "n", "M")])
    margins = {
        "age_band": {"a": 900.0, "b": 100.0},
        "region": {"n": 1000.0},
        "sex": {"F": 500.0, "M": 500.0},
    }
    result = rake(persons, [1.0, 1.0], margins, RakingConfig(lower_factor=0.5, upper_factor=2.0))
    assert not result.success
    assert result.status == "bounds_unreachable"
    assert any(c["cell"] == "age_band=a" for c in result.unreachable_cells)


def test_raking_non_convergence_keeps_residuals():
    persons = _persons(
        [("a", "n", "F")] * 4 + [("b", "s", "M")] * 4 + [("c", "n", "M")] * 4
    )
    margins = {
        "age_band": {"a": 4.0, "b": 4.0, "c": 4.0},
        "region": {"n": 9.0, "s": 3.0},
        "sex": {"F": 7.0, "M": 5.0},
    }
    result = rake(
        persons,
        [1.0] * 12,
        margins,
        RakingConfig(tolerance=1e-12, max_iterations=1),
    )
    assert not result.success
    assert result.status == "non_convergence"
    assert result.cells and result.max_rel_residual > 1e-12
    # Residuals are preserved on every cell, including the ones that are close.
    assert len(result.cells) == 7


def test_stratum_merges_are_explicit_and_exclusive():
    mapping, applied = effective_strata(["a", "b", "c"], [["a", "b"]])
    assert mapping == {"a": "a+b", "b": "a+b", "c": "c"}
    assert applied == [{"merged_stratum": "a+b", "members": ["a", "b"]}]
    with pytest.raises(ValueError):
        effective_strata(["a", "b"], [["a", "b"], ["b"]])
