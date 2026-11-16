"""Bounded raking (iterative proportional fitting) with explicit failure states.

The estimator never fabricates a sample: if a cell has no sample, if clamping
makes a margin unreachable, or if iteration stops without meeting tolerance,
the result is a failure carrying residuals and diagnostics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence


@dataclass(frozen=True)
class RakingConfig:
    tolerance: float = 1e-4
    max_iterations: int = 100
    lower_factor: float = 0.3
    upper_factor: float = 4.0
    dimensions: tuple[str, ...] = ("age_band", "region", "sex")


@dataclass
class RakingResult:
    success: bool
    status: str
    weights: list[float]
    bounds: list[dict[str, float]]
    mean_base_weight: float
    cells: list[dict] = field(default_factory=list)
    rounds: list[dict] = field(default_factory=list)
    max_rel_residual: float | None = None
    clamped_persons: int = 0
    empty_cells: list[dict] = field(default_factory=list)
    unreachable_cells: list[dict] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "status": self.status,
            "success": self.success,
            "max_rel_residual": self.max_rel_residual,
            "rounds": len(self.rounds),
            "clamped_persons": self.clamped_persons,
            "bounds": {
                "lower_factor": None,
                "upper_factor": None,
                "persons": self.bounds,
            },
            "mean_base_weight": self.mean_base_weight,
            "empty_cells": self.empty_cells,
            "unreachable_cells": self.unreachable_cells,
            "details": self.details,
        }


def _cell_key(dimension: str, category: str) -> str:
    return f"{dimension}={category}"


def rake(
    persons: Sequence[dict],
    base_weights: Sequence[float],
    margins: dict[str, dict[str, float]],
    config: RakingConfig,
) -> RakingResult:
    """Calibrate weights to known marginal totals with per-person bounds.

    Each person's calibrated weight is clamped to
    ``[lower_factor * base_i, upper_factor * base_i]``. Bounds are relative to
    the design weight, so the population scale implied by 1/p is preserved.
    ``persons`` is aligned with ``base_weights`` by position, which means
    bootstrap copies of the same identifier are independent rows.
    """
    n = len(persons)
    if n != len(base_weights):
        raise ValueError("persons and base_weights must align")
    if n == 0:
        return RakingResult(
            success=False,
            status="sample_empty",
            weights=[],
            bounds=[],
            mean_base_weight=0.0,
            details={"reason": "no eligible persons"},
        )

    base = [float(w) for w in base_weights]
    mean_base = sum(base) / n
    lower = [config.lower_factor * w for w in base]
    upper = [config.upper_factor * w for w in base]
    bounds = [
        {"person_index": i, "base_weight": base[i], "lower": lower[i], "upper": upper[i]}
        for i in range(n)
    ]
    weights = [min(max(base[i], lower[i]), upper[i]) for i in range(n)]

    empty_cells: list[dict] = []
    unreachable_cells: list[dict] = []
    cell_members: dict[tuple[str, str], list[int]] = {}

    for dim in config.dimensions:
        if dim not in margins:
            raise ValueError(f"margin missing for dimension {dim}")
        for cat, target in margins[dim].items():
            members = [i for i, p in enumerate(persons) if p.get(dim) == cat]
            cell_members[(dim, cat)] = members
            key = _cell_key(dim, cat)
            if target <= 0:
                continue
            if not members:
                empty_cells.append(
                    {
                        "cell": key,
                        "dimension": dim,
                        "category": cat,
                        "target": target,
                        "sample_persons": 0,
                        "reason": "no_sample_in_cell",
                    }
                )
                continue
            min_possible = sum(lower[i] for i in members)
            max_possible = sum(upper[i] for i in members)
            eps = max(1e-9, abs(target) * 1e-9)
            if target < min_possible - eps or target > max_possible + eps:
                unreachable_cells.append(
                    {
                        "cell": key,
                        "dimension": dim,
                        "category": cat,
                        "target": target,
                        "sample_persons": len(members),
                        "min_possible": round(min_possible, 6),
                        "max_possible": round(max_possible, 6),
                        "reason": "target_outside_bound_feasible_range",
                    }
                )

    cells_meta = []
    for dim in config.dimensions:
        for cat, target in margins[dim].items():
            members = cell_members[(dim, cat)]
            cells_meta.append(
                {
                    "dimension": dim,
                    "category": cat,
                    "target": float(target),
                    "members": members,
                    "initial_sum": sum(base[i] for i in members),
                }
            )

    def residual_report() -> tuple[list[dict], float]:
        rows = []
        worst = 0.0
        for meta in cells_meta:
            total = sum(weights[i] for i in meta["members"])
            target = meta["target"]
            residual = total - target
            rel = abs(residual) / target if target else 0.0
            worst = max(worst, rel)
            rows.append(
                {
                    "dimension": meta["dimension"],
                    "category": meta["category"],
                    "target": round(target, 6),
                    "initial_sum": round(meta["initial_sum"], 6),
                    "final_sum": round(total, 6),
                    "residual": round(residual, 6),
                    "relative_residual": rel,
                    "sample_persons": len(meta["members"]),
                }
            )
        return rows, worst

    if empty_cells or unreachable_cells:
        rows, worst = residual_report()
        return RakingResult(
            success=False,
            status="empty_cell" if empty_cells else "bounds_unreachable",
            weights=weights,
            bounds=bounds,
            mean_base_weight=mean_base,
            cells=rows,
            rounds=[],
            max_rel_residual=worst,
            empty_cells=empty_cells,
            unreachable_cells=unreachable_cells,
            details={
                "reason": "margin cell has no sample" if empty_cells else "clamp bounds exclude target",
            },
        )

    rounds: list[dict] = []
    converged = False
    for round_no in range(1, config.max_iterations + 1):
        clamped_this_round: set[int] = set()
        for dim in config.dimensions:
            for cat, target in margins[dim].items():
                if target <= 0:
                    continue
                members = cell_members[(dim, cat)]
                total = sum(weights[i] for i in members)
                if total <= 0:
                    rows, worst = residual_report()
                    return RakingResult(
                        success=False,
                        status="empty_cell",
                        weights=weights,
                        bounds=bounds,
                        mean_base_weight=mean_base,
                        cells=rows,
                        rounds=rounds,
                        max_rel_residual=worst,
                        empty_cells=[
                            {
                                "cell": _cell_key(dim, cat),
                                "dimension": dim,
                                "category": cat,
                                "target": target,
                                "sample_persons": len(members),
                                "reason": "zero_weight_after_adjustment",
                            }
                        ],
                    )
                factor = target / total
                for i in members:
                    weights[i] *= factor
        # Clamp after every full pass over all margins, then re-check.
        for i, w in enumerate(weights):
            if w < lower[i]:
                weights[i] = lower[i]
                clamped_this_round.add(i)
            elif w > upper[i]:
                weights[i] = upper[i]
                clamped_this_round.add(i)
        rows, worst = residual_report()
        rounds.append(
            {
                "round_no": round_no,
                "max_rel_residual": worst,
                "clamped_persons": len(clamped_this_round),
            }
        )
        if worst <= config.tolerance:
            converged = True
            break

    rows, worst = residual_report()
    clamped_final = {
        i for i, w in enumerate(weights) if abs(w - lower[i]) <= 1e-12 or abs(w - upper[i]) <= 1e-12
    }
    for row in rows:
        meta = next(
            m
            for m in cells_meta
            if m["dimension"] == row["dimension"] and m["category"] == row["category"]
        )
        row["clamped_persons"] = sum(1 for i in meta["members"] if i in clamped_final)
    return RakingResult(
        success=converged,
        status="converged" if converged else "non_convergence",
        weights=weights,
        bounds=bounds,
        mean_base_weight=mean_base,
        cells=rows,
        rounds=rounds,
        max_rel_residual=worst,
        clamped_persons=len(clamped_final),
        empty_cells=[],
        unreachable_cells=[],
        details={} if converged else {"reason": "tolerance not met within max_iterations"},
    )


def effective_strata(
    strata: Iterable[str], merges: Sequence[Sequence[str]]
) -> tuple[dict[str, str], list[dict]]:
    mapping: dict[str, str] = {}
    applied: list[dict] = []
    for group_no, group in enumerate(merges):
        members = [str(s) for s in group]
        if len(set(members)) != len(members):
            raise ValueError("stratum merge contains duplicates")
        label = "+".join(sorted(members))
        for member in members:
            if member in mapping:
                raise ValueError(f"stratum {member!r} appears in more than one merge group")
            mapping[member] = label
        applied.append({"merged_stratum": label, "members": sorted(members)})
    for s in strata:
        mapping.setdefault(s, s)
    return mapping, applied
