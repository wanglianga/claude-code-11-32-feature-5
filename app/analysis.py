"""Analysis sample construction and per-person program exposure.

Every step records *why* a person or diary row entered or left the calculation,
so a single program number can be traced back to the rows behind it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from .analytics import diary as diary_analysis
from .analytics.diary import Submission
from .analytics.raking import effective_strata
from .errors import invalid
from .jsonutil import loads
from .timeutil import (
    clip,
    day_window,
    length_minutes,
    merge,
    parse_hhmm,
    subtract,
)

DIMENSIONS = ("age_band", "region", "sex")
MAX_SURVEY_DAYS = 92


@dataclass
class FramePerson:
    person_id: str
    household_id: str
    strata: str
    strata_effective: str
    age_band: str
    region: str
    sex: str
    inclusion_probability: float | None
    base_weight: float | None
    eligible: bool = False
    exclusion_code: str | None = None
    exclusion_detail: dict[str, Any] = field(default_factory=dict)


def survey_days(period_start: date, period_end: date) -> list[date]:
    if period_end < period_start:
        raise invalid("survey_period_invalid", "survey period end precedes start")
    span = (period_end - period_start).days + 1
    if span > MAX_SURVEY_DAYS:
        raise invalid(
            "survey_period_too_long",
            f"survey period exceeds {MAX_SURVEY_DAYS} days",
            {"days": span},
        )
    return [date.fromordinal(period_start.toordinal() + i) for i in range(span)]


def build_frame(
    recruitment: dict[str, Any],
    diary_by_person: dict[str, list[dict[str, Any]]],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Combine recruitment rows and diary submissions into the analysis frame."""
    rows = recruitment["rows"]
    probabilities = [float(r["inclusion_probability"]) for r in rows if r["inclusion_probability"] is not None]
    unknown = [r["person_id"] for r in rows if r["inclusion_probability"] is None]
    strata_map, merges = effective_strata(
        [r["strata"] for r in rows], config["raking"]["stratum_merges"]
    )
    persons: list[FramePerson] = []
    for r in rows:
        p = float(r["inclusion_probability"]) if r["inclusion_probability"] is not None else None
        fp = FramePerson(
            person_id=r["person_id"],
            household_id=r["household_id"],
            strata=r["strata"],
            strata_effective=strata_map[r["strata"]],
            age_band=r["age_band"],
            region=r["region"],
            sex=r["sex"],
            inclusion_probability=p,
            base_weight=(1.0 / p) if p else None,
        )
        subs = diary_by_person.get(fp.person_id, [])
        if p is None:
            fp.exclusion_code = "inclusion_probability_unknown"
            fp.exclusion_detail = {
                "reason": "Base weight 1/p is undefined, so no formal population estimate is possible.",
                "frame_can_estimate": False,
            }
        elif not subs:
            fp.exclusion_code = "missing_diary"
            fp.exclusion_detail = {
                "reason": "No diary submission at the pinned cutoff. Missing is treated as unknown, not as zero listening.",
                "treat_missing_as_zero_listening": False,
                "unknown_minutes_imputed": None,
            }
        else:
            mismatched = sorted({s["household_id"] for s in subs if s["household_id"] != fp.household_id})
            if mismatched:
                fp.exclusion_code = "household_mismatch"
                fp.exclusion_detail = {
                    "reason": "Diary household differs from recruitment household; the clustering unit is ambiguous.",
                    "diary_households": mismatched,
                }
        persons.append(fp)

    frame_ids = {p.person_id for p in persons}
    diary_only = [
        {"person_id": pid, "submission_count": len(subs)}
        for pid, subs in sorted(diary_by_person.items())
        if pid not in frame_ids
    ]
    return {
        "persons": persons,
        "strata_merges": merges,
        "unknown_probability_persons": unknown,
        "diary_only_persons": diary_only,
        "probability_range": {
            "min": min(probabilities) if probabilities else None,
            "max": max(probabilities) if probabilities else None,
        },
        "formal_frame_possible": not unknown,
    }


def submissions_of(subs: list[dict[str, Any]]) -> list[Submission]:
    out = []
    for sub in subs:
        out.append(
            Submission(
                submission_seq=sub["submission_seq"],
                revision=sub["revision"],
                submitted_at=sub["submitted_at"],
                revision_note=None,
                replacement_scope=loads(sub["replacement_scope"]) or {"type": "append"},
                coverage=diary_analysis.parse_coverage(loads(sub["coverage"]) or []),
                entries=diary_analysis.parse_entries(sub["entries"], sub["revision"]),
            )
        )
    return out


def resolution_key(person_id: str, a: str, b: str, start: datetime, end: datetime) -> tuple:
    return (person_id, *(sorted((a, b))), start, end)


