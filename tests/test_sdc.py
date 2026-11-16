"""Pure-engine tests for the disclosure-control check (no database)."""
from __future__ import annotations

from itertools import product

import pytest

from app.analytics import sdc
from app.analytics.sdc import (
    BudgetExceeded,
    Policy,
    SnapshotPerson,
    build_atoms,
    canonical_cell_id,
    cell_values,
    determination_certificate,
    exact_hidden_ranges,
    make_cell,
    run_check,
    table_cell_defs,
)

POLICY = Policy(min_unweighted_households=3, min_protection_width=3, enumeration_node_budget=100_000)


def person(pid, hid, region="north", age="18-24", sex="F", weight=1.0, reached=frozenset({"P1"})):
    return SnapshotPerson(pid, hid, {"region": region, "age_band": age, "sex": sex}, weight, reached)


def group(prefix, n, **kwargs):
    return [person(f"{prefix}{i}", f"h{prefix}{i}", **kwargs) for i in range(n)]


def cells_for(tables, categories):
    cells = {}
    for spec in tables:
        for cell in table_cell_defs(spec, categories):
            cells.setdefault(cell.cell_id, cell)
    return cells


# ---------------------------------------------------------------------------
# Atoms and overlapping audiences
# ---------------------------------------------------------------------------
def test_atoms_partition_households_and_overlap_is_not_summed():
    # One household watches both programs; another watches only P1.
    persons = [
        person("p1", "h1", reached=frozenset({"P1", "P2"})),
        person("p2", "h1", reached=frozenset({"P1"}), age="45-64"),
        person("p3", "h2", reached=frozenset({"P1"})),
    ]
    spec = {"name": "t", "dimensions": ["region"], "programs": ["P1", "P2"], "include_totals": False}
    categories = {"region": ["north"], "age_band": ["18-24", "45-64"], "sex": ["F"]}
    cells = cells_for([spec], categories)
    atoms = build_atoms(persons, list(cells.values()))
    p1 = canonical_cell_id({"region": "north"}, ("P1",))
    p2 = canonical_cell_id({"region": "north"}, ("P2",))
    # h1 is in P1 and P2; h2 only in P1.
    patterns = {frozenset(pat): size for pat, size in atoms}
    assert patterns[frozenset({p1, p2})] == 1
    assert patterns[frozenset({p1})] == 1
    # The union total is 2 households, not 2 + 1 = 3 (overlap must not be summed).
    union_cell = make_cell({"region": "north"}, ("P1", "P2"), "row_total")
    atoms_with_union = build_atoms(persons, list(cells.values()) + [union_cell])
    values = {cid: cell_values(persons, atoms_with_union, cell)[0] for cid, cell in cells.items()}
    union_count = cell_values(persons, atoms_with_union, union_cell)[0]
    assert values[p1] == 2 and values[p2] == 1 and union_count == 2
    assert union_count != values[p1] + values[p2]


def test_primary_suppression_threshold_and_zero_cells():
    persons = group("a", 5, region="north") + group("b", 2, region="south") + group("c", 3, region="east")
    spec = {"name": "t", "dimensions": ["region"], "programs": ["P1"], "include_totals": False}
    categories = {"region": ["east", "north", "south", "west"], "age_band": ["18-24"], "sex": ["F"]}
    cells = cells_for([spec], categories)
    result = run_check(
        persons=persons, tables=[spec], categories=categories, cells=cells,
        catalog_counts={}, policy=POLICY,
    )
    assert result["status"] == "ok"
    decisions = result["decisions"]
    assert decisions[canonical_cell_id({"region": "south"}, ("P1",))] == "suppressed_primary"
    assert decisions[canonical_cell_id({"region": "north"}, ("P1",))] == "published"
    # A zero-count cell has no household to protect: it is published as 0.
    assert decisions[canonical_cell_id({"region": "west"}, ("P1",))] == "published"


# ---------------------------------------------------------------------------
# Single-table subtraction
# ---------------------------------------------------------------------------
def test_single_table_subtraction_is_closed_by_secondary_suppression():
    persons = group("n", 10, region="north") + group("s", 2, region="south")
    spec = {"name": "t", "dimensions": ["region"], "programs": ["P1"], "include_totals": True}
    categories = {"region": ["north", "south"], "age_band": ["18-24"], "sex": ["F"]}
    cells = cells_for([spec], categories)
    south = canonical_cell_id({"region": "south"}, ("P1",))
    total = canonical_cell_id({}, ("P1",))

    # The naive "star the small cell" release leaks it exactly: total - north.
    atoms = build_atoms(persons, list(cells.values()))
    naive_equations = {canonical_cell_id({"region": "north"}, ("P1",)): 10, total: 12}
    ranges, _ = exact_hidden_ranges(atoms, naive_equations, {south}, 10_000)
    assert ranges[south] == (2, 2)  # exactly reconstructed -> not acceptable

    result = run_check(
        persons=persons, tables=[spec], categories=categories, cells=cells,
        catalog_counts={}, policy=POLICY,
    )
    assert result["status"] == "ok"
    assert result["decisions"][south] == "suppressed_primary"
    # The total (or an equivalent cell) had to be hidden as secondary suppression.
    assert result["decisions"][total] == "suppressed_secondary"
    lo, hi = result["protected_ranges"][south]["lo"], result["protected_ranges"][south]["hi"]
    assert hi is None or hi - lo >= POLICY.min_protection_width


