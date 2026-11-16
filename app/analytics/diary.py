"""Diary reconstruction, station conflicts and per-day coverage accounting.

Rules implemented here:
* Every resubmission must declare an explicit replacement scope. Rows that
  intersect the scope stop contributing to the current calculation but remain
  in history (``superseded_by_submission_seq``).
* A replacement scope spanning more than one survey day must start and end on
  survey-day boundaries, so a cross-day replacement can never delete only half
  of a day.
* Same person + same station overlapping intervals count once (union).
  Overlaps across different stations are left as *unconfirmed conflicts* and
  never resolved by "last upload wins".
* Each minute of each declared daily window is classified into exactly one of:
  confirmed listening, confirmed no listening, unconfirmed conflict, uncovered.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from ..errors import invalid
from ..timeutil import clip, length_minutes, merge, parse_aware, require_interval, subtract


@dataclass
class Submission:
    submission_seq: int
    revision: int
    submitted_at: datetime
    revision_note: str | None
    replacement_scope: dict[str, Any]
    coverage: list[tuple[datetime, datetime]]
    entries: list[dict[str, Any]]


def parse_coverage(raw: list[dict[str, str]]) -> list[tuple[datetime, datetime]]:
    ranges = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict) or "start" not in item or "end" not in item:
            raise invalid("diary_coverage_invalid", f"coverage window #{idx} needs start and end")
        s = parse_aware(item["start"], f"coverage[{idx}].start")
        e = parse_aware(item["end"], f"coverage[{idx}].end")
        require_interval(s, e)
        ranges.append((s, e))
    return merge(ranges)


def parse_entries(raw: list[dict[str, Any]], revision: int) -> list[dict[str, Any]]:
    out = []
    for idx, item in enumerate(raw):
        missing = [k for k in ("start", "end", "station", "source_serial") if not item.get(k)]
        if missing:
            raise invalid(
                "diary_entry_invalid",
                f"entry #{idx} missing fields: {', '.join(missing)}",
            )
        s = parse_aware(item["start"], f"entries[{idx}].start")
        e = parse_aware(item["end"], f"entries[{idx}].end")
        require_interval(s, e)
        out.append(
            {
                "order": idx,
                "entry_id": item.get("entry_id"),
                "start": s,
                "end": e,
                "start_raw": item["start"],
                "end_raw": item["end"],
                "station": str(item["station"]).strip(),
                "source_serial": str(item["source_serial"]).strip(),
                "revision": int(item.get("revision") if item.get("revision") is not None else revision),
            }
        )
    return out


def _scope_range(scope: dict[str, Any], tz, submission_seq: int) -> tuple[datetime, datetime] | None:
    scope_type = (scope or {}).get("type", "append")
    if scope_type == "append":
        return None
    if scope_type != "replace":
        raise invalid("diary_scope_invalid", "replacement_scope.type must be append or replace")
    try:
        ss = parse_aware(scope["start"], "replacement_scope.start")
        se = parse_aware(scope["end"], "replacement_scope.end")
    except (KeyError, TypeError) as exc:
        raise invalid(
            "diary_scope_invalid",
            "replace scope requires start and end",
        ) from exc
    require_interval(ss, se)
    local_start = ss.astimezone(tz)
    local_end = se.astimezone(tz)
    if local_start.date() != local_end.date():
        start_midnight = (local_start.hour, local_start.minute, local_start.second, local_start.microsecond) == (0, 0, 0, 0)
        end_midnight = (local_end.hour, local_end.minute, local_end.second, local_end.microsecond) == (0, 0, 0, 0)
        if not (start_midnight and end_midnight):
            raise invalid(
                "diary_partial_day_replacement",
                "a replacement spanning several survey days must cover whole survey days",
                {
                    "submission_seq": submission_seq,
                    "scope_start_local": local_start.isoformat(),
                    "scope_end_local": local_end.isoformat(),
                },
            )
    return (ss, se)


def reconstruct(submissions: list[Submission], tz) -> dict[str, Any]:
    """Rebuild the effective diary view for one person at the pinned cutoff."""
    if not submissions:
        return {
            "entries": [],
            "coverage": [],
            "retired": [],
            "conflicts": [],
            "conflict_ranges": [],
            "submission_count": 0,
            "last_revision": None,
            "last_submission_seq": None,
            "valid": True,
        }

    ordered = sorted(submissions, key=lambda s: s.submission_seq)
    active_entries: list[dict[str, Any]] = []
    active_coverage: list[tuple[datetime, datetime]] = []
    retired: list[dict[str, Any]] = []
    last_revision = 0

    for pos, sub in enumerate(ordered):
        if sub.revision <= last_revision:
            raise invalid(
                "diary_revision_conflict",
                "resubmission revision must increase",
                {
                    "submission_seq": sub.submission_seq,
                    "revision": sub.revision,
                    "previous_revision": last_revision,
                },
            )
        scope = _scope_range(sub.replacement_scope, tz, sub.submission_seq)
        if pos > 0 and scope is None:
            # Appending may not silently overwrite retired coverage: the caller
            # must declare exactly which window the new upload replaces.
            for cov in sub.coverage:
                for old in active_coverage:
                    if clip(cov[0], cov[1], old[0], old[1]) is not None:
                        raise invalid(
                            "diary_replacement_scope_required",
                            "resubmission coverage overlaps existing coverage without a replacement scope",
                            {"submission_seq": sub.submission_seq},
                        )
            for entry in sub.entries:
                for old in active_coverage:
                    if clip(entry["start"], entry["end"], old[0], old[1]) is not None:
                        raise invalid(
                            "diary_replacement_scope_required",
                            "resubmission entry overlaps existing coverage without a replacement scope",
                            {"submission_seq": sub.submission_seq},
                        )

        if scope is not None:
            surviving_entries = []
            for entry in active_entries:
                if clip(entry["start"], entry["end"], scope[0], scope[1]) is not None:
                    retired.append({**entry, "retired_by_submission_seq": sub.submission_seq})
                else:
                    surviving_entries.append(entry)
            active_entries = surviving_entries
            # Coverage windows are kept un-merged across submissions so a
            # replacement can retire exactly the scoped part: a 3-day filled
            # window replaced for one day keeps the other two days filled.
            surviving_coverage = []
            for cov in active_coverage:
                overlapped = clip(cov[0], cov[1], scope[0], scope[1])
                if overlapped is None:
                    surviving_coverage.append(cov)
                    continue
                retired.append(
                    {
                        "entry_id": None,
                        "kind": "coverage_window",
                        "start": overlapped[0],
                        "end": overlapped[1],
                        "retired_by_submission_seq": sub.submission_seq,
                    }
                )
                surviving_coverage.extend(subtract([cov], [scope]))
            active_coverage = surviving_coverage

        for entry in sub.entries:
            active_entries.append({**entry, "submission_seq": sub.submission_seq})
        active_coverage = active_coverage + sub.coverage
        last_revision = sub.revision

    # Declared listening always implies the diary was filled for that period.
    declared = merge(active_coverage + [(e["start"], e["end"]) for e in active_entries])
    conflicts = detect_conflicts(active_entries)
    conflict_ivs = conflict_ranges(active_entries)
    return {
        "entries": active_entries,
        "coverage": declared,
        "retired": retired,
        "conflicts": conflicts,
        "conflict_ranges": conflict_ivs,
        "submission_count": len(ordered),
        "last_revision": last_revision,
        "last_submission_seq": ordered[-1].submission_seq,
        "valid": not conflicts,
    }


def conflict_ranges(entries: list[dict[str, Any]]) -> list[tuple[datetime, datetime]]:
    """Regions where at least two distinct stations are reported simultaneously."""
    events: list[tuple[datetime, int, str]] = []
    for e in entries:
        events.append((e["start"], 1, e["station"]))
        events.append((e["end"], -1, e["station"]))
    # Sort so end events at the same instant close before new intervals open.
    events.sort(key=lambda ev: (ev[0], ev[1]))
    active: dict[str, int] = {}
    open_at: datetime | None = None
    ranges: list[tuple[datetime, datetime]] = []

    def distinct() -> int:
        return sum(1 for v in active.values() if v > 0)

    for instant, delta, station in events:
        before = distinct()
        if delta == -1:
            active[station] = active.get(station, 0) - 1
        else:
            active[station] = active.get(station, 0) + 1
        after = distinct()
        if before < 2 <= after:
            open_at = instant
        elif before >= 2 > after and open_at is not None and instant > open_at:
            ranges.append((open_at, instant))
            open_at = None
    return merge(ranges)


def detect_conflicts(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Detailed pairwise report of cross-station overlaps (unconfirmed)."""
    conflicts: list[dict[str, Any]] = []
    ordered = sorted(entries, key=lambda e: (e["start"], e["end"]))
    for i, a in enumerate(ordered):
        for b in ordered[i + 1 :]:
            if b["start"] >= a["end"]:
                break
            if a["station"] == b["station"]:
                continue
            seg = clip(b["start"], b["end"], a["start"], a["end"])
            if seg is None:
                continue
            conflicts.append(
                {
                    "station_a": a["station"],
                    "station_b": b["station"],
                    "start": seg[0],
                    "end": seg[1],
                    "entry_a": a.get("entry_id"),
                    "entry_b": b.get("entry_id"),
                    "minutes": (seg[1] - seg[0]).total_seconds() / 60.0,
                }
            )
    unique: dict[tuple, dict[str, Any]] = {}
    for c in conflicts:
        stations = tuple(sorted((c["station_a"], c["station_b"])))
        key = (stations[0], stations[1], c["start"], c["end"])
        unique.setdefault(key, c)
    return list(unique.values())