def apply_resolutions(
    person_id: str,
    station_intervals: dict[str, list[tuple[datetime, datetime]]],
    conflicts: list[dict[str, Any]],
    resolutions: dict[tuple, dict[str, Any]],
) -> dict[str, Any]:
    """Apply analyst conflict decisions; unresolved conflicts stay unknown.

    Confirming station A removes the identical minutes from station B, so the
    same physical minutes are never counted twice.
    """
    unknown: list[tuple[datetime, datetime]] = []
    confirmed_nonlisten: list[tuple[datetime, datetime]] = []
    applied = []
    pending = []
    for c in conflicts:
        key = resolution_key(person_id, c["station_a"], c["station_b"], c["start"], c["end"])
        decision = resolutions.get(key)
        seg = (c["start"], c["end"])
        if decision is None:
            status = "pending"
        else:
            status = decision["resolution"]
            applied.append(
                {
                    "resolution_id": decision.get("resolution_id"),
                    "status": status,
                    "start": c["start"],
                    "end": c["end"],
                    "stations": [c["station_a"], c["station_b"]],
                }
            )
        if status == "confirmed_station_a":
            station_intervals[c["station_a"]] = station_intervals.get(c["station_a"], []) + [seg]
            station_intervals[c["station_b"]] = [
                p for p in subtract(station_intervals.get(c["station_b"], []), [seg]) if p
            ]
        elif status == "confirmed_station_b":
            station_intervals[c["station_b"]] = station_intervals.get(c["station_b"], []) + [seg]
            station_intervals[c["station_a"]] = [
                p for p in subtract(station_intervals.get(c["station_a"], []), [seg]) if p
            ]
        elif status == "confirmed_neither":
            for station in (c["station_a"], c["station_b"]):
                station_intervals[station] = [
                    p for p in subtract(station_intervals.get(station, []), [seg]) if p
                ]
            confirmed_nonlisten.append(seg)
        else:  # pending or dismissed
            unknown.append(seg)
            if status == "dismissed":
                # Dismissed means "not a real conflict" in the analyst's view;
                # the rows stay as they were reported and remain unconfirmed.
                continue
        if status == "pending":
            pending.append(
                {
                    "station_a": c["station_a"],
                    "station_b": c["station_b"],
                    "start": c["start"],
                    "end": c["end"],
                    "entry_a": c.get("entry_a"),
                    "entry_b": c.get("entry_b"),
                    "minutes": round(c["minutes"], 3),
                }
            )
    return {
        "station_intervals": {s: merge(iv) for s, iv in station_intervals.items()},
        "unknown_ranges": merge(unknown),
        "confirmed_nonlisten_ranges": merge(confirmed_nonlisten),
        "applied_resolutions": applied,
        "pending_conflicts": pending,
    }


def reconstruct_and_resolve(
    person_id: str,
    subs: list[dict[str, Any]],
    resolutions: dict[tuple, dict[str, Any]],
    tz: ZoneInfo,
) -> dict[str, Any]:
    reconstruction = diary_analysis.reconstruct(submissions_of(subs), tz)
    per_station: dict[str, list[tuple[datetime, datetime]]] = {}
    for entry in reconstruction["entries"]:
        per_station.setdefault(entry["station"], []).append((entry["start"], entry["end"]))
    resolved = apply_resolutions(person_id, per_station, reconstruction["conflicts"], resolutions)
    return {**reconstruction, "resolved": resolved}


def exposure_intervals(
    station_intervals: dict[str, list[tuple[datetime, datetime]]],
    unknown_ranges: list[tuple[datetime, datetime]],
    confirmed_nonlisten: list[tuple[datetime, datetime]],
    station: str,
    slot: tuple[datetime, datetime],
) -> list[tuple[datetime, datetime]]:
    """Confirmed listening to a station inside a half-open program slot."""
    pieces: list[tuple[datetime, datetime]] = []
    for interval in station_intervals.get(station, []):
        for seg in subtract([interval], unknown_ranges + confirmed_nonlisten):
            piece = clip(seg[0], seg[1], slot[0], slot[1])
            if piece is not None:
                pieces.append(piece)
    return merge(pieces)