# ---------------------------------------------------------------------------
# Cross-table joint restoration
# ---------------------------------------------------------------------------
def _cross_table_persons():
    return (
        group("ny", 5, region="north", age="18-24")
        + group("no", 5, region="north", age="45-64")
        + group("sy", 1, region="south", age="18-24")
        + group("so", 5, region="south", age="45-64")
    )


def test_cross_table_joint_restoration_is_prevented():
    persons = _cross_table_persons()
    categories = {"region": ["north", "south"], "age_band": ["18-24", "45-64"], "sex": ["F"]}
    spec_a = {"name": "A", "dimensions": ["region"], "programs": ["P1"], "include_totals": True}
    spec_b = {"name": "B", "dimensions": ["region", "age_band"], "programs": ["P1"], "include_totals": False}
    cells = cells_for([spec_a, spec_b], categories)
    atoms = build_atoms(persons, list(cells.values()))
    # Table A is already public.
    catalog = {}
    for cell in table_cell_defs(spec_a, categories):
        catalog[cell.cell_id] = sum(s for pat, s in atoms if cell.cell_id in pat)

    sy = canonical_cell_id({"region": "south", "age_band": "18-24"}, ("P1",))
    so = canonical_cell_id({"region": "south", "age_band": "45-64"}, ("P1",))
    # Naively publishing B next to A exposes (south, 18-24) = 6 - 5 = 1.
    naive = dict(catalog)
    naive[canonical_cell_id({"region": "north", "age_band": "18-24"}, ("P1",))] = 5
    naive[canonical_cell_id({"region": "north", "age_band": "45-64"}, ("P1",))] = 5
    naive[so] = 5
    ranges, _ = exact_hidden_ranges(atoms, naive, {sy}, 10_000)
    assert ranges[sy] == (1, 1)

    result = run_check(
        persons=persons, tables=[spec_b], categories=categories, cells=cells,
        catalog_counts=catalog, policy=POLICY,
    )
    assert result["status"] == "ok"
    assert result["decisions"][sy] == "suppressed_primary"
    # The joint check hides the complementary cell so the difference no longer works.
    assert result["decisions"][so] == "suppressed_secondary"
    rng = result["protected_ranges"][sy]
    assert rng["hi"] is None or rng["hi"] - rng["lo"] >= POLICY.min_protection_width


