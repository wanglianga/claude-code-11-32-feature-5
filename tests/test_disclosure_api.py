"""Integration tests for disclosure-checked publication of external tables.

These tests use their own schema: the disclosure catalog is global state and
other test files register conflict resolutions / late diaries that would
change the sample underneath a fixed scenario.
"""
from __future__ import annotations

import os
import uuid

import pytest

from app.config import Settings
from app.db import Database
from app.main import create_app

TEST_DB_URL = os.environ.get(
    "AUDIENCE_TEST_DATABASE_URL", "postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev"
)

ADMIN = {"X-API-Key": "test-admin-key"}
ANALYST = {"X-API-Key": "test-analyst-key"}

POLICY = {"min_unweighted_households": 3, "min_protection_width": 3, "enumeration_node_budget": 500_000}


@pytest.fixture(scope="module")
def disclosure_env():
    from zoneinfo import ZoneInfo

    from app import ingest
    from demo import fixtures

    schema = f"test_disc_{uuid.uuid4().hex[:10]}"
    settings = Settings(
        database_url=TEST_DB_URL,
        admin_api_key="test-admin-key",
        analyst_api_key="test-analyst-key",
        default_schema=schema,
        service_timezone="Asia/Shanghai",
    )
    db = Database(settings)
    db.open()
    tz = ZoneInfo("Asia/Shanghai")
    rec = ingest.create_recruitment_batch(
        db, fixtures.recruitment_batch(include_unknown_probability=False), "pytest"
    )
    tgt = ingest.create_target_batch(db, fixtures.target_batch(), "pytest")
    prg = ingest.create_program_batch(db, fixtures.program_batch(), "pytest")
    for sub in fixtures.diary_submissions():
        ingest.create_diary_submission(db, sub, "pytest", tz)
    ingest.create_diary_submission(db, fixtures.S701_RESUBMISSION, "pytest", tz)
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(submission_seq),0) AS m FROM diary_submissions")
        latest = cur.fetchone()["m"]
    seeded = {
        "recruitment_batch_seq": rec["batch_seq"],
        "target_batch_seq": tgt["batch_seq"],
        "program_batch_seq": prg["batch_seq"],
        "latest_cutoff": latest,
    }
    from fastapi.testclient import TestClient

    app = create_app(settings=settings, db=db)
    with TestClient(app) as client:
        client.headers.update({"X-API-Key": "test-analyst-key"})
        yield client, seeded
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname=%s", (schema,))
        tables = [r["tablename"] for r in cur.fetchall()]
        if tables:
            cur.execute("DROP TABLE IF EXISTS " + ", ".join(f'"{t}"' for t in tables) + " CASCADE")
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        conn.commit()
    db.close()


@pytest.fixture(scope="module")
def client(disclosure_env):
    return disclosure_env[0]


@pytest.fixture(scope="module")
def seeded(disclosure_env):
    return disclosure_env[1]


def _published_run(client, seeded, note="disclosure-test"):
    body = {
        "note": note,
        "inputs": {
            "recruitment_batch_seq": seeded["recruitment_batch_seq"],
            "target_batch_seq": seeded["target_batch_seq"],
            "program_batch_seq": seeded["program_batch_seq"],
            "diary_seq_cutoff": seeded["latest_cutoff"],
        },
        "config": {},
    }
    created = client.post("/v1/runs", json=body)
    assert created.status_code == 201, created.text
    run_id = created.json()["run"]["run_id"]
    executed = client.post(f"/v1/runs/{run_id}/execute", json={"max_steps": 50})
    assert executed.status_code == 200, executed.text
    assert executed.json()["run"]["status"] == "published", executed.text
    return run_id