def analyze_person(
    fp: FramePerson,
    subs: list[dict[str, Any]],
    expected_days: list[date],
    config: dict[str, Any],
    resolutions: dict[tuple, dict[str, Any]],
    tz: ZoneInfo,
) -> dict[str, Any]:
    """Reconstruct one person's diary, classify coverage and record usage."""
    window_cfg = config["diary_window"]
    start_min = parse_hhmm(window_cfg["start_local"])
    end_min = parse_hhmm(window_cfg["end_local"])
    windows = {day: day_window(day, start_min, end_min, tz) for day in expected_days}

    reconstruction = reconstruct_and_resolve(fp.person_id, subs, resolutions, tz)
    resolved = reconstruction["resolved"]

    usage: list[dict[str, Any]] = []
    for entry in reconstruction["entries"]:
        usage.append(
            {
                "entry_id": entry.get("entry_id"),
                "submission_seq": entry.get("submission_seq"),
                "disposition": "used",
                "reason": "active entry contributes to the current calculation",
                "start_utc": entry["start"],
                "end_utc": entry["end"],
                "station": entry["station"],
                "source_serial": entry["source_serial"],
                "revision": entry["revision"],
                "survey_days_touched": [
                    d.isoformat()
                    for d, (lo, hi) in windows.items()
                    if clip(entry["start"], entry["end"], lo, hi) is not None
                ],
            }
        )
    for retired in reconstruction["retired"]:
        usage.append(
            {
                "entry_id": retired.get("entry_id"),
                "submission_seq": retired.get("submission_seq"),
                "disposition": "superseded_retired",
                "reason": "removed from the current calculation by a declared replacement scope; history retained",
                "start_utc": retired.get("start"),
                "end_utc": retired.get("end"),
                "station": retired.get("station"),
                "source_serial": retired.get("source_serial"),
                "revision": retired.get("revision"),
                "retired_by_submission_seq": retired.get("retired_by_submission_seq"),
            }
        )
    for conflict in resolved["pending_conflicts"]:
        usage.append(
            {
                "entry_id": None,
                "submission_seq": None,
                "disposition": "conflict_pending",
                "reason": "two stations reported simultaneously; left unconfirmed, never resolved by upload order",
                "start_utc": conflict["start"],
                "end_utc": conflict["end"],
                "station": f"{conflict['station_a']}|{conflict['station_b']}",
                "source_serial": None,
                "revision": None,
            }
        )
    for decision in resolved["applied_resolutions"]:
        usage.append(
            {
                "entry_id": None,
                "submission_seq": None,
                "disposition": f"conflict_{decision['status']}",
                "reason": "analyst-confirmed conflict decision applied to this run",
                "start_utc": decision["start"],
                "end_utc": decision["end"],
                "station": "|".join(decision["stations"]),
                "source_serial": None,
                "revision": None,
                "resolution_id": decision["resolution_id"],
            }
        )

    day_reports = []
    for day in expected_days:
        lo, hi = windows[day]
        day_reports.append(
            diary_analysis.day_accounting(
                reconstruction["entries"],
                reconstruction["coverage"],
                resolved["unknown_ranges"],
                resolved["confirmed_nonlisten_ranges"],
                day,
                lo,
                hi,
                tz,
            )
        )
    completeness = diary_analysis.completeness(day_reports, config["completeness"], expected_days)
    return {
        "reconstruction": reconstruction,
        "resolved": resolved,
        "usage": usage,
        "day_reports": day_reports,
        "completeness": completeness,
    }


def day_windows(expected_days: list[date], config: dict[str, Any], tz: ZoneInfo):
    start_min = parse_hhmm(config["diary_window"]["start_local"])
    end_min = parse_hhmm(config["diary_window"]["end_local"])
    return {day: day_window(day, start_min, end_min, tz) for day in expected_days}


def program_exposure(
    station_intervals: dict[str, list[tuple[datetime, datetime]]],
    unknown_ranges: list[tuple[datetime, datetime]],
    confirmed_nonlisten: list[tuple[datetime, datetime]],
    program_map: dict[str, dict[str, Any]],
    expected_days: list[date],
    windows: dict[date, tuple[datetime, datetime]],
    threshold: float,
) -> dict[str, dict[str, Any]]:
    """Per-program listening minutes for one person.

    Same-person same-station overlaps are unioned (count once); unresolved
    cross-station overlaps are excluded as unknown.
    """
    out: dict[str, dict[str, Any]] = {}
    for key, program in program_map.items():
        covered: list[tuple[datetime, datetime]] = []
        by_airing = []
        for airing in program["airings"]:
            slot = (airing["start_utc"], airing["end_utc"])
            hits = exposure_intervals(
                station_intervals, unknown_ranges, confirmed_nonlisten, airing["station"], slot
            )
            if hits:
                covered.extend(hits)
                by_airing.append(
                    {
                        "station": airing["station"],
                        "air_date": airing["air_date"].isoformat(),
                        "start_local": airing["start_local"],
                        "end_local": airing["end_local"],
                        "minutes": round(length_minutes(hits), 3),
                    }
                )
        covered = merge(covered)
        minutes = length_minutes(covered)
        by_day: dict[str, float] = {}
        by_day_range: dict[str, list[dict[str, str]]] = {}
        for day, (lo, hi) in windows.items():
            pieces = [p for p in (clip(s, e, lo, hi) for s, e in covered) if p is not None]
            if pieces:
                by_day[day.isoformat()] = round(length_minutes(pieces), 3)
                by_day_range[day.isoformat()] = [
                    {"start": p[0].isoformat(), "end": p[1].isoformat()} for p in pieces
                ]
        out[key] = {
            "minutes": round(minutes, 3),
            "by_day": by_day,
            "by_day_ranges": by_day_range,
            "by_airing": by_airing,
            "reached": minutes >= threshold,
        }
    return out
