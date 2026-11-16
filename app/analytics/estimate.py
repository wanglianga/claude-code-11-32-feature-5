"""Program reach, per-person mean minutes and household bootstrap confidence intervals."""
from __future__ import annotations

import math
import random
from typing import Any


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def point_estimates(
    rows: list[dict[str, Any]],
    weights: list[float],
    threshold_minutes: float,
) -> dict[str, dict[str, Any]]:
    """Estimate reach, per-capita and per-listener mean minutes per program.

    ``rows`` are aligned with ``weights``; each row carries ``by_program``
    mapping program_key -> minutes listened to that program.
    """
    programs: dict[str, dict[str, Any]] = {}
    for i, row in enumerate(rows):
        w = weights[i]
        for key, minutes in row["by_program"].items():
            bucket = programs.setdefault(
                key,
                {
                    "n": 0,
                    "reached": 0,
                    "reached_weight": 0.0,
                    "minutes_weight": 0.0,
                    "listener_weight": 0.0,
                    "listener_n": 0,
                    "weights": [],
                    "minutes_unweighted": [],
                },
            )
            bucket["n"] += 1
            bucket["weights"].append(w)
            bucket["minutes_unweighted"].append(minutes)
            bucket["minutes_weight"] += w * minutes
            if minutes >= threshold_minutes:
                bucket["reached"] += 1
                bucket["reached_weight"] += w
                bucket["listener_weight"] += w
                bucket["listener_n"] += 1

    out: dict[str, dict[str, Any]] = {}
    total_weight = sum(weights)
    for key, b in programs.items():
        reach = b["reached_weight"]
        per_capita = b["minutes_weight"] / total_weight if total_weight else 0.0
        per_listener = (
            b["minutes_weight"] / b["listener_weight"] if b["listener_weight"] else None
        )
        wstats = _weight_stats(b["weights"])
        out[key] = {
            "reach_persons": round(reach, 3),
            "reach_rate": round(reach / total_weight, 6) if total_weight else 0.0,
            "mean_minutes_per_person": round(per_capita, 3),
            "mean_minutes_per_listener": None if per_listener is None else round(per_listener, 3),
            "unweighted_n": b["n"],
            "listener_unweighted_n": b["listener_n"],
            "weight_min": wstats["min"],
            "weight_max": wstats["max"],
            "weight_sum": wstats["sum"],
            "effective_sample_size": wstats["kish"],
            "reached_weight_share": round(reach / total_weight, 6) if total_weight else 0.0,
        }
    return out


def _weight_stats(weights: list[float]) -> dict[str, float]:
    if not weights:
        return {"min": 0.0, "max": 0.0, "sum": 0.0, "kish": 0.0}
    s = sum(weights)
    sumsq = sum(w * w for w in weights)
    return {
        "min": round(min(weights), 6),
        "max": round(max(weights), 6),
        "sum": round(s, 6),
        "kish": round((s * s) / sumsq, 3) if sumsq else 0.0,
    }


def difference_interval(a: float, b: float, replicates: list[dict[str, float]]) -> dict[str, Any]:
    """Percentile interval for (a - b) using replicate replicate estimates."""
    diffs = [r["a"] - r["b"] for r in replicates]
    if not diffs:
        return {"estimate": round(a - b, 3), "low": None, "high": None, "replicates": 0}
    diffs.sort()
    lo = _percentile(diffs, 0.025)
    hi = _percentile(diffs, 0.975)
    return {"estimate": round(a - b, 3), "low": round(lo, 3), "high": round(hi, 3), "replicates": len(diffs)}


def _percentile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    pos = q * (len(sorted_values) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_values[lo]
    frac = pos - lo
    return sorted_values[lo] * (1 - frac) + sorted_values[hi] * frac


def bootstrap_households(
    households: list[list[int]],
    replicate_count: int,
    seed: int,
) -> list[list[int]]:
    """Draw whole-household bootstrap replicates with fixed-seed RNG.

    Each selected household contributes an independent *copy* of all of its
    member rows. Copies are not deduplicated: identical person identifiers in
    different copies are separate draws.
    """
    rng = random.Random(seed)
    reps: list[list[int]] = []
    if not households:
        return reps
    for _ in range(replicate_count):
        picks: list[int] = []
        for _h in range(len(households)):
            bucket = rng.randrange(len(households))
            picks.extend(households[bucket])
        reps.append(picks)
    return reps


def stratified_household_bootstrap(
    households_by_stratum: dict[str, list[list[int]]],
    replicate_count: int,
    seed: int,
) -> list[list[int]]:
    """Resample whole households independently within each effective stratum.

    For every stratum, ``h`` households are drawn with replacement where ``h``
    is the number of households available in that stratum. Households selected
    more than once appear multiple times in the flat pick list, and their
    members are treated as separate draws (identifiers are NOT deduplicated).

    Plan choice: the sample is stratified, so replicates are drawn with
    replacement *within* each stratum. Each replicate keeps the original
    stratum sizes, which is what makes re-running the calibration meaningful.
    """
    rng = random.Random(seed)
    reps: list[list[int]] = []
    if not households_by_stratum:
        return reps
    ordered_strata = sorted(households_by_stratum)
    for _ in range(replicate_count):
        picks: list[int] = []
        for stratum in ordered_strata:
            bucket = households_by_stratum[stratum]
            for _h in range(len(bucket)):
                chosen = bucket[rng.randrange(len(bucket))]
                picks.extend(chosen)
        reps.append(picks)
    return reps


def percentile_interval(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"low": None, "high": None, "replicates": 0}
    ordered = sorted(values)
    return {
        "low": round(_percentile(ordered, 0.025), 3),
        "high": round(_percentile(ordered, 0.975), 3),
        "replicates": len(ordered),
    }
