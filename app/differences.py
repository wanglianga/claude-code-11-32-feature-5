"""Difference intervals.

Two different questions:

1. *Within one program*, between two demographic groups (e.g. 18-24 vs 45-64).
   This is a legitimate subgroup contrast and gets a bootstrap interval when
   the replicates are usable.
2. *Between two programs*. The service refuses this: coverage gaps differ by
   program and by group, so a program-versus-program difference is not a
   performance comparison. The refusal is a first-class response, not an
   error, so a caller can see exactly why.
"""
from __future__ import annotations

from typing import Any

from .analytics.estimate import difference_interval
from .errors import invalid, not_found
from .jsonutil import loads


def program_group_difference(
    cur,
    run_id: str,
    program_key: str,
    dimension: str,
    group_a: str,
    group_b: str,
    metric: str = "mean_minutes_per_person",
) -> dict[str, Any]:
    cur.execute(
        """
        SELECT estimates, intervals, by_domain FROM run_program_estimates
         WHERE run_id=%s AND program_key=%s
        """,
        (run_id, program_key),
    )
    row = cur.fetchone()
    if row is None:
        raise not_found("program_estimate_not_found", "no estimate for this program", {"program_key": program_key})
    by_domain = loads(row["by_domain"]) or {}
    if dimension not in by_domain:
        raise invalid("dimension_unknown", f"no domain breakdown for {dimension}", {"available": sorted(by_domain)})
    cats = by_domain[dimension]
    missing = [g for g in (group_a, group_b) if g not in cats]
    if missing:
        raise invalid("group_unknown", "unknown group in difference request", {"missing": missing})
    a, b = cats[group_a], cats[group_b]
    if metric not in ("mean_minutes_per_person", "reach_rate"):
        raise invalid("metric_unknown", "metric must be mean_minutes_per_person or reach_rate")
    est_a, est_b = a.get(metric), b.get(metric)
    if est_a is None or est_b is None:
        raise invalid("estimate_unavailable", "metric is undefined for one of the groups")

    cur.execute("SELECT interval_status FROM runs WHERE run_id=%s", (run_id,))
    status_row = cur.fetchone()
    interval_status = status_row["interval_status"] if status_row else None

    cur.execute(
        "SELECT results FROM run_bootstrap_replicates WHERE run_id=%s AND success ORDER BY replicate",
        (run_id,),
    )
    replicates = []
    for r in cur.fetchall():
        data = loads(r["results"]) or {}
        prog = data.get(program_key) or {}
        da = (prog.get("domain") or {}).get(dimension, {}).get(group_a)
        db = (prog.get("domain") or {}).get(dimension, {}).get(group_b)
        if da is None or db is None:
            continue
        replicates.append({"a": da[metric], "b": db[metric]})

    if not replicates:
        interval = {"estimate": round(est_a - est_b, 6), "low": None, "high": None, "replicates": 0}
        reason = interval_status or "no_successful_replicates_for_groups"
    else:
        interval = difference_interval(est_a, est_b, replicates)
        interval["level"] = 0.95
        interval["method"] = "percentile_household_bootstrap_recalibrated"
        reason = None

    return {
        "run_id": run_id,
        "program_key": program_key,
        "dimension": dimension,
        "group_a": group_a,
        "group_b": group_b,
        "metric": metric,
        "values": {group_a: est_a, group_b: est_b},
        "difference": interval,
        "interval_suppressed_reason": reason,
        "interpretation": (
            "A within-program subgroup contrast. It still describes only the groups the diary frame "
            "represents; check /coverage-gaps before reading it as a population difference."
        ),
        "cross_program_comparison": {
            "supported": False,
            "see": f"/v1/runs/{run_id}/compare",
            "reason": "coverage gaps differ by program, so program-to-program differences are not comparable",
        },
    }


def cross_program_comparison(cur, run_id: str, program_a: str, program_b: str) -> dict[str, Any]:
    """Always refuses: kept as an explicit endpoint so the refusal is auditable."""
    cur.execute(
        "SELECT program_key FROM run_program_estimates WHERE run_id=%s AND program_key IN (%s,%s)",
        (run_id, program_a, program_b),
    )
    found = {r["program_key"] for r in cur.fetchall()}
    return {
        "run_id": run_id,
        "requested": [program_a, program_b],
        "programs_found": sorted(found),
        "supported": False,
        "difference_estimate": None,
        "interval": None,
        "reason": (
            "This service does not compare the performance of two programs. Each program's reach and "
            "mean minutes rest on a different, incompletely represented population; the differences "
            "between coverage gaps would be read as program performance, which is invalid."
        ),
        "what_is_supported": [
            "each program's weighted reach and mean minutes with a household-bootstrap interval",
            "a within-program subgroup contrast (see /programs/{program_key}/differences)",
            "the list of under-covered groups for the run",
        ],
    }
