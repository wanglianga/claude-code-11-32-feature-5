"""Statistical disclosure control for external audience tables.

Policy (explicitly *not* differential privacy, and no random noise is added
to any published number):

* A table cell may be published only when its unweighted household count is
  zero or at least ``min_unweighted_households``. Cells below the threshold
  are **primarily suppressed**.
* **Secondary suppression** then hides additional cells or totals until
  every suppressed cell keeps a feasible integer count range of at least
  ``min_protection_width`` under every count constraint implied by the
  cells that remain public. Marking a small cell with an asterisk is not
  enough: a row total minus the other cells reconstructs it, and two
  tables with slightly different groupings can be solved jointly.
* The pending tables are checked **jointly with every table already
  published on the same data snapshot** (the public catalog, including
  withdrawn tables — withdrawal does not make the audience forget).
  The base units are mutually exclusive household characteristic
  combinations (*atoms*); each table cell contributes an integer count
  constraint through household membership. A household reached by two
  programs belongs to both cells, so union totals are computed from
  membership and are never assembled by adding overlapping cells.
* Feasible ranges are computed by **exact enumeration** of the
  non-negative integer solutions of the constraint system. If the
  enumeration exceeds its node budget the release is blocked; a heuristic
  is only ever used to *choose* a secondary-suppression candidate, never
  to declare the result safe.
* Weighted person counts and percentages of suppressed cells are withheld.
  Percentages are published only when both numerator and denominator are
  public. Derived columns that cannot be expressed in this constraint
  system (means, listener ratios, per-person weights, ...) are not
  published at all.

Everything in this module is pure Python over in-memory structures; the
service layer supplies the snapshot persons and the catalog cells.
"""
from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from itertools import product
from typing import Any, Iterable

DIMENSIONS = ("region", "age_band", "sex")

# Hard caps that keep the exact enumeration tractable. Exceeding any of
# them blocks the release instead of degrading to an approximate check.
MAX_ATOMS = 512
MAX_CANDIDATE_TRIALS = 32

# When choosing secondary suppressions, derived totals are hidden before
# body cells: body cells carry the granular information the analyst wants.
_KIND_RANK = {"grand_total": 0, "col_total": 1, "row_total": 2, "body": 3}

POLICY_STATEMENT = (
    "Threshold rule on unweighted household counts plus secondary suppression, "
    "verified by exact enumeration of the integer feasible set of the count "
    "constraints. No random noise is added and no differential-privacy "
    "guarantee is claimed."
)


class BudgetExceeded(Exception):
    """The exact enumeration used more nodes than the configured budget."""

    def __init__(self, nodes: int, budget: int):
        self.nodes = nodes
        self.budget = budget
        super().__init__(f"enumeration exceeded node budget {budget} (used {nodes})")


class InconsistentConstraints(Exception):
    """The public cells cannot be produced by any non-negative integer vector."""


# ---------------------------------------------------------------------------
# Cells
# ---------------------------------------------------------------------------
def canonical_cell_id(dims: dict[str, str], programs: Iterable[str]) -> str:
    """Stable identifier for a logical cell (dimension values + program set).

    A multi-program id denotes the *union* of the programs (a total), never
    a sum of the single-program cells.
    """
    d = ";".join(f"{k}={v}" for k, v in sorted(dims.items()))
    p = "+".join(sorted(programs))
    return f"d|{d}|p|{p}"


@dataclass(frozen=True)
class CellDef:
    cell_id: str
    dims: tuple  # tuple[tuple[str, str], ...], sorted
    programs: tuple  # tuple[str, ...], sorted; len > 1 => union total
    kind: str  # body | row_total | col_total | grand_total

    @property
    def dims_dict(self) -> dict[str, str]:
        return dict(self.dims)


def make_cell(dims: dict[str, str], programs: Iterable[str], kind: str) -> CellDef:
    programs = tuple(sorted(programs))
    return CellDef(canonical_cell_id(dims, programs), tuple(sorted(dims.items())), programs, kind)