def _request(client, run_id, tables, policy=None):
    body = {"run_id": run_id, "tables": tables, "policy": policy or POLICY}
    resp = client.post("/v1/disclosure/requests", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _approve(client, request_id, note="looks readable"):
    resp = client.post(f"/v1/disclosure/requests/{request_id}/approve", json={"note": note}, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _export(client, request_id):
    return client.post(f"/v1/disclosure/requests/{request_id}/export")


def _walk(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------
def test_full_lifecycle_and_clean_export(client, seeded):
    run_id = _published_run(client, seeded)
    detail = _request(client, run_id, [
        {"name": "age_youth", "dimensions": ["age_band"], "programs": ["youth_drive"]},
        {"name": "region_two_programs", "dimensions": ["region"], "programs": ["youth_drive", "afternoon_talk"]},
    ])
    assert detail["status"] == "awaiting_approval"
    plan = detail["plan"]
    assert plan["verification"]["exhaustive"] is True
    assert plan["verification"]["safety_from_heuristics"] is False
    # The plan marks suppressed cells with feasible ranges, never true values.
    suppressed = [c for t in plan["tables"] for c in t["cells"] if c["decision"] != "published"]
    assert suppressed, "expected some suppressed cells in the demo dataset"
    for cell in suppressed:
        assert "unweighted_households" not in cell
        assert "weighted_persons" not in cell
        rng = cell["feasible_range"]
        assert rng["hi"] is None or rng["hi"] - rng["lo"] >= POLICY["min_protection_width"]

    approved = _approve(client, detail["request_id"])
    assert approved["status"] == "approved"
    exported = _export(client, detail["request_id"])
    assert exported.status_code == 200, exported.text
    document = exported.json()["document"]

    # The export mirrors the approved plan exactly: hidden cells carry no values.
    decisions = {}
    for table in plan["tables"]:
        for cell in table["cells"]:
            decisions[(table["name"], cell["cell_id"])] = cell["decision"]
    seen = 0
    for table in document["tables"]:
        for cell in table["cells"]:
            seen += 1
            if decisions[(table["name"], cell["cell_id"])] == "published":
                assert cell["status"] == "published"
                assert "unweighted_households" in cell and "weighted_persons" in cell
            else:
                assert cell["status"] == "suppressed"
                assert set(cell) == {"cell_id", "kind", "dims", "programs", "status", "marker"}
    assert seen == len(decisions)

    # No identifiers or per-person weight fields anywhere in the public file.
    forbidden = ("person_id", "household_id", "final_weight", "base_weight", "inclusion_probability")
    for key, _value in _walk(document):
        assert not any(fragment in key for fragment in forbidden), key

    # The catalog now lists both tables at the same revision.
    catalog = client.get("/v1/disclosure/catalog", params={"run_id": run_id}).json()
    assert {t["name"] for t in catalog} == {"age_youth", "region_two_programs"}
    assert all(t["status"] == "published" for t in catalog)

    # The stored export is retrievable.
    fetched = client.get(f"/v1/disclosure/requests/{detail['request_id']}/export").json()
    assert fetched["document"] == document


def test_approval_requires_director_key(client, seeded):
    run_id = _published_run(client, seeded)
    detail = _request(client, run_id, [
        {"name": "age_youth", "dimensions": ["age_band"], "programs": ["youth_drive"]},
    ])
    resp = client.post(f"/v1/disclosure/requests/{detail['request_id']}/approve", json={}, headers=ANALYST)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "director_key_required"
    # Reject also requires the director.
    resp = client.post(f"/v1/disclosure/requests/{detail['request_id']}/reject", json={}, headers=ANALYST)
    assert resp.status_code == 403


def test_request_requires_published_run(client, seeded):
    body = {
        "note": "not-executed",
        "inputs": {
            "recruitment_batch_seq": seeded["recruitment_batch_seq"],
            "target_batch_seq": seeded["target_batch_seq"],
            "program_batch_seq": seeded["program_batch_seq"],
            "diary_seq_cutoff": seeded["latest_cutoff"],
        },
        "config": {},
    }
    run_id = client.post("/v1/runs", json=body).json()["run"]["run_id"]
    resp = client.post("/v1/disclosure/requests", json={
        "run_id": run_id,
        "tables": [{"name": "t", "dimensions": ["region"], "programs": ["youth_drive"]}],
        "policy": POLICY,
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "run_not_published"


def test_unknown_program_is_rejected(client, seeded):
    run_id = _published_run(client, seeded)
    resp = client.post("/v1/disclosure/requests", json={
        "run_id": run_id,
        "tables": [{"name": "t", "dimensions": ["region"], "programs": ["no_such_program"]}],
        "policy": POLICY,
    })
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "program_not_in_run"


# ---------------------------------------------------------------------------
# Blocked releases keep their basis
# ---------------------------------------------------------------------------
def test_catalog_pinned_cell_blocks_and_saves_basis(client, seeded):
    run_id = _published_run(client, seeded)
    # Every (region, age) afternoon cell has 0 or >= 3 households, so this
    # table is published in full.
    first = _request(client, run_id, [
        {"name": "region_age_afternoon", "dimensions": ["region", "age_band"], "programs": ["afternoon_talk"]},
    ])
    assert first["status"] == "awaiting_approval"
    _approve(client, first["request_id"])
    assert _export(client, first["request_id"]).status_code == 200

    # A stricter follow-up policy would hide the 25-44 age cell (7 < 8), but
    # the catalog already lets anyone add the two region cells back to it.
    second = _request(client, run_id, [
        {"name": "age_afternoon", "dimensions": ["age_band"], "programs": ["afternoon_talk"]},
    ], policy={"min_unweighted_households": 8, "min_protection_width": 3, "enumeration_node_budget": 500_000})
    assert second["status"] == "blocked"
    blocking = second["blocking"]
    assert blocking["reason"] == "protection_width_unreachable"
    pinned = blocking["pinned_by_catalog"]
    assert pinned["cell_id"] == "d|age_band=25-44|p|afternoon_talk"
    assert len(pinned["certificate"]) == 2
    # The basis is persisted and retrievable.
    fetched = client.get(f"/v1/disclosure/requests/{second['request_id']}").json()
    assert fetched["blocking"]["reason"] == "protection_width_unreachable"
    assert fetched["blocking"]["catalog_revision"] >= 1


def test_enumeration_budget_exceeded_blocks(client, seeded):
    run_id = _published_run(client, seeded)
    detail = _request(client, run_id, [
        {"name": "big", "dimensions": ["region", "age_band"], "programs": ["youth_drive", "afternoon_talk"]},
    ], policy={"min_unweighted_households": 3, "min_protection_width": 3, "enumeration_node_budget": 1_000})
    assert detail["status"] == "blocked"
    assert detail["blocking"]["reason"] == "enumeration_budget_exceeded"
    assert detail["blocking"]["enumeration_nodes"] > 1_000


# ---------------------------------------------------------------------------
# Catalog revision: concurrent requests cannot bypass each other
# ---------------------------------------------------------------------------
def test_concurrent_requests_are_serialised_by_catalog_revision(client, seeded):
    run_id = _published_run(client, seeded)
    first = _request(client, run_id, [
        {"name": "region_two_programs", "dimensions": ["region"], "programs": ["youth_drive", "afternoon_talk"]},
    ])
    second = _request(client, run_id, [
        {"name": "age_afternoon", "dimensions": ["age_band"], "programs": ["afternoon_talk"]},
    ])
    assert first["status"] == second["status"] == "awaiting_approval"
    assert first["catalog_revision_checked"] == second["catalog_revision_checked"]
    _approve(client, first["request_id"])
    _approve(client, second["request_id"])

    assert _export(client, first["request_id"]).status_code == 200
    # The second request was approved against the pre-export catalog: it must
    # not bypass the first table.
    stale = _export(client, second["request_id"])
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "catalog_revision_changed"
    assert client.get(f"/v1/disclosure/requests/{second['request_id']}").json()["status"] == "stale"

    rechecked = client.post(f"/v1/disclosure/requests/{second['request_id']}/recheck").json()
    assert rechecked["status"] == "awaiting_approval"
    catalog_names = {t["name"] for t in rechecked["plan"]["catalog_tables"]}
    assert "region_two_programs" in catalog_names
    _approve(client, second["request_id"])
    assert _export(client, second["request_id"]).status_code == 200


def test_recheck_keeps_spec_pinned(client, seeded):
    run_id = _published_run(client, seeded)
    detail = _request(client, run_id, [
        {"name": "age_youth", "dimensions": ["age_band"], "programs": ["youth_drive"]},
    ])
    rechecked = client.post(f"/v1/disclosure/requests/{detail['request_id']}/recheck").json()
    # A recheck re-verifies the same tables; a changed grouping or sample is a
    # new request, never a mutation of this one.
    assert rechecked["spec_hash"] == detail["spec_hash"]
    assert rechecked["spec"] == detail["spec"]
    other = _request(client, run_id, [
        {"name": "sex_youth", "dimensions": ["sex"], "programs": ["youth_drive"]},
    ])
    assert other["spec_hash"] != detail["spec_hash"]


# ---------------------------------------------------------------------------
# Withdrawal does not erase the past
# ---------------------------------------------------------------------------
def test_withdrawn_table_still_constrains(client, seeded):
    run_id = _published_run(client, seeded)
    detail = _request(client, run_id, [
        {"name": "age_youth", "dimensions": ["age_band"], "programs": ["youth_drive"]},
    ])
    _approve(client, detail["request_id"])
    assert _export(client, detail["request_id"]).status_code == 200
    table_id = client.get("/v1/disclosure/catalog", params={"run_id": run_id}).json()[0]["table_id"]

    resp = client.post(f"/v1/disclosure/catalog/{table_id}/withdraw", json={"note": "retracted"}, headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json()["still_constrains_future_checks"] is True

    followup = _request(client, run_id, [
        {"name": "age_youth_again", "dimensions": ["age_band"], "programs": ["youth_drive"]},
    ])
    plan = followup["plan"]
    catalog_entries = {t["name"]: t["status"] for t in plan["catalog_tables"]}
    assert catalog_entries.get("age_youth") == "withdrawn"
    # Cells the withdrawn table suppressed stay suppressed in the new plan.
    catalog_decisions = [c for t in plan["tables"] for c in t["cells"] if c["decision"] == "suppressed_catalog"]
    assert catalog_decisions, "withdrawn table's hidden cells must keep constraining"


# ---------------------------------------------------------------------------
# Content rules
# ---------------------------------------------------------------------------
def test_overlap_union_total_is_not_a_sum(client, seeded):
    run_id = _published_run(client, seeded)
    detail = _request(client, run_id, [
        {"name": "region_two_programs", "dimensions": ["region"], "programs": ["youth_drive", "afternoon_talk"]},
    ])
    plan = detail["plan"]
    table = plan["tables"][0]
    body = {}
    row_totals = {}
    col_totals = {}
    grand = None
    for cell in table["cells"]:
        if cell["decision"] != "published":
            continue
        key = tuple(sorted(cell["dims"].items()))
        if cell["kind"] == "body":
            body.setdefault(key, {})[cell["programs"][0]] = cell["unweighted_households"]
        elif cell["kind"] == "row_total":
            row_totals[key] = cell["unweighted_households"]
        elif cell["kind"] == "col_total":
            col_totals[cell["programs"][0]] = cell["unweighted_households"]
        elif cell["kind"] == "grand_total":
            grand = cell["unweighted_households"]
    for group, total in row_totals.items():
        summed = sum(body.get(group, {}).values())
        assert total <= summed, "a union total must never exceed the sum of its cells"
    # Households HF023 and H04 listen to both programs, so the grand union is
    # strictly smaller than the sum of the per-program column totals.
    assert grand is not None and len(col_totals) == 2
    assert grand < sum(col_totals.values())


def test_percentages_only_come_from_public_cells(client, seeded):
    run_id = _published_run(client, seeded)
    detail = _request(client, run_id, [
        {"name": "region_two_programs", "dimensions": ["region"], "programs": ["youth_drive", "afternoon_talk"]},
    ])
    _approve(client, detail["request_id"])
    document = _export(client, detail["request_id"]).json()["document"]
    for table in document["tables"]:
        published_ids = {c["cell_id"] for c in table["cells"] if c["status"] == "published"}
        for cell in table["cells"]:
            if "pct_of_program" in cell:
                program = cell["programs"][0]
                assert f"d||p|{program}" in published_ids
                assert 0 <= cell["pct_of_program"] <= 1


def test_export_of_unapproved_request_is_refused(client, seeded):
    run_id = _published_run(client, seeded)
    detail = _request(client, run_id, [
        {"name": "age_youth", "dimensions": ["age_band"], "programs": ["youth_drive"]},
    ])
    resp = _export(client, detail["request_id"])
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "request_not_approved"