def test_catalog_pinned_cell_blocks_the_release():
    persons = _cross_table_persons()
    categories = {"region": ["north", "south"], "age_band": ["18-24", "45-64"], "sex": ["F"]}
    spec = {"name": "C", "dimensions": ["region"], "programs": ["P1"], "include_totals": False}
    cells = cells_for([spec], categories)
    # The catalog already determines (south, 18-24) on its own:
    # young total minus (north, young) = 1.
    catalog_counts = {
        canonical_cell_id({"age_band": "18-24"}, ("P1",)): 6,
        canonical_cell_id({"age_band": "45-64"}, ("P1",)): 10,
        canonical_cell_id({"region": "north", "age_band": "18-24"}, ("P1",)): 5,
        canonical_cell_id({"region": "north", "age_band": "45-64"}, ("P1",)): 5,
        canonical_cell_id({"region": "south", "age_band": "45-64"}, ("P1",)): 5,
    }
    for cid, cell in list(cells.items()):
        cells.setdefault(cid, cell)
    for cid in catalog_counts:
        dims = dict(part.split("=") for part in cid.split("|")[1].split(";") if part)
        progs = tuple(cid.split("|")[3].split("+"))
        cells.setdefault(cid, make_cell(dims, progs, "body"))
    catalog_hidden = {canonical_cell_id({"region": "south", "age_band": "18-24"}, ("P1",)): "suppressed_primary"}
    for cid in catalog_hidden:
        dims = dict(part.split("=") for part in cid.split("|")[1].split(";") if part)
        progs = tuple(cid.split("|")[3].split("+"))
        cells.setdefault(cid, make_cell(dims, progs, "body"))
    result = run_check(
        persons=persons, tables=[spec], categories=categories, cells=cells,
        catalog_counts=catalog_counts, catalog_hidden=catalog_hidden, policy=POLICY,
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "protection_width_unreachable"
    assert result["pinned_by_catalog"]["cell_id"] == canonical_cell_id({"region": "south", "age_band": "18-24"}, ("P1",))


def test_catalog_hidden_cell_forces_pending_cell_to_stay_hidden():
    persons = _cross_table_persons()
    categories = {"region": ["north", "south"], "age_band": ["18-24", "45-64"], "sex": ["F"]}
    spec = {"name": "C", "dimensions": ["region", "age_band"], "programs": ["P1"], "include_totals": False}
    cells = cells_for([spec], categories)
    sy = canonical_cell_id({"region": "south", "age_band": "18-24"}, ("P1",))
    result = run_check(
        persons=persons, tables=[spec], categories=categories, cells=cells,
        catalog_counts={}, catalog_hidden={sy: "suppressed_primary"}, policy=POLICY,
    )
    assert result["status"] == "ok"
    assert result["decisions"][sy] == "suppressed_catalog"
    rng = result["protected_ranges"][sy]
    assert rng["hi"] is None or rng["hi"] - rng["lo"] >= POLICY.min_protection_width


def test_catalog_mismatch_is_detected():
    persons = group("n", 5, region="north")
    categories = {"region": ["north"], "age_band": ["18-24"], "sex": ["F"]}
    spec = {"name": "t", "dimensions": ["region"], "programs": ["P1"], "include_totals": False}
    cells = cells_for([spec], categories)
    result = run_check(
        persons=persons, tables=[spec], categories=categories, cells=cells,
        catalog_counts={canonical_cell_id({"region": "north"}, ("P1",)): 99}, policy=POLICY,
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "catalog_snapshot_mismatch"


# ---------------------------------------------------------------------------
# Exact enumeration
# ---------------------------------------------------------------------------
def test_exact_enumeration_matches_bruteforce():
    # Atoms: x0 (in A), x1 (in A and B), x2 (in B). Equations A=3, B=2.
    cells = [make_cell({}, ("A",), "body"), make_cell({}, ("B",), "body"), make_cell({}, ("H",), "body")]
    a_id, b_id, h_id = (c.cell_id for c in cells)
    atoms = [
        (frozenset({a_id, h_id}), 1),  # x0, also in hidden cell H
        (frozenset({a_id, b_id}), 1),  # x1
        (frozenset({b_id}), 1),        # x2
    ]
    equations = {a_id: 3, b_id: 2}
    ranges, nodes = exact_hidden_ranges(atoms, equations, {h_id}, 100_000)
    # Brute force over x0..x2 in [0..3].
    feasible = [
        (x0, x1, x2)
        for x0, x1, x2 in product(range(4), repeat=3)
        if x0 + x1 == 3 and x1 + x2 == 2
    ]
    h_values = [x0 for x0, _, _ in feasible]
    assert ranges[h_id] == (min(h_values), max(h_values))
    assert nodes > 0


def test_free_atoms_make_hidden_range_unbounded():
    persons = group("n", 5, region="north") + group("s", 2, region="south", reached=frozenset({"P2"}))
    spec = {"name": "t", "dimensions": ["region"], "programs": ["P1", "P2"], "include_totals": False}
    categories = {"region": ["north", "south"], "age_band": ["18-24"], "sex": ["F"]}
    cells = cells_for([spec], categories)
    south_p2 = canonical_cell_id({"region": "south"}, ("P2",))
    result = run_check(
        persons=persons, tables=[spec], categories=categories, cells=cells,
        catalog_counts={}, policy=POLICY,
    )
    assert result["status"] == "ok"
    assert result["decisions"][south_p2] == "suppressed_primary"
    rng = result["protected_ranges"][south_p2]
    assert rng["lo"] == 0 and rng["hi"] is None  # unbounded above -> safe


def test_budget_exceeded_blocks_release():
    persons = group("n", 10, region="north") + group("s", 2, region="south")
    spec = {"name": "t", "dimensions": ["region"], "programs": ["P1"], "include_totals": True}
    categories = {"region": ["north", "south"], "age_band": ["18-24"], "sex": ["F"]}
    cells = cells_for([spec], categories)
    tight = Policy(min_unweighted_households=3, min_protection_width=3, enumeration_node_budget=3)
    result = run_check(
        persons=persons, tables=[spec], categories=categories, cells=cells,
        catalog_counts={}, policy=tight,
    )
    assert result["status"] == "blocked"
    assert result["reason"] == "enumeration_budget_exceeded"
    assert result["enumeration_nodes"] > tight.enumeration_node_budget


def test_inconsistent_public_cells_raise():
    atoms = [(frozenset({"A"}), 1)]
    with pytest.raises(Exception):
        exact_hidden_ranges(atoms, {"A": 0, "B": 5}, set(), 1_000)


def test_certificate_names_the_determining_cells():
    # north = 10, total = 12 -> south is exactly total - north.
    cells = [
        make_cell({"region": "north"}, ("P1",), "body"),
        make_cell({"region": "south"}, ("P1",), "body"),
        make_cell({}, ("P1",), "col_total"),
    ]
    north, south, total = (c.cell_id for c in cells)
    atoms = [(frozenset({north, total}), 10), (frozenset({south, total}), 2)]
    rows = sdc._equation_matrix(atoms, [north, total])
    target = [1 if south in pat else 0 for pat, _ in atoms]
    certificate = determination_certificate(rows, target)
    named = { [north, total][i] for i in certificate }
    assert named == {north, total}
    # A cell not determined by the system has no certificate.
    free_target = [1, 0]
    assert determination_certificate([[1, 1]], free_target) is None