def table_cell_defs(spec: dict[str, Any], categories: dict[str, list[str]]) -> list[CellDef]:
    """All display cells of one table in a stable, export-friendly order."""
    dims = list(spec["dimensions"])
    programs = sorted(spec["programs"])
    include_totals = bool(spec.get("include_totals", True))
    value_lists = [categories[d] for d in dims]
    cells: list[CellDef] = []
    for combo in product(*value_lists):
        group = dict(zip(dims, combo))
        for program in programs:
            cells.append(make_cell(group, (program,), "body"))
        if include_totals and len(programs) > 1:
            cells.append(make_cell(group, programs, "row_total"))
    if include_totals:
        for program in programs:
            cells.append(make_cell({}, (program,), "col_total"))
        if len(programs) > 1:
            cells.append(make_cell({}, programs, "grand_total"))
    return cells


# ---------------------------------------------------------------------------
# Snapshot persons and household atoms
# ---------------------------------------------------------------------------
@dataclass
class SnapshotPerson:
    person_id: str
    household_id: str
    attrs: dict[str, str]
    weight: float
    reached: frozenset  # program keys this person listened to (>= threshold)


def person_matches(person: SnapshotPerson, dims: tuple, programs: tuple) -> bool:
    for key, value in dims:
        if person.attrs.get(key) != value:
            return False
    return any(p in person.reached for p in programs)


def build_atoms(persons: list[SnapshotPerson], cells: list[CellDef]) -> list[tuple[frozenset, int]]:
    """Partition households into mutually exclusive membership-pattern atoms.

    Returns a sorted list of ``(pattern, size)`` where ``pattern`` is the
    frozenset of cell ids the households belong to and ``size`` is the
    number of households with exactly that pattern. Households matching no
    cell are dropped: they constrain nothing.
    """
    by_household: dict[str, list[SnapshotPerson]] = {}
    for person in persons:
        by_household.setdefault(person.household_id, []).append(person)
    patterns: dict[frozenset, int] = {}
    for members in by_household.values():
        pattern = frozenset(
            cell.cell_id
            for cell in cells
            if any(person_matches(p, cell.dims, cell.programs) for p in members)
        )
        if pattern:
            patterns[pattern] = patterns.get(pattern, 0) + 1
    return sorted(patterns.items(), key=lambda kv: (len(kv[0]), sorted(kv[0])))


def cell_values(persons: list[SnapshotPerson], atoms: list[tuple[frozenset, int]], cell: CellDef) -> tuple[int, float]:
    """True (unweighted household count, weighted persons) of a cell."""
    count = sum(size for pattern, size in atoms if cell.cell_id in pattern)
    weighted = sum(p.weight for p in persons if person_matches(p, cell.dims, cell.programs))
    return count, round(weighted, 3)