def day_accounting(
    entries: list[dict[str, Any]],
    coverage: list[tuple[datetime, datetime]],
    unknown_ranges: list[tuple[datetime, datetime]],
    confirmed_nonlisten: list[tuple[datetime, datetime]],
    day: date,
    window_start: datetime,
    window_end: datetime,
    tz,
) -> dict[str, Any]:
    """Partition the day's declared window into four disjoint minute buckets.

    Buckets: listening | no_listening | unknown (unconfirmed conflict) |
    uncovered. Both ``unknown`` and ``uncovered`` minutes are MISSING: they are
    never converted into "no listening". ``uncovered`` = the person did not
    fill that part of the day at all; ``unknown`` = they filled it with
    contradictory station information.
    """
    window = [(window_start, window_end)]
    filled = merge([seg for seg in (clip(s, e, window_start, window_end) for s, e in coverage) if seg])
    uncovered = subtract(window, filled)
    unknown_bucket = merge(
        [seg for seg in (clip(s, e, window_start, window_end) for s, e in unknown_ranges) if seg]
    )
    unknown_bucket = subtract(unknown_bucket, uncovered)
    nonlisten_bucket = merge(
        [seg for seg in (clip(s, e, window_start, window_end) for s, e in confirmed_nonlisten) if seg]
    )
    nonlisten_bucket = subtract(nonlisten_bucket, uncovered + unknown_bucket)
    confirmed = subtract(filled, unknown_bucket + nonlisten_bucket)
    listening = merge(
        [seg for seg in (clip(e["start"], e["end"], window_start, window_end) for e in entries) if seg]
    )
    listening = subtract(listening, unknown_bucket + nonlisten_bucket)
    no_listening = subtract(confirmed, listening)

    listening_min = length_minutes(listening)
    no_listening_min = length_minutes(no_listening)
    unknown_min = length_minutes(unknown_bucket)
    uncovered_min = length_minutes(uncovered)
    window_min = length_minutes(window)
    total = listening_min + no_listening_min + unknown_min + uncovered_min
    return {
        "survey_day": day,
        "window_start": window_start,
        "window_end": window_end,
        "window_minutes": round(window_min, 3),
        "covered_minutes": round(window_min - uncovered_min, 3),
        "listening_minutes": round(listening_min, 3),
        "no_listening_minutes": round(no_listening_min, 3),
        "unknown_minutes": round(unknown_min, 3),
        "uncovered_minutes": round(uncovered_min, 3),
        "missing_minutes": round(unknown_min + uncovered_min, 3),
        "partition_check": round(total - window_min, 6),
        "segments": {
            "listening": listening,
            "no_listening": no_listening,
            "unknown": unknown_bucket,
            "uncovered": uncovered,
        },
    }


