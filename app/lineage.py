"""Lineage: from one published program number back to rows and decisions."""
from __future__ import annotations

from typing import Any

from .errors import not_found
from .jsonutil import loads


def _fetch_estimate(cur, run_id: str, program_key: str) -> dict[str, Any]:
    cur.execute(
        "SELECT title, estimates, intervals, by_domain, lineage_summary FROM run_program_estimates WHERE run_id=%s AND program_key=%s",
        (run_id, program_key),
    )
    row = cur.fetchone()
    if row is None:
        raise not_found(
            "program_estimate_not_found",
            "no estimate for this program in this run",
            {"run_id": run_id, "program_key": program_key},
        )
    return row


def program_lineage(cur, run_id: str, program_key: str) -> dict[str, Any]:
    row = _fetch_estimate(cur, run_id, program_key)
    estimates = loads(row["estimates"])
    intervals = loads(row["intervals"])
    lineage = loads(row["lineage_summary"]) or {}

    cur.execute(
        """
        SELECT COUNT(*) AS n,
               COUNT(*) FILTER (WHERE p.eligible) AS eligible,
               COUNT(*) FILTER (WHERE pp.minutes > 0) AS listeners,
               COUNT(*) FILTER (WHERE pp.reached) AS reached_rows,
               SUM(pp.minutes) AS minutes
          FROM run_person_program pp
          JOIN run_persons p ON p.run_id = pp.run_id AND p.person_id = pp.person_id
         WHERE pp.run_id=%s AND pp.program_key=%s
        """,
        (run_id, program_key),
    )
    usage = cur.fetchone()

    cur.execute(
        """
        SELECT person_id, exclusion_code, exclusion_detail
          FROM run_persons WHERE run_id=%s AND NOT eligible
         ORDER BY person_id
        """,
        (run_id,),
    )
    exclusions: dict[str, dict[str, Any]] = {}
    for r in cur.fetchall():
        code = r["exclusion_code"] or "unknown"
        bucket = exclusions.setdefault(code, {"count": 0, "persons": []})
        bucket["count"] += 1
        if len(bucket["persons"]) < 25:
            bucket["persons"].append(
                {"person_id": r["person_id"], "detail": loads(r["exclusion_detail"])}
            )

    cur.execute(
        "SELECT stage, status, payload FROM run_stages WHERE run_id=%s ORDER BY stage", (run_id,)
    )
    stages = [
        {"stage": s["stage"], "status": s["status"], "payload": loads(s["payload"])}
        for s in cur.fetchall()
    ]

    cur.execute("SELECT kind, payload FROM run_inputs WHERE run_id=%s", (run_id,))
    inputs = {r["kind"]: loads(r["payload"]) for r in cur.fetchall()}

    return {
        "run_id": run_id,
        "program_key": program_key,
        "title": row["title"],
        "number": {
            "estimates": estimates,
            "intervals": intervals,
            "by_domain": loads(row["by_domain"]),
        },
        "trace": {
            "inputs": inputs,
            "stages": stages,
            "sample_rows": {
                "person_program_rows": usage["n"],
                "eligible_persons": usage["eligible"],
                "persons_with_minutes": usage["listeners"],
                "persons_at_or_above_threshold": usage["reached_rows"],
                "summed_minutes": float(usage["minutes"] or 0.0),
                "calibration": lineage.get("calibration_status"),
                "seed": lineage.get("seed"),
                "reach_threshold_minutes": lineage.get("reach_threshold_minutes"),
                "bootstrap": lineage.get("bootstrap"),
            },
            "interval_decomposition": {
                "method": "household bootstrap, recalibrated each replicate, 95% percentile",
                "reach_interval": (intervals or {}).get("reach_persons"),
                "mean_per_person_interval": (intervals or {}).get("mean_minutes_per_person"),
                "mean_per_listener_interval": (intervals or {}).get("mean_minutes_per_listener"),
                "replicates_used": (intervals or {}).get("replicates_used"),
                "see": f"/v1/runs/{run_id}/bootstrap",
            },
            "weight_adjustment": {
                "see": f"/v1/runs/{run_id}/calibration",
                "method": "bounded raking on age_band x region x sex margins",
            },
            "exclusions": exclusions,
            "exclusion_totals": {k: v["count"] for k, v in exclusions.items()},
            "conflicts": {"see": f"/v1/runs/{run_id}/conflicts"},
            "diary_usage": {"see": f"/v1/runs/{run_id}/diary-usage"},
            "coverage_gaps": {"see": f"/v1/runs/{run_id}/coverage-gaps"},
        },
        "comparison": {
            "supported": False,
            "reason": "coverage gaps make cross-program performance comparison invalid for this service",
        },
    }


def program_sample(cur, run_id: str, program_key: str, limit: int, offset: int) -> dict[str, Any]:
    _fetch_estimate(cur, run_id, program_key)
    cur.execute(
        """
        SELECT p.person_id, p.household_id, p.age_band, p.region, p.sex, p.strata_effective,
               p.inclusion_probability, p.base_weight, p.final_weight, p.eligible,
               p.exclusion_code, pp.minutes, pp.reached, pp.by_day, pp.by_airing
          FROM run_persons p
          LEFT JOIN run_person_program pp
                 ON pp.run_id = p.run_id AND pp.person_id = p.person_id AND pp.program_key = %s
         WHERE p.run_id = %s
         ORDER BY p.person_id
         LIMIT %s OFFSET %s
        """,
        (program_key, run_id, limit, offset),
    )
    rows = cur.fetchall()
    cur.execute("SELECT COUNT(*) AS c FROM run_persons WHERE run_id=%s", (run_id,))
    total = cur.fetchone()["c"]
    return {
        "run_id": run_id,
        "program_key": program_key,
        "total_persons": total,
        "limit": limit,
        "offset": offset,
        "rows": [
            {
                **r,
                "by_day": loads(r["by_day"]),
                "by_airing": loads(r["by_airing"]),
            }
            for r in rows
        ],
    }