# ---------------------------------------------------------------------------
# Exact integer feasibility
# ---------------------------------------------------------------------------
def exact_hidden_ranges(
    atoms: list[tuple[frozenset, int]],
    equations: dict[str, int],
    hidden: Iterable[str],
    budget: int,
) -> tuple[dict[str, tuple[int, int | None]], int]:
    """Exact feasible [lo, hi] for every hidden cell's household count.

    ``equations`` maps a public cell id to its published count; the unknowns
    are the atom sizes (non-negative integers). ``hi`` is ``None`` when the
    cell contains an atom no public equation constrains (unbounded above).

    The system is decomposed into independent components (atoms and
    equations that share no variables); each is enumerated exactly and the
    per-component ranges are summed, which is exact because components are
    independent. Raises :class:`BudgetExceeded` when the cumulative node
    budget is exceeded and :class:`InconsistentConstraints` when the public
    cells admit no solution. Returns ``(ranges, nodes_used)``.
    """
    hidden_list = sorted(hidden)
    cell_atoms: dict[str, list[int]] = {}
    for idx, (pattern, _size) in enumerate(atoms):
        for cid in pattern:
            cell_atoms.setdefault(cid, []).append(idx)

    eq_cells: list[str] = []
    eq_atoms: list[list[int]] = []
    rhs: list[int] = []
    for cid in sorted(equations):
        members = cell_atoms.get(cid, [])
        value = int(equations[cid])
        if not members:
            if value != 0:
                raise InconsistentConstraints(f"public cell {cid} has no households but value {value}")
            continue
        eq_cells.append(cid)
        eq_atoms.append(members)
        rhs.append(value)

    hidden_members = {h: cell_atoms.get(h, []) for h in hidden_list}

    if not eq_cells:
        # No public equation: every atom is free, so every hidden cell with
        # at least one atom ranges from 0 to unbounded.
        return ({h: ((0, None) if hidden_members[h] else (0, 0)) for h in hidden_list}, 0)

    atom_eqs: list[list[int]] = [[] for _ in atoms]
    for e, members in enumerate(eq_atoms):
        for a in members:
            atom_eqs[a].append(e)
    constrained = {a for a in range(len(atoms)) if atom_eqs[a]}
    free = set(range(len(atoms))) - constrained
    unbounded = {h: any(i in free for i in hidden_members[h]) for h in hidden_list}

    # Connected components of the atom<->equation bipartite graph.
    comp_of_atom = [-1] * len(atoms)
    comp_of_eq = [-1] * len(eq_cells)
    n_components = 0
    for seed in sorted(constrained):
        if comp_of_atom[seed] != -1:
            continue
        stack = [seed]
        comp_of_atom[seed] = n_components
        while stack:
            atom = stack.pop()
            for e in atom_eqs[atom]:
                if comp_of_eq[e] != n_components:
                    comp_of_eq[e] = n_components
                    for other in eq_atoms[e]:
                        if comp_of_atom[other] != n_components:
                            comp_of_atom[other] = n_components
                            stack.append(other)
        n_components += 1

    hidden_index = {h: k for k, h in enumerate(hidden_list)}
    lo_total = [0] * len(hidden_list)
    hi_total: list[int | None] = [0] * len(hidden_list)
    nodes = 0
    for comp in range(n_components):
        comp_atoms = sorted(a for a in range(len(atoms)) if comp_of_atom[a] == comp)
        comp_eqs = sorted(e for e in range(len(eq_cells)) if comp_of_eq[e] == comp)
        local_eq_atoms = [[comp_atoms.index(a) for a in eq_atoms[e]] for e in comp_eqs]
        local_rhs = [rhs[e] for e in comp_eqs]
        local_atom_hidden: list[list[int]] = [[] for _ in comp_atoms]
        for pos, a in enumerate(comp_atoms):
            for h in hidden_list:
                if a in cell_atoms.get(h, []):
                    local_atom_hidden[pos].append(hidden_index[h])
        lo_c, hi_c, used = _enumerate_component(local_eq_atoms, local_rhs, local_atom_hidden, len(hidden_list), budget - nodes)
        nodes += used
        for k in range(len(hidden_list)):
            lo_total[k] += lo_c[k]
            hi_total[k] = None if hi_total[k] is None or hi_c[k] is None else hi_total[k] + hi_c[k]

    out: dict[str, tuple[int, int | None]] = {}
    for k, h in enumerate(hidden_list):
        if unbounded[h]:
            out[h] = (lo_total[k], None)
        else:
            out[h] = (lo_total[k], hi_total[k])
    return out, nodes


