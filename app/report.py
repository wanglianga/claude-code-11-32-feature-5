"""Report assembly: per-program estimates, coverage gaps, limitations.

The published report is a JSON document stored once in ``run_reports``. It
contains no cross-program performance ranking, and it states which target
groups are under-covered.
"""
from __future__ import annotations

from typing import Any

from .jsonutil import loads
from .timeutil import iso

PROGRAM_COMPARISON_BLOCK = {
    "supported": False,
    "reason": (
        "Coverage gaps differ by program and by target group. Reach and mean "
        "minutes are population estimates conditional on the people the diary "
        "frame actually represents; a difference between two programs cannot be "
        "read as a difference in program performance."
    ),
}


def build_report(cur, run_id) -> dict[str, Any]:
    cur.execute("SELECT * FROM runs WHERE run_id=%s", (str(run_id),))
    run = cur.fetchone()
    config = loads(run["config"])
    calibration = loads(run["calibration"]) if run["calibration"] else None
    bootstrap = loads(run["bootstrap"]) if run["bootstrap"] else None
    inputs = {}
    cur.execute("SELECT kind, payload FROM run_inputs WHERE run_id=%s", (str(run_id),))
    for row in cur.fetchall():
        inputs[row["kind"]] = loads(row["payload"])

    cur.execute(
        """
        SELECT person_id, exclusion_code, exclusion_detail, eligible, strata_effective,
               age_band, region, sex, final_weight, inclusion_probability, completeness
          FROM run_persons WHERE run_id=%s ORDER BY person_id
        """,
        (str(run_id),),
    )
    persons = cur.fetchall()
    eligible = [p for p in persons if p["eligible"]]
    exclusions: dict[str, dict[str, Any]] = {}
    for p in persons:
        if p["eligible"]:
            continue
        code = p["exclusion_code"] or "unknown"
        bucket = exclusions.setdefault(code, {"count": 0, "examples": []})
        bucket["count"] += 1
        detail = loads(p["exclusion_detail"]) or {}
        bucket.setdefault("reason", detail.get("reason"))
        if len(bucket["examples"]) < 5:
            bucket["examples"].append({"person_id": p["person_id"], "detail": detail})

    cur.execute(
        "SELECT program_key, title, estimates, intervals, lineage_summary FROM run_program_estimates WHERE run_id=%s ORDER BY program_key",
        (str(run_id),),
    )
    programs = []
    for row in cur.fetchall():
        programs.append(
            {
                "program_key": row["program_key"],
                "title": row["title"],
                "estimates": loads(row["estimates"]),
                "intervals": loads(row["intervals"]),
                "lineage": loads(row["lineage_summary"]),
            }
        )

    cur.execute(
        "SELECT dimension, category, payload FROM run_coverage_gaps WHERE run_id=%s ORDER BY dimension, category",
        (str(run_id),),
    )
    gaps = [loads(r["payload"]) for r in cur.fetchall()]
    undercovered = [g for g in gaps if g.get("status") == "undercovered"]

    cur.execute(
        "SELECT person_id, station_a, station_b, start_utc, end_utc, status FROM run_conflicts WHERE run_id=%s ORDER BY person_id, start_utc",
        (str(run_id),),
    )
    conflicts = [
        {
            "person_id": r["person_id"],
            "stations": [r["station_a"], r["station_b"]],
            "start": iso(r["start_utc"]),
            "end": iso(r["end_utc"]),
            "status": r["status"],
        }
        for r in cur.fetchall()
    ]

    cur.execute(
        """
        SELECT disposition, COUNT(*) AS c FROM run_diary_usage WHERE run_id=%s GROUP BY disposition ORDER BY disposition
        """,
        (str(run_id),),
    )
    diary_dispositions = {r["disposition"]: r["c"] for r in cur.fetchall()}

    cur.execute(
        "SELECT survey_day, SUM(listening_minutes) AS l, SUM(no_listening_minutes) AS n, SUM(unknown_minutes) AS u, SUM(uncovered_minutes) AS uc FROM run_person_days WHERE run_id=%s GROUP BY 1 ORDER BY 1",
        (str(run_id),),
    )
    daily = [
        {
            "survey_day": r["survey_day"].isoformat(),
            "listening_minutes": float(r["l"]),
            "confirmed_no_listening_minutes": float(r["n"]),
            "unknown_minutes": float(r["u"]),
            "uncovered_minutes": float(r["uc"]),
        }
        for r in cur.fetchall()
    ]

    weights = [float(p["final_weight"]) for p in eligible if p["final_weight"] is not None]
    report = {
        "run_id": str(run_id),
        "status": "published",
        "generated_from_inputs": inputs,
        "seed": run["seed"],
        "config": config,
        "sample": {
            "frame_persons": len(persons),
            "eligible_persons": len(eligible),
            "excluded_persons": len(persons) - len(eligible),
            "exclusions": exclusions,
            "weight_min": round(min(weights), 6) if weights else None,
            "weight_max": round(max(weights), 6) if weights else None,
            "weight_sum": round(sum(weights), 6) if weights else None,
            "effective_sample_size": kish(weights),
        },
        "calibration": calibration,
        "bootstrap": bootstrap,
        "programs": programs,
        "coverage_gaps": undercovered,
        "coverage_assessment": {
            "method": "unweighted eligible diarists per group versus target population share",
            "under_representation_ratio_floor": config["thresholds"]["under_representation_ratio"],
            "min_cell_persons": config["thresholds"]["min_cell_persons"],
            "dimensions": sorted({g["dimension"] for g in gaps}),
            "undercovered_groups": [
                f"{g['dimension']}={g['category']}" for g in undercovered
            ],
            "why_not_weighted_shares": (
                "Calibrated weights are forced onto the margins, so a weighted coverage check is "
                "guaranteed to look complete. The reported gaps come from how many eligible diarists "
                "each group actually produced."
            ),
        },
        "conflicts": {
            "pending": conflicts,
            "resolution_policy": "unresolved conflicts make the affected minutes unknown; analysts may confirm a resolution and start a new run",
        },
        "diary_usage": diary_dispositions,
        "daily_totals": daily,
        "missing_data_policy": {
            "uncovered_minutes": "unknown: the diary was not filled for this period",
            "unknown_minutes": "unknown: contradictory station information",
            "never_imputed_as_no_listening": True,
            "persons_without_diary_excluded_not_zero": True,
        },
        "comparison": PROGRAM_COMPARISON_BLOCK,
        "limitations": [
            "Fictional data only: no real population register or listening device is used.",
            "Reach and mean minutes are model estimates; only the recorded diary coverage is represented.",
            "Unknown inclusion probability blocks a formal population estimate.",
            "Uncalibrated or non-converged weights are never published.",
        ],
    }
    return report


def kish(weights: list[float]) -> float | None:
    if not weights:
        return None
    s = sum(weights)
    sq = sum(w * w for w in weights)
    return round((s * s) / sq, 3) if sq else None
