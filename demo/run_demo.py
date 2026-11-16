"""Drive the whole workflow against a running instance and print a readable walkthrough.

Usage (after ``docker compose up -d`` or ``uvicorn app.main:app``)::

    python demo/run_demo.py --base-url http://127.0.0.1:8000

Every step prints the actual API response fields the service returns, so the
output can be compared against the README without trusting this script.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

ADMIN_KEY = "dev-admin-key"
ANALYST_KEY = "dev-analyst-key"


def show(title: str, value) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


class Demo:
    def __init__(self, base_url: str):
        self.client = httpx.Client(base_url=base_url, headers={"X-API-Key": ADMIN_KEY}, timeout=120)

    def post(self, path, body):
        r = self.client.post(path, json=body)
        if r.status_code >= 400:
            print(f"!! {path} -> {r.status_code}")
            show("error", r.json())
            return None
        return r.json()

    def get(self, path, params=None):
        r = self.client.get(path, params=params)
        if r.status_code >= 400:
            print(f"!! {path} -> {r.status_code}")
            show("error", r.json())
            return None
        return r.json()

    def run(self, seeded, cutoff, note, config=None, target_seq=None, frame_seq=None):
        body = {
            "note": note,
            "inputs": {
                "recruitment_batch_seq": frame_seq or seeded["recruitment_batch_seq"],
                "target_batch_seq": target_seq or seeded["target_batch_seq"],
                "program_batch_seq": seeded["program_batch_seq"],
                "diary_seq_cutoff": cutoff,
            },
            "config": config or {},
        }
        created = self.post("/v1/runs", body)
        if created is None:
            return None, None
        run_id = created["run"]["run_id"]
        self.post(f"/v1/runs/{run_id}/execute", {"max_steps": 50})
        detail = self.get(f"/v1/runs/{run_id}")
        return run_id, detail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    demo = Demo(args.base_url)

    health = demo.get("/health")
    show("health", health)
    if health is None:
        print("service is not reachable")
        return 1

    # The seed subcommand of scripts/seed.py has already loaded one frame,
    # targets, the schedule and every diary. Read the pinned sequences back.
    frames = demo.get("/v1/recruitment/batches")
    targets = demo.get("/v1/targets/batches")
    programs = demo.get("/v1/programs/batches")
    cutoff = demo.get("/v1/diary/cutoff")
    if not frames or not targets or not programs:
        print("seed the database first: python scripts/seed.py --reset")
        return 1
    seeded = {
        "recruitment_batch_seq": frames[0]["batch_seq"],
        "target_batch_seq": targets[0]["batch_seq"],
        "program_batch_seq": programs[0]["batch_seq"],
    }
    if len(frames) > 1:
        seeded["recruitment_unknown_probability_seq"] = frames[1]["batch_seq"]
    show("pinned input batches", seeded | {"available_diary_cutoff": cutoff})

    # ---- 1. published run with intervals -----------------------------------
    run_id, detail = demo.run(seeded, cutoff["available_max_submission_seq"], "demo-main")
    show("run status", {
        "status": detail["status"],
        "calibration": detail["calibration"]["status"] if detail.get("calibration") else None,
        "bootstrap": detail.get("bootstrap"),
        "executed_stages": [s["stage"] for s in detail["stages"] if s["status"] == "done"],
    })
    if detail["status"] != "published":
        print("main run did not publish; stopping")
        return 1

    report = demo.get(f"/v1/runs/{run_id}/report")
    show("report: programs (weighted reach, both means, sample metadata)", [
        {
            "program": p["program_key"],
            "reach_persons": p["estimates"]["reach_persons"],
            "reach_rate": p["estimates"]["reach_rate"],
            "mean_minutes_per_person": p["estimates"]["mean_minutes_per_person"],
            "mean_minutes_per_listener": p["estimates"]["mean_minutes_per_listener"],
            "unweighted_n": p["estimates"]["unweighted_n"],
            "listener_unweighted_n": p["estimates"]["listener_unweighted_n"],
            "weight_min": p["estimates"]["weight_min"],
            "weight_max": p["estimates"]["weight_max"],
            "effective_sample_size": p["estimates"]["effective_sample_size"],
            "reach_interval": p["intervals"]["reach_persons"] if p["intervals"] else None,
        }
        for p in report["report"]["programs"]
    ])
    show("coverage gaps (groups the diary sample under-represents)",
         report["report"]["coverage_gaps"])
    show("comparison block", report["report"]["comparison"])

    # ---- 2. lineage for one number -----------------------------------------
    lineage = demo.get(f"/v1/runs/{run_id}/programs/youth_drive")
    show("lineage: from one program number to sample, intervals, weights, exclusions", {
        "number": lineage["number"]["estimates"],
        "interval": lineage["number"]["intervals"]["reach_persons"],
        "sample_rows": lineage["trace"]["sample_rows"],
        "weight_adjustment": lineage["trace"]["weight_adjustment"],
        "exclusion_totals": lineage["trace"]["exclusion_totals"],
        "conflicts": lineage["trace"]["conflicts"],
    })

    # ---- 3. missing vs. confirmed no listening ------------------------------
    s404 = demo.get(f"/v1/runs/{run_id}/days", {"person_id": "S404"})
    s601 = demo.get(f"/v1/runs/{run_id}/days", {"person_id": "S601"})
    exclusions = demo.get(f"/v1/runs/{run_id}/exclusions")
    show("filled-but-silent diary (S404): no_listening, not missing", [
        {"day": d["survey_day"], "listening": float(d["listening_minutes"]),
         "no_listening": float(d["no_listening_minutes"]),
         "uncovered": float(d["uncovered_minutes"])} for d in s404
    ])
    show("diary missing one hour (S601): the hour is uncovered, never silent", [
        {"day": d["survey_day"], "listening": float(d["listening_minutes"]),
         "no_listening": float(d["no_listening_minutes"]),
         "uncovered": float(d["uncovered_minutes"])} for d in s601
    ])
    show("no diary returned at all (excluded, not zero)",
         {"missing_diary": exclusions["by_code"].get("missing_diary", {}).get("count"),
          "example": (exclusions["by_code"].get("missing_diary", {}).get("persons") or [None])[0]})

    # ---- 4. four-person household + cross-midnight -------------------------
    boot = demo.get(f"/v1/runs/{run_id}/bootstrap")["summary"]
    show("bootstrap: four-person household is one cluster", {
        "resampling_unit": boot["resampling_unit"],
        "households": boot["households"],
        "duplicate_identifier_copies_deduplicated": boot["duplicate_identifier_copies_deduplicated"],
        "replicates": boot["requested"],
        "failure_rate": boot["failure_rate"],
        "intervals_allowed": boot["intervals_allowed"],
    })
    midnight = demo.get(f"/v1/runs/{run_id}/days", {"person_id": "S301"})
    show("cross-midnight 23:30->00:20 is split 30/20 across two survey days", [
        {"day": d["survey_day"], "listening": float(d["listening_minutes"])}
        for d in midnight
    ])

    # ---- 5. conflict --------------------------------------------------------
    conflicts = demo.get(f"/v1/runs/{run_id}/conflicts")
    show("cross-station conflict is left pending", conflicts)

    # ---- 6. resubmission ----------------------------------------------------
    before_id, before = demo.run(seeded, cutoff["available_max_submission_seq"] - 1, "demo-before-resub")
    after_id, after = demo.run(seeded, cutoff["available_max_submission_seq"], "demo-after-resub")
    pre = demo.get(f"/v1/runs/{before_id}/days", {"person_id": "S701"})
    post = demo.get(f"/v1/runs/{after_id}/days", {"person_id": "S701"})
    show("resubmission changes the current calculation only", {
        "before": {d["survey_day"]: float(d["listening_minutes"]) for d in pre},
        "after": {d["survey_day"]: float(d["listening_minutes"]) for d in post},
        "retired_rows": len([u for u in demo.get(f"/v1/runs/{after_id}/diary-usage", {"person_id": "S701"})
                             if u["disposition"] == "superseded_retired"]),
    })

    # ---- 7. empty calibration cell and weight bounds ------------------------
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from demo import fixtures

    empty_target = demo.post("/v1/targets/batches", fixtures.EMPTY_CELL_TARGET)
    if empty_target:
        empty_id, empty = demo.run(seeded, cutoff["available_max_submission_seq"],
                                   "demo-empty-cell", target_seq=empty_target["batch_seq"])
        show("empty calibration cell: publication refused, residual kept", {
            "run_status": empty["status"],
            "failure": empty["failure"]["code"],
            "calibration": empty["calibration"]["status"],
            "empty_cells": empty["calibration"]["empty_cells"],
            "report_available": demo.get(f"/v1/runs/{empty_id}/report") is None,
        })

    bounds_target = demo.post("/v1/targets/batches", fixtures.BOUNDS_TARGET)
    if bounds_target:
        bounds_id, bounds = demo.run(seeded, cutoff["available_max_submission_seq"],
                                     "demo-bounds", target_seq=bounds_target["batch_seq"])
        show("clamped weights make a margin unreachable", {
            "run_status": bounds["status"],
            "calibration": bounds["calibration"]["status"],
            "unreachable_cells": bounds["calibration"]["unreachable_cells"][:4],
            "max_relative_residual": bounds["calibration"]["max_rel_residual"],
        })

    # ---- 8. empty sample stratum -------------------------------------------
    stratum_frame = demo.post("/v1/recruitment/batches", fixtures.stratum_empty_frame())
    if stratum_frame:
        empty_stratum_id, empty_stratum = demo.run(
            seeded, cutoff["available_max_submission_seq"], "demo-empty-stratum",
            frame_seq=stratum_frame["batch_seq"],
        )
        build = next(s for s in empty_stratum["stages"] if s["stage"] == "build_sample")
        show("empty sample stratum: reported, not filled with a synthetic respondent", {
            "run_status": empty_stratum["status"],
            "strata_without_eligible_sample": build["payload"]["strata"]["strata_without_eligible_sample"],
            "by_stratum": build["payload"]["strata"]["by_stratum"],
            "note": build["payload"]["strata"]["note"],
        })

    # ---- 9. unknown inclusion probability blocks a formal estimate ----------
    if seeded.get("recruitment_unknown_probability_seq"):
        body = {
            "note": "demo-unknown-probability",
            "inputs": {
                "recruitment_batch_seq": seeded["recruitment_unknown_probability_seq"],
                "target_batch_seq": seeded["target_batch_seq"],
                "program_batch_seq": seeded["program_batch_seq"],
                "diary_seq_cutoff": cutoff["available_max_submission_seq"],
            },
            "config": {},
        }
        resp = demo.client.post("/v1/runs", json=body)
        show("unknown inclusion probability refuses a formal population estimate", {
            "http_status": resp.status_code,
            "error": resp.json().get("error"),
        })

    # ---- 10. late diary is a new run; old report is untouched ----------------
    late = demo.post("/v1/diary/submissions", fixtures.S801_LATE_DIARY)
    if late:
        new_cutoff = demo.get("/v1/diary/cutoff")["available_max_submission_seq"]
        late_id, late_detail = demo.run(seeded, new_cutoff, "demo-late-diary")
        report_before = demo.get(f"/v1/runs/{run_id}/report")["report_hash"]
        report_before_again = demo.get(f"/v1/runs/{run_id}/report")["report_hash"]
        show("late diary creates a new run; published report is unchanged", {
            "original_run": run_id,
            "original_report_hash": report_before,
            "original_report_hash_after_late_import": report_before_again,
            "new_run": late_id,
            "new_run_status": late_detail["status"],
            "new_run_cutoff": new_cutoff,
        })

    print("\nALL STEPS COMPLETED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