def _enumerate_component(
    eq_atoms: list[list[int]],
    rhs: list[int],
    atom_hidden: list[list[int]],
    n_hidden: int,
    budget: int,
) -> tuple[list[int], list[int | None], int]:
    """Enumerate one independent component.

    Returns per-hidden-cell (lo, hi) contributions (length ``n_hidden``;
    hidden cells without atoms in this component contribute 0) and the
    nodes used.
    """
    atom_eqs: list[list[int]] = [[] for _ in range(len(atom_hidden))]
    for e, members in enumerate(eq_atoms):
        for a in members:
            atom_eqs[a].append(e)
    n = len(atom_hidden)
    order = sorted(range(n), key=lambda a: -len(atom_eqs[a]))
    pos_of = [0] * n
    for pos, a in enumerate(order):
        pos_of[a] = pos
    # suffix_atoms[e][pos]: atoms of equation e still unassigned after pos.
    suffix_atoms = [
        [[a for a in eq_atoms[e] if pos_of[a] > pos] for pos in range(n)]
        for e in range(len(eq_atoms))
    ]
    remaining = list(rhs)
    unassigned = [len(members) for members in eq_atoms]
    lo: list[int | None] = [None] * n_hidden
    hi: list[int | None] = [None] * n_hidden
    hsum = [0] * n_hidden
    nodes = 0
    solutions = 0

    def dfs(pos: int) -> None:
        nonlocal nodes, solutions
        if pos == n:
            solutions += 1
            for k in range(n_hidden):
                value = hsum[k]
                lo[k] = value if lo[k] is None else min(lo[k], value)
                hi[k] = value if hi[k] is None else max(hi[k], value)
            return
        atom = order[pos]
        eqs = atom_eqs[atom]
        max_value = min(remaining[e] for e in eqs)
        own_hidden = atom_hidden[atom]
        for value in range(max_value + 1):
            nodes += 1
            if nodes > budget:
                raise BudgetExceeded(nodes, budget)
            ok = True
            for e in eqs:
                remaining[e] -= value
                unassigned[e] -= 1
                if remaining[e] < 0 or (remaining[e] > 0 and unassigned[e] == 0):
                    ok = False
            if ok:
                # Achievable-remaining bound: every equation's remaining sum
                # must be reachable from its unassigned atoms, each capped by
                # the smallest remaining of the equations it sits in.
                for e in range(len(eq_atoms)):
                    need = remaining[e]
                    if need == 0:
                        continue
                    achievable = 0
                    for a in suffix_atoms[e][pos]:
                        cap = remaining[atom_eqs[a][0]]
                        for e2 in atom_eqs[a][1:]:
                            if remaining[e2] < cap:
                                cap = remaining[e2]
                        achievable += cap
                        if achievable >= need:
                            break
                    if achievable < need:
                        ok = False
                        break
            if ok:
                for k in own_hidden:
                    hsum[k] += value
                dfs(pos + 1)
                for k in own_hidden:
                    hsum[k] -= value
            for e in eqs:
                remaining[e] += value
                unassigned[e] += 1

    if n:
        dfs(0)
    if solutions == 0 and eq_atoms:
        raise InconsistentConstraints("public cells admit no non-negative integer solution")
    lo_out = [v if v is not None else 0 for v in lo]
    hi_out: list[int | None] = [v if v is not None else 0 for v in hi]
    return lo_out, hi_out, nodes


def determination_certificate(eq_rows: list[list[int]], target: list[int]) -> list[int] | None:
    """Row indices proving ``target`` is an exact rational combination of rows.

    Used to name the public cells that jointly pin a suppressed cell to a
    single value (width 0). Returns ``None`` when the target is not in the
    row span (the cell is not exactly determined).
    """
    m = len(eq_rows)
    n = len(target)
    if m == 0:
        return None
    mat = [
        [Fraction(eq_rows[j][i]) for j in range(m)] + [Fraction(target[i])]
        for i in range(n)
    ]
    pivot_cols: list[int] = []
    row = 0
    for col in range(m):
        pivot = next((r for r in range(row, n) if mat[r][col] != 0), None)
        if pivot is None:
            continue
        mat[row], mat[pivot] = mat[pivot], mat[row]
        factor = mat[row][col]
        mat[row] = [x / factor for x in mat[row]]
        for r in range(n):
            if r != row and mat[r][col] != 0:
                f = mat[r][col]
                mat[r] = [a - f * b for a, b in zip(mat[r], mat[row])]
        pivot_cols.append(col)
        row += 1
        if row == n:
            break
    for r in range(row, n):
        if all(mat[r][c] == 0 for c in range(m)) and mat[r][m] != 0:
            return None
    lam = [Fraction(0)] * m
    for k, col in enumerate(pivot_cols):
        lam[col] = mat[k][m]
    return [j for j in range(m) if lam[j] != 0]


# ---------------------------------------------------------------------------
# Secondary suppression
# ---------------------------------------------------------------------------
@dataclass
class Policy:
    min_unweighted_households: int = 3
    min_protection_width: int = 3
    enumeration_node_budget: int = 200_000


def _width(cell_range: tuple[int, int | None]) -> float:
    lo, hi = cell_range
    if hi is None:
        return float("inf")
    return float(hi - lo)


def _equation_matrix(atoms: list[tuple[frozenset, int]], public: list[str]) -> list[list[int]]:
    return [[1 if cid in pattern else 0 for pattern, _ in atoms] for cid in public]