def completeness(
    day_reports: list[dict[str, Any]],
    policy: dict[str, Any],
    expected_days: list[date],
) -> dict[str, Any]:
    """Evaluate the pre-declared complete-recall condition."""
    kind = policy.get("type", "full_coverage")
    required = float(policy.get("max_uncovered_minutes", 0.0))
    min_days = int(policy.get("min_days", len(expected_days)))
    per_day = []
    for rep in day_reports:
        complete = rep["missing_minutes"] <= required
        per_day.append(
            {
                "survey_day": rep["survey_day"].isoformat(),
                "uncovered_minutes": rep["uncovered_minutes"],
                "unknown_minutes": rep["unknown_minutes"],
                "missing_minutes": rep["missing_minutes"],
                "complete": complete,
            }
        )
    complete_days = [d for d in per_day if d["complete"]]
    if kind == "full_coverage":
        ok = len(complete_days) == len(expected_days)
    elif kind == "min_complete_days":
        ok = len(complete_days) >= min(min_days, len(expected_days))
    else:
        raise invalid("completeness_policy_invalid", f"unknown completeness policy {kind!r}")
    return {
        "policy": policy,
        "type": kind,
        "expected_days": [d.isoformat() for d in expected_days],
        "complete_days": [d["survey_day"] for d in complete_days],
        "complete_day_count": len(complete_days),
        "per_day": per_day,
        "satisfied": ok,
    }
