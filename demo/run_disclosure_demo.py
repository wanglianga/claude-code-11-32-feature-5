"""Demonstrate the disclosure-control workflow for external audience tables.

Runs against a live service (seeded fictional data) and shows, with real
numbers:

1.  a single-table subtraction attack (row total minus the other cells
    reconstructs a starred cell) and how primary + secondary suppression
    closes it, verified by exact integer ranges;
2.  a cross-table joint restoration attack (two groupings solved together)
    and a release that is *blocked* because the catalog already pins the
    cell;
3.  why overlapping audiences are union-counted, never summed;
4.  the computation budget blocking a release instead of a partial check;
5.  catalog-revision serialisation of concurrent releases;
6.  withdrawal not erasing the past.

Artefacts (plans with final feasible ranges, blocking bases, and the clean
public export files) are written to ``demo/out/disclosure/``.

Usage::

    python demo/run_disclosure_demo.py --base-url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import httpx

ADMIN_KEY = "dev-admin-key"
ANALYST_KEY = "dev-analyst-key"
OUT_DIR = Path(__file__).resolve().parent / "out" / "disclosure"

POLICY = {"min_unweighted_households": 3, "min_protection_width": 3, "enumeration_node_budget": 500_000}


def show(title: str, value) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


class Demo:
    def __init__(self, base_url: str):
        self.client = httpx.Client(base_url=base_url, headers={"X-API-Key": ANALYST_KEY}, timeout=120)
        self.director = httpx.Client(base_url=base_url, headers={"X-API-Key": ADMIN_KEY}, timeout=120)
        self.artifacts: dict[str, Path] = {}

    def post(self, path, body, director=False):
        client = self.director if director else self.client
        r = client.post(path, json=body)
        if r.status_code >= 400:
            return None, r.status_code, r.json()
        return r.json(), r.status_code, None

    def get(self, path, params=None):
        r = self.client.get(path, params=params)
        if r.status_code >= 400:
            return None
        return r.json()

    def save(self, name: str, document) -> Path:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / name
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
        self.artifacts[name] = path
        return path

    # -- workflow helpers ----------------------------------------------------
    def publish_run(self, seeded, cutoff) -> str:
        body = {
            "note": "disclosure-demo",
            "inputs": {
                "recruitment_batch_seq": seeded["recruitment_batch_seq"],
                "target_batch_seq": seeded["target_batch_seq"],
                "program_batch_seq": seeded["program_batch_seq"],
                "diary_seq_cutoff": cutoff,
            },
            "config": {},
        }
        created, _, err = self.post("/v1/runs", body)
        assert created, err
        run_id = created["run"]["run_id"]
        executed, _, err = self.post(f"/v1/runs/{run_id}/execute", {"max_steps": 50})
        assert executed and executed["run"]["status"] == "published", err
        return run_id

    def request(self, run_id, tables, policy=None):
        body = {"run_id": run_id, "tables": tables, "policy": policy or POLICY}
        created, _, err = self.post("/v1/disclosure/requests", body)
        assert created, err
        return created

    def approve(self, request_id, note):
        approved, status, err = self.post(f"/v1/disclosure/requests/{request_id}/approve", {"note": note}, director=True)
        assert approved, (status, err)
        return approved

    def export(self, request_id):
        exported, status, err = self.post(f"/v1/disclosure/requests/{request_id}/export", {})
        return exported, status, err


def household_counts(demo: Demo, run_id: str, program: str):
    """Internal analyst view: unweighted households per (age_band, region) reached by a program."""
    sample = demo.get(f"/v1/runs/{run_id}/programs/{program}/sample", {"limit": 2000})
    counts: dict[tuple, set] = {}
    for row in sample["rows"]:
        if not row["eligible"] or not row["reached"]:
            continue
        key = (row["age_band"], row["region"])
        counts.setdefault(key, set()).add(row["household_id"])
    return {key: len(h) for key, h in counts.items()}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    demo = Demo(args.base_url)

    health = demo.get("/health")
    if not health:
        print("service is not reachable")
        return 1
    frames = demo.get("/v1/recruitment/batches")
    targets = demo.get("/v1/targets/batches")
    programs = demo.get("/v1/programs/batches")
    cutoff = demo.get("/v1/diary/cutoff")
    seeded = {
        "recruitment_batch_seq": frames[0]["batch_seq"],
        "target_batch_seq": targets[0]["batch_seq"],
        "program_batch_seq": programs[0]["batch_seq"],
    }
    run_a = demo.publish_run(seeded, cutoff["available_max_submission_seq"])
    show("published snapshot A (lifecycle, concurrency, cross-table, withdrawal)", {"run_id": run_a})

    # ------------------------------------------------------------------
    # 0. The naive attacks, computed from the internal sample
    # ------------------------------------------------------------------
    midnight = household_counts(demo, run_a, "midnight_news")
    age_counts = {age: sum(n for (a, _r), n in midnight.items() if a == age) for age in ("18-24", "25-44", "45-64", "65+")}
    total = sum(age_counts.values())
    leaked = total - age_counts["18-24"] - age_counts["45-64"] - age_counts["65+"]
    show(
        "ATTACK 1 (single-table subtraction): if a naive release starred only the small cell",
        {
            "naive_release_would_publish": {
                "18-24": age_counts["18-24"],
                "25-44": "S (starred)",
                "45-64": age_counts["45-64"],
                "65+": age_counts["65+"],
                "total": total,
            },
            "attacker_computes": f"{total} - {age_counts['18-24']} - {age_counts['45-64']} - {age_counts['65+']} = {leaked}",
            "leaked_cell": "25-44 listeners of midnight_news = exactly one household",
        },
    )

    afternoon = household_counts(demo, run_a, "afternoon_talk")
    n2544 = sum(n for (a, r), n in afternoon.items() if a == "25-44" and r == "north")
    s2544 = sum(n for (a, r), n in afternoon.items() if a == "25-44" and r == "south")
    show(
        "ATTACK 2 (cross-table joint restoration): a fine-grained public table leaks sums",
        {
            "public_table_cells": {"(25-44, north)": n2544, "(25-44, south)": s2544},
            "attacker_computes": f"{n2544} + {s2544} = {n2544 + s2544}",
            "consequence": "a later table may not hide the 25-44 afternoon cell: it is already derivable",
        },
    )

    # ------------------------------------------------------------------
    # 1. Real flow: age x midnight_news request -> plan -> approve -> export
    # ------------------------------------------------------------------
    req1 = demo.request(run_a, [
        {"name": "age_midnight", "dimensions": ["age_band"], "programs": ["midnight_news"]},
    ])
    plan1 = req1["plan"]
    show(
        "system plan for age x midnight_news (primary + secondary suppression)",
        {
            "decisions": plan1["decisions"],
            "protected_ranges": plan1["protected_ranges"],
            "verification": plan1["verification"],
            "readability": plan1["readability"],
        },
    )
    demo.save("01_plan_age_midnight.json", req1)
    demo.approve(req1["request_id"], "one body cell and the total are hidden; the table stays readable")
    exported1, status1, err1 = demo.export(req1["request_id"])
    assert exported1, (status1, err1)
    doc1 = exported1["document"]
    show(
        "public export (suppressed cells carry no values)",
        {"tables": doc1["tables"], "policy_statement": doc1["policy_statement"]},
    )
    demo.save("01_public_export_age_midnight.json", doc1)

    # ------------------------------------------------------------------
    # 2. Concurrent requests are serialised by the catalog revision
    #    (run while the catalog is small: two requests approved against
    #    the same revision cannot both export).
    # ------------------------------------------------------------------
    req_a = demo.request(run_a, [{"name": "sex_youth", "dimensions": ["sex"], "programs": ["youth_drive"]}])
    req_b = demo.request(run_a, [{"name": "region_midnight", "dimensions": ["region"], "programs": ["midnight_news"]}])
    assert req_a["status"] == req_b["status"] == "awaiting_approval", (req_a["status"], req_b["status"])
    demo.approve(req_a["request_id"], "ok")
    demo.approve(req_b["request_id"], "ok")
    exported_a, _, _ = demo.export(req_a["request_id"])
    stale_body, stale_status, _ = demo.export(req_b["request_id"])
    show(
        "second export is refused: it was approved against the pre-export catalog",
        {"first_export_revision": exported_a["catalog_revision"], "second_export_http_status": stale_status, "error": stale_body},
    )
    rechecked_b, _, _ = demo.post(f"/v1/disclosure/requests/{req_b['request_id']}/recheck", {})
    show(
        "recheck re-verifies against the catalog that now includes the first table",
        {"status": rechecked_b["status"], "catalog_tables": rechecked_b["plan"]["catalog_tables"]},
    )
    demo.save("02_plan_region_midnight_rechecked.json", rechecked_b)
    demo.approve(req_b["request_id"], "re-approved after recheck")
    exported_b, _, _ = demo.export(req_b["request_id"])
    demo.save("02_public_export_region_midnight.json", exported_b["document"])

    # ------------------------------------------------------------------
    # 3. Cross-table: publish the fine table, then a stricter one is blocked
    # ------------------------------------------------------------------
    req2 = demo.request(run_a, [
        {"name": "region_age_afternoon", "dimensions": ["region", "age_band"], "programs": ["afternoon_talk"]},
    ])
    assert req2["status"] == "awaiting_approval"
    demo.approve(req2["request_id"], "every cell is either empty or backed by enough households")
    exported2, status2, err2 = demo.export(req2["request_id"])
    assert exported2, (status2, err2)
    demo.save("03_public_export_region_age_afternoon.json", exported2["document"])

    strict = {"min_unweighted_households": 8, "min_protection_width": 3, "enumeration_node_budget": 500_000}
    req3 = demo.request(run_a, [
        {"name": "age_afternoon_strict", "dimensions": ["age_band"], "programs": ["afternoon_talk"]},
    ], policy=strict)
    show(
        "stricter follow-up is blocked: the catalog already pins the cell",
        {
            "status": req3["status"],
            "reason": req3["blocking"]["reason"],
            "pinned_by_catalog": req3["blocking"].get("pinned_by_catalog"),
        },
    )
    demo.save("04_blocking_age_afternoon.json", req3)

    # ------------------------------------------------------------------
    # 3. Overlapping audiences: union totals are not sums
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 4. Overlapping audiences on a fresh snapshot: union totals are not sums
    # ------------------------------------------------------------------
    run_b = demo.publish_run(seeded, cutoff["available_max_submission_seq"])
    show("published snapshot B (overlap and budget chapters)", {"run_id": run_b})
    req4 = demo.request(run_b, [
        {"name": "region_two_programs", "dimensions": ["region"], "programs": ["youth_drive", "afternoon_talk"]},
    ])
    plan4 = req4["plan"]
    cells4 = {c["cell_id"]: c for c in plan4["tables"][0]["cells"] if c["decision"] == "published"}
    col_youth = cells4["d||p|youth_drive"]["unweighted_households"]
    col_after = cells4["d||p|afternoon_talk"]["unweighted_households"]
    grand = cells4["d||p|afternoon_talk+youth_drive"]["unweighted_households"]
    show(
        "overlapping audiences: the grand union is not the sum of the program columns",
        {
            "youth_drive_households": col_youth,
            "afternoon_talk_households": col_after,
            "sum_would_double_count": col_youth + col_after,
            "published_union_total": grand,
            "overlapping_households": col_youth + col_after - grand,
        },
    )
    demo.approve(req4["request_id"], "union totals verified against overlap")
    exported4, _, _ = demo.export(req4["request_id"])
    demo.save("05_public_export_region_two_programs.json", exported4["document"])

    # ------------------------------------------------------------------
    # 4. Computation budget: a partial check is never released
    # ------------------------------------------------------------------
    tiny_budget = {"min_unweighted_households": 3, "min_protection_width": 3, "enumeration_node_budget": 1_000}
    req5 = demo.request(run_b, [
        {"name": "too_big", "dimensions": ["region", "age_band"], "programs": ["youth_drive", "afternoon_talk"]},
    ], policy=tiny_budget)
    show(
        "computation budget exceeded -> blocked, no heuristic shortcut",
        {"status": req5["status"], "reason": req5["blocking"]["reason"], "nodes": req5["blocking"].get("enumeration_nodes")},
    )
    demo.save("06_blocking_budget.json", req5)

    # ------------------------------------------------------------------
    # 6. Withdrawal does not erase the past
    # ------------------------------------------------------------------
    catalog = demo.get("/v1/disclosure/catalog", {"run_id": run_a})
    midnight_table = next(t for t in catalog if t["name"] == "age_midnight")
    withdrawn, _, _ = demo.post(f"/v1/disclosure/catalog/{midnight_table['table_id']}/withdraw", {"note": "retracted by the director"}, director=True)
    req7 = demo.request(run_a, [
        {"name": "age_midnight_again", "dimensions": ["age_band"], "programs": ["midnight_news"]},
    ])
    plan7 = req7["plan"]
    show(
        "withdrawn table still constrains: the same cells stay hidden",
        {
            "withdrawal": withdrawn,
            "catalog_tables": plan7["catalog_tables"],
            "decisions_after_withdraw": plan7["decisions"],
        },
    )
    demo.save("07_plan_age_midnight_after_withdraw.json", req7)

    # ------------------------------------------------------------------
    # 7. Cleanliness gate over every public export produced above
    # ------------------------------------------------------------------
    forbidden = ("person_id", "household_id", "final_weight", "base_weight", "inclusion_probability")
    checked = 0
    for name, path in demo.artifacts.items():
        if "public_export" not in name:
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        text = json.dumps(doc)
        assert not any(fragment in text for fragment in forbidden), name
        for table in doc["tables"]:
            for cell in table["cells"]:
                if cell["status"] == "suppressed":
                    assert set(cell) == {"cell_id", "kind", "dims", "programs", "status", "marker"}, (name, cell)
                else:
                    assert "unweighted_households" in cell and "weighted_persons" in cell
        checked += 1
    show("export cleanliness gate", {"public_files_checked": checked, "result": "no hidden values, no identifiers"})
    show("artefacts written", {name: str(path) for name, path in demo.artifacts.items()})

    print("\nALL DISCLOSURE STEPS COMPLETED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