def _component_candidates(
    atoms: list[tuple[frozenset, int]],
    public: list[str],
    seed_cell: str,
) -> list[str]:
    """Public cells in the same constraint component as ``seed_cell``.

    Atoms and public cells form a bipartite graph; only cells in the
    violator's component can influence its feasible range.
    """
    cell_atoms: dict[str, set[int]] = {}
    for idx, (pattern, _size) in enumerate(atoms):
        for cid in pattern:
            cell_atoms.setdefault(cid, set()).add(idx)
    public_set = set(public)
    seed_atoms = set(cell_atoms.get(seed_cell, set()))
    seen_cells: set[str] = set()
    seen_atoms: set[int] = set()
    frontier_atoms = set(seed_atoms)
    while frontier_atoms:
        seen_atoms |= frontier_atoms
        next_cells = {
            cid
            for cid in public_set
            if cid not in seen_cells and cell_atoms.get(cid, set()) & frontier_atoms
        }
        if not next_cells:
            break
        seen_cells |= next_cells
        frontier_atoms = set().union(*(cell_atoms[c] for c in next_cells)) - seen_atoms
    return sorted(seen_cells)


# ---------------------------------------------------------------------------
# Top-level check
# ---------------------------------------------------------------------------
def run_check(
    *,
    persons: list[SnapshotPerson],
    tables: list[dict[str, Any]],
    categories: dict[str, list[str]],
    cells: dict[str, CellDef],
    catalog_counts: dict[str, int],
    catalog_hidden: dict[str, str] | None = None,
    policy: Policy,
) -> dict[str, Any]:
    """Run the full disclosure check for a set of pending tables.

    ``cells`` holds every logical cell (pending tables and catalog tables);
    ``catalog_counts`` maps already-public cell ids to their published
    household counts; ``catalog_hidden`` maps cell ids an earlier release
    deliberately suppressed to their suppression reason — those cells keep
    their protection in this check, and a pending table may not publish
    them. Returns a plan dict with ``status == "ok"`` or a blocking-basis
    dict with ``status == "blocked"``. True values of suppressed cells are
    never included in the result.
    """
    catalog_hidden = dict(catalog_hidden or {})
    display_cells: list[list[CellDef]] = [table_cell_defs(spec, categories) for spec in tables]
    pending_ids = {cell.cell_id for defs in display_cells for cell in defs}
    all_ids = sorted(pending_ids | set(catalog_counts) | set(catalog_hidden))
    atom_list = build_atoms(persons, [cells[cid] for cid in all_ids])
    if len(atom_list) > MAX_ATOMS:
        return {
            "status": "blocked",
            "reason": "atom_space_too_large",
            "message": (
                f"{len(atom_list)} household atoms exceed the exact-enumeration cap "
                f"of {MAX_ATOMS}; the release is blocked rather than checked approximately"
            ),
            "atoms": len(atom_list),
            "atom_cap": MAX_ATOMS,
        }

    true_counts: dict[str, int] = {}
    for cid in all_ids:
        count = sum(size for pattern, size in atom_list if cid in pattern)
        true_counts[cid] = count

    # Defensive consistency check: catalog values were produced from this
    # same snapshot, so a mismatch means the catalog is corrupt.
    catalog_mismatch = [
        cid
        for cid, published in catalog_counts.items()
        if true_counts[cid] != published
    ]
    if catalog_mismatch:
        return {
            "status": "blocked",
            "reason": "catalog_snapshot_mismatch",
            "message": "catalog cells do not match the snapshot they were published from",
            "mismatched_cells": sorted(catalog_mismatch),
        }

    # Primary suppression. A pending cell that is already public in the
    # catalog cannot be taken back; it stays public with its catalog value.
    # A pending cell the catalog deliberately suppresses stays suppressed.
    hidden: dict[str, str] = {}  # cell_id -> reason
    for cid in sorted(pending_ids):
        if cid in catalog_counts:
            continue
        if cid in catalog_hidden:
            hidden[cid] = "suppressed_catalog"
            continue
        count = true_counts[cid]
        if 0 < count < policy.min_unweighted_households:
            hidden[cid] = "suppressed_primary"
    # Cells hidden by earlier releases are protected cells of this check:
    # the pending tables must not reopen their feasible range below the
    # required width.
    for cid, reason in sorted(catalog_hidden.items()):
        hidden.setdefault(cid, "suppressed_catalog")

    budget = policy.enumeration_node_budget
    total_nodes = 0
    candidates_tried: list[str] = []
    certificates_seen: list[dict[str, Any]] = []
    ranges: dict[str, tuple[int, int | None]] = {}
    nodes = 0

    def remaining_budget() -> int:
        return budget - total_nodes

    def enumerate_exact(equations, hidden_cells):
        """Exact ranges charged against the cumulative computation budget."""
        nonlocal total_nodes
        found, used = exact_hidden_ranges(atom_list, equations, hidden_cells, remaining_budget())
        total_nodes += used
        return found, used

    def public_equations() -> dict[str, int]:
        eqs = dict(catalog_counts)
        for cid in pending_ids:
            if cid not in hidden:
                eqs[cid] = true_counts[cid]
        return eqs

    def pending_public() -> list[str]:
        return [cid for cid in sorted(pending_ids) if cid not in hidden and cid not in catalog_counts]

    def hide(candidate: str) -> None:
        hidden[candidate] = "suppressed_secondary"
        candidates_tried.append(candidate)

    max_rounds = 3 * max(1, len(pending_ids)) + 5
    rounds = 0
    while True:
        rounds += 1
        if rounds > max_rounds:
            return {
                "status": "blocked",
                "reason": "secondary_suppression_not_converged",
                "message": "secondary suppression did not converge within the round cap",
                "hidden": sorted(hidden),
            }
        equations = public_equations()
        try:
            ranges, nodes = enumerate_exact(equations, hidden)
        except BudgetExceeded as exc:
            return _budget_block(exc, budget, atom_list, equations, total_nodes + exc.nodes)
        except InconsistentConstraints as exc:
            return {"status": "blocked", "reason": "constraints_inconsistent", "message": str(exc)}
        violators = [
            cid
            for cid in sorted(hidden)
            if _has_atoms(atom_list, cid) and _width(ranges[cid]) < policy.min_protection_width
        ]
        if not violators:
            break
        # Width-0 violators are exactly determined: destroy their
        # certificates one by one. A certificate made only of catalog cells
        # can never be destroyed -> the release is blocked.
        pinned = [cid for cid in violators if _width(ranges[cid]) == 0]
        if pinned:
            eq_cells = sorted(equations)
            rows = _equation_matrix(atom_list, eq_cells)
            hits: dict[str, int] = {}
            for cid in pinned:
                target = [1 if cid in pattern else 0 for pattern, _ in atom_list]
                certificate = determination_certificate(rows, target)
                if certificate is None:
                    continue
                cert_cells = [eq_cells[i] for i in certificate]
                certificates_seen.append({"violator": cid, "certificate": cert_cells})
                pending_hits = [c for c in cert_cells if c in pending_public()]
                if not pending_hits:
                    return _cannot_protect(
                        hidden, ranges, violators, candidates_tried, atom_list, equations,
                        total_nodes, budget,
                        pinned_by_catalog=cid,
                        certificate=cert_cells,
                    )
                for cell_id in pending_hits:
                    hits[cell_id] = hits.get(cell_id, 0) + 1
            if hits:
                # Hide the pending cell that hits the most certificates
                # (ties: totals before body cells, then fewer atoms).
                choice = min(
                    hits,
                    key=lambda c: (
                        -hits[c],
                        _KIND_RANK.get(cells[c].kind if c in cells else "body", 3),
                        sum(1 for pattern, _ in atom_list if c in pattern),
                        c,
                    ),
                )
                hide(choice)
                continue
        # Partial-width violators (or no certificate available): evaluate
        # every pending public cell in the constraint component exactly and
        # hide the one that reduces the total violation most. Selection is
        # heuristic; only the exact enumerator ever declares safety.
        violator = min(violators, key=lambda c: (_width(ranges[c]), c))
        component = _component_candidates(atom_list, sorted(equations), violator)
        pool = [
            cid
            for cid in component
            if cid in pending_public() and _has_atoms(atom_list, cid)
        ][:MAX_CANDIDATE_TRIALS]
        if not pool:
            return _cannot_protect(
                hidden, ranges, violators, candidates_tried, atom_list, equations,
                total_nodes, budget,
            )
        width_cap = policy.min_protection_width

        def violation_score(cell_ranges: dict[str, tuple[int, int | None]]) -> float:
            return sum(min(_width(cell_ranges[v]), width_cap) for v in violators)

        current_score = violation_score(ranges)
        best: tuple[float, str] | None = None
        for candidate in pool:
            trial_equations = dict(equations)
            trial_equations.pop(candidate, None)
            try:
                trial_ranges, _trial_nodes = enumerate_exact(
                    trial_equations, {**hidden, candidate: "suppressed_secondary"}
                )
            except BudgetExceeded as exc:
                return _budget_block(exc, budget, atom_list, equations, total_nodes + exc.nodes)
            score = violation_score(trial_ranges)
            if best is None or score > best[0] or (
                score == best[0]
                and (
                    _KIND_RANK.get(cells[candidate].kind if candidate in cells else "body", 3),
                    candidate,
                )
                < (
                    _KIND_RANK.get(cells[best[1]].kind if best[1] in cells else "body", 3),
                    best[1],
                )
            ):
                best = (score, candidate)
        if best is None or best[0] <= current_score:
            return _cannot_protect(
                hidden, ranges, violators, candidates_tried, atom_list, equations,
                total_nodes, budget,
            )
        hide(best[1])

    # The loop's last enumeration certified the final hidden set (no
    # violators), so ``ranges``/``nodes`` are the certifying values.
    decisions: dict[str, str] = {}
    for cid in sorted(pending_ids):
        if cid in hidden:
            decisions[cid] = hidden[cid]
        else:
            decisions[cid] = "published"

    protected_ranges = {
        cid: {"lo": lo, "hi": hi, "width": None if hi is None else hi - lo}
        for cid, (lo, hi) in sorted(ranges.items())
        if cid in hidden
    }
    catalog_protected_ranges = {
        cid: rng for cid, rng in protected_ranges.items() if cid in catalog_hidden
    }

    return {
        "status": "ok",
        "decisions": decisions,
        "protected_ranges": protected_ranges,
        "catalog_protected_ranges": catalog_protected_ranges,
        "display_cells": display_cells,
        "atoms": atom_list,
        "secondary_suppression": {
            "cells_hidden": sorted(cid for cid, reason in hidden.items() if reason == "suppressed_secondary"),
            "candidates_tried": candidates_tried,
            "certificates_destroyed": certificates_seen,
        },
        "verification": {
            "method": "exact enumeration of non-negative integer solutions",
            "exhaustive": True,
            "atoms": len(atom_list),
            "equations": len(equations),
            "enumeration_nodes_final": nodes,
            "enumeration_nodes_total": total_nodes,
            "node_budget": budget,
            "safety_from_heuristics": False,
        },
    }


def _has_atoms(atoms: list[tuple[frozenset, int]], cid: str) -> bool:
    return any(cid in pattern for pattern, _ in atoms)


def _budget_block(exc: "BudgetExceeded", budget: int, atoms, equations, total_nodes: int) -> dict[str, Any]:
    return {
        "status": "blocked",
        "reason": "enumeration_budget_exceeded",
        "message": (
            f"exact verification used more than the {budget} allowed enumeration "
            "nodes; the release is blocked instead of trusting a partial search"
        ),
        "enumeration_nodes": total_nodes,
        "node_budget": budget,
        "atoms": len(atoms),
        "equations": len(equations),
    }


def _cannot_protect(
    hidden,
    ranges,
    violators,
    candidates_tried,
    atoms,
    equations,
    total_nodes,
    budget,
    pinned_by_catalog: str | None = None,
    certificate: list[str] | None = None,
) -> dict[str, Any]:
    out = {
        "status": "blocked",
        "reason": "protection_width_unreachable",
        "message": (
            "no remaining pending cell can be hidden to widen the feasible range of "
            "the protected cells; already-published catalog cells pin them down. "
            "The release is blocked."
        ),
        "violators": [
            {"cell_id": cid, "lo": ranges[cid][0], "hi": ranges[cid][1]}
            for cid in violators
        ],
        "candidates_tried": candidates_tried,
        "atoms": len(atoms),
        "equations": len(equations),
        "enumeration_nodes_total": total_nodes,
        "node_budget": budget,
    }
    if pinned_by_catalog is not None:
        out["pinned_by_catalog"] = {
            "cell_id": pinned_by_catalog,
            "certificate": certificate,
            "explanation": (
                "this suppressed cell is an exact linear consequence of cells that are "
                "already public in the catalog; no suppression inside the pending "
                "tables can protect it"
            ),
        }
    return out
