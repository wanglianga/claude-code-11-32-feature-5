"""End-to-end API tests over the fictional dataset."""
from __future__ import annotations

import copy

import pytest

from demo import fixtures


def _run_body(seeded, cutoff=None, config=None, note="pytest", parent=None):
    return {
        "note": note,
        "inputs": {
            "recruitment_batch_seq": seeded["recruitment_batch_seq"],
            "target_batch_seq": seeded["target_batch_seq"],
            "program_batch_seq": seeded["program_batch_seq"],
            "diary_seq_cutoff": seeded["latest_cutoff"] if cutoff is None else cutoff,
        },
        "config": config or {},
        "parent_run_id": parent,
    }


def _execute(client, run_id, steps=20):
    resp = client.post(f"/v1/runs/{run_id}/execute", json={"max_steps": steps})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _publish_run(client, seeded, cutoff=None, config=None, note="pytest"):
    created = client.post("/v1/runs", json=_run_body(seeded, cutoff, config, note))
    assert created.status_code == 201, created.text
    run_id = created.json()["run"]["run_id"]
    _execute(client, run_id)
    detail = client.get(f"/v1/runs/{run_id}").json()
    return run_id, detail


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------
def test_requires_api_key(client):
    resp = client.get("/v1/runs", headers={"X-API-Key": ""})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "api_key_required"
    resp = client.get("/v1/runs", headers={"X-API-Key": "wrong"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "api_key_invalid"


def test_health_has_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


# ---------------------------------------------------------------------------
# Ingestion validation
# ---------------------------------------------------------------------------
def test_inconsistent_target_totals_are_refused(client):
    bad = copy.deepcopy(fixtures.INCONSISTENT_TARGET)
    resp = client.post("/v1/targets/batches", json=bad)
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] in {"target_totals_inconsistent", "request_invalid"}


def test_diary_partial_day_replacement_is_rejected(client, seeded):
    # A throwaway person keeps this rejection from disturbing seeded history.
    body = {
        "person_id": "REJECT-1",
        "household_id": "REJECT-H",
        "revision": 2,
        "submitted_at": "2026-09-04T10:00:00+08:00",
        "replacement_scope": {
            "type": "replace",
            "start": "2026-09-02T22:00:00+08:00",
            "end": "2026-09-03T06:00:00+08:00",
        },
        "coverage": [],
        "entries": [],
    }
    resp = client.post("/v1/diary/submissions", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "diary_partial_day_replacement"


# ---------------------------------------------------------------------------
# Ingestion validation (no seeded dependency)
# ---------------------------------------------------------------------------
def test_duplicate_person_in_recruitment_batch_is_refused(client):
    batch = fixtures.recruitment_batch(batch_ref="dup-person")
    batch["rows"].append(dict(batch["rows"][0]))
    resp = client.post("/v1/recruitment/batches", json=batch)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "recruitment_duplicate_person"


def test_recruitment_batch_is_idempotent(client):
    batch = fixtures.recruitment_batch(batch_ref="idem-frame")
    first = client.post("/v1/recruitment/batches", json=batch)
    second = client.post("/v1/recruitment/batches", json=batch)
    assert first.status_code == second.status_code == 201
    assert first.json()["batch_seq"] == second.json()["batch_seq"]
    assert second.json()["idempotent"] is True


def test_diary_revision_must_increase(client, seeded):
    body = copy.deepcopy(fixtures.S701_RESUBMISSION)
    body["revision"] = 1
    resp = client.post("/v1/diary/submissions", json=body)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "diary_revision_not_increasing"


def test_unknown_timezone_is_rejected(client):
    batch = fixtures.program_batch(batch_ref="bad-tz")
    batch["timezone"] = "Mars/Olympus"
    resp = client.post("/v1/programs/batches", json=batch)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "timezone_unknown"


# ---------------------------------------------------------------------------
# Full run
# ---------------------------------------------------------------------------
def test_published_run_has_traceable_lineage(client, seeded):
    run_id, detail = _publish_run(client, seeded, note="lineage")
    assert detail["status"] == "published", detail.get("failure")

    report = client.get(f"/v1/runs/{run_id}/report")
    assert report.status_code == 200
    payload = report.json()
    assert payload["report_hash"]
    programs = payload["report"]["programs"]
    assert {p["program_key"] for p in programs} == {"youth_drive", "midnight_news", "afternoon_talk"}
    for p in programs:
        est = p["estimates"]
        # Weighted reach, both mean denominators and the sample metadata are all present.
        assert est["reach_persons"] > 0
        assert est["mean_minutes_per_person"] > 0
        assert est["mean_minutes_per_listener"] >= est["mean_minutes_per_person"]
        assert est["unweighted_n"] > 0
        assert est["weight_min"] <= est["weight_max"]
        assert 0 < est["effective_sample_size"] <= est["unweighted_n"]
        assert p["intervals"]["reach_persons"]["low"] <= est["reach_persons"] <= p["intervals"]["reach_persons"]["high"]

    lineage = client.get(f"/v1/runs/{run_id}/programs/youth_drive").json()
    assert lineage["title"] == "Youth Drive"
    assert lineage["trace"]["weight_adjustment"]["method"]
    assert lineage["trace"]["interval_decomposition"]["replicates_used"] > 0
    assert lineage["trace"]["exclusions"]
    assert lineage["comparison"]["supported"] is False

    sample = client.get(f"/v1/runs/{run_id}/programs/youth_drive/sample?limit=10").json()
    assert sample["rows"]
    assert any(r["minutes"] and r["minutes"] > 0 for r in sample["rows"])


def test_report_is_immutable(client, seeded):
    run_id, _ = _publish_run(client, seeded, note="immutable")
    first = client.get(f"/v1/runs/{run_id}/report").json()
    # Re-executing a published run does not rewrite anything.
    again = client.post(f"/v1/runs/{run_id}/execute", json={"max_steps": 20})
    assert again.status_code == 200
    assert again.json()["message"] == "run already finalised"
    second = client.get(f"/v1/runs/{run_id}/report").json()
    assert second["report_hash"] == first["report_hash"]


def test_report_row_cannot_be_updated_in_db(client, seeded, database):
    run_id, _ = _publish_run(client, seeded, note="db-trigger")
    import psycopg

    with database.scoped() as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg.errors.RaiseException):
            cur.execute(
                "UPDATE run_reports SET report = '{}'::jsonb WHERE run_id = %s", (run_id,)
            )
        conn.rollback()


def test_resubmission_changes_current_calculation_only(client, seeded):
    before_id, _ = _publish_run(client, seeded, cutoff=seeded["cutoff_before_resub"], note="pre-resubmission")
    after_id, _ = _publish_run(client, seeded, note="post-resubmission")
    pre = client.get(f"/v1/runs/{before_id}/days?person_id=S701").json()
    post = client.get(f"/v1/runs/{after_id}/days?person_id=S701").json()
    pre_day2 = next(d for d in pre if d["survey_day"] == "2026-09-02")
    post_day2 = next(d for d in post if d["survey_day"] == "2026-09-02")
    assert float(pre_day2["listening_minutes"]) == 90.0
    assert float(post_day2["listening_minutes"]) == 30.0

    usage = client.get(f"/v1/runs/{after_id}/diary-usage?person_id=S701").json()
    retired = [u for u in usage if u["disposition"] == "superseded_retired"]
    assert retired, "revision 2 must retire the old rows rather than delete history"
    # Cross-day replacement did not erase the untouched day.
    pre_day1 = next(d for d in pre if d["survey_day"] == "2026-09-01")
    post_day1 = next(d for d in post if d["survey_day"] == "2026-09-01")
    assert float(pre_day1["uncovered_minutes"]) == float(post_day1["uncovered_minutes"]) == 0.0


def test_late_diary_creates_a_new_run(client, seeded, database):
    first_id, first = _publish_run(client, seeded, note="before-late")
    from app import ingest
    from zoneinfo import ZoneInfo

    ingest.create_diary_submission(
        database, copy.deepcopy(fixtures.S801_LATE_DIARY), "pytest", ZoneInfo("Asia/Shanghai")
    )
    with database.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT MAX(submission_seq) AS m FROM diary_submissions")
        new_cutoff = cur.fetchone()["m"]
    second_id, second = _publish_run(client, seeded, cutoff=new_cutoff, note="after-late")
    assert first_id != second_id
    old_report = client.get(f"/v1/runs/{first_id}/report").json()
    new_report = client.get(f"/v1/runs/{second_id}/report").json()
    assert old_report["report_hash"] != new_report["report_hash"]
    # The original run still exists with its original diary cutoff.
    detail = client.get(f"/v1/runs/{first_id}").json()
    assert detail["inputs"]["diary"]["payload"]["seq_cutoff"] < new_cutoff


def test_corrected_targets_create_new_run_without_touching_old_report(client, seeded):
    first_id, _ = _publish_run(client, seeded, note="targets-v1")
    resp = client.post("/v1/targets/batches", json=fixtures.CORRECTED_TARGET_BATCH)
    assert resp.status_code == 201
    new_target = resp.json()["batch_seq"]
    body = _run_body(seeded, note="targets-v2")
    body["inputs"]["target_batch_seq"] = new_target
    created = client.post("/v1/runs", json=body)
    assert created.status_code == 201
    second_id = created.json()["run"]["run_id"]
    _execute(client, second_id)
    old = client.get(f"/v1/runs/{first_id}/report").json()
    old_again = client.get(f"/v1/runs/{first_id}/report").json()
    assert old["report_hash"] == old_again["report_hash"]
    new = client.get(f"/v1/runs/{second_id}/report")
    assert new.status_code == 200


def test_diary_cutoff_beyond_available_is_rejected(client, seeded):
    body = _run_body(seeded)
    body["inputs"]["diary_seq_cutoff"] = 999999
    resp = client.post("/v1/runs", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "diary_cutoff_beyond_available"


def test_resume_from_interruption_uses_same_inputs(client, seeded):
    created = client.post("/v1/runs", json=_run_body(seeded, note="resume"))
    run_id = created.json()["run"]["run_id"]
    first = _execute(client, run_id, steps=2)
    assert first["executed"] == ["validate_inputs", "build_sample"]
    second = _execute(client, run_id, steps=20)
    assert "publish_report" in second["executed"]
    detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["status"] == "published"
    stages = {s["stage"]: s["status"] for s in detail["stages"]}
    assert all(v == "done" for v in stages.values())


# ---------------------------------------------------------------------------
# Infeasible / failed calibration
# ---------------------------------------------------------------------------
def test_empty_cell_blocks_publication_and_keeps_residuals(client, seeded):
    resp = client.post("/v1/targets/batches", json=fixtures.EMPTY_CELL_TARGET)
    assert resp.status_code == 201
    batch = resp.json()["batch_seq"]
    body = _run_body(seeded, note="empty-cell")
    body["inputs"]["target_batch_seq"] = batch
    created = client.post("/v1/runs", json=body)
    run_id = created.json()["run"]["run_id"]
    _execute(client, run_id)
    detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["status"] == "rejected"
    assert detail["failure"]["code"] == "calibration_not_successful"
    assert detail["calibration"]["status"] == "empty_cell"
    cells = client.get(f"/v1/runs/{run_id}/calibration").json()
    assert any(c["category"] == "under-18" for c in cells["cells"])
    report = client.get(f"/v1/runs/{run_id}/report")
    assert report.status_code == 404
    assert report.json()["error"]["code"] == "report_not_published"


def test_bounds_unreachable_is_reported(client, seeded):
    resp = client.post("/v1/targets/batches", json=fixtures.BOUNDS_TARGET)
    assert resp.status_code == 201
    batch = resp.json()["batch_seq"]
    body = _run_body(seeded, note="bounds")
    body["inputs"]["target_batch_seq"] = batch
    run_id = client.post("/v1/runs", json=body).json()["run"]["run_id"]
    _execute(client, run_id)
    detail = client.get(f"/v1/runs/{run_id}").json()
    assert detail["status"] == "rejected"
    assert detail["calibration"]["status"] == "bounds_unreachable"
    assert detail["calibration"]["unreachable_cells"]


def test_non_convergence_keeps_rounds_and_residuals(client, seeded):
    config = {"raking": {"max_iterations": 1, "tolerance": 1e-12}}
    run_id, detail = _publish_run(client, seeded, config=config, note="nonconverge")
    assert detail["status"] == "rejected"
    calib = client.get(f"/v1/runs/{run_id}/calibration").json()
    assert calib["summary"]["status"] == "non_convergence"
    assert len(calib["rounds"]) == 1
    assert any(abs(c["relative_residual"]) > 0 for c in calib["cells"])


def test_bootstrap_failure_rate_suppresses_intervals(client, seeded):
    # A very tight tolerance with one iteration forces most replicates to fail.
    config = {
        "raking": {"max_iterations": 1, "tolerance": 1e-12},
        "bootstrap": {"replicates": 20, "max_calibration_failure_rate": 0.0},
    }
    run_id, detail = _publish_run(client, seeded, config=config, note="boot-fail")
    # Point calibration fails too, so the run is rejected; use the summary.
    assert detail["status"] == "rejected"
    boot = client.get(f"/v1/runs/{run_id}/status").json()["bootstrap"]
    assert boot["failure_rate"] > 0
    assert boot["intervals_allowed"] is False


def test_unknown_inclusion_probability_blocks_formal_estimate(client, seeded):
    # A frame batch that carries S902 with a null inclusion probability cannot
    # produce a formal population estimate, so run creation is refused.
    body = _run_body(seeded, note="unknown-p")
    body["inputs"]["recruitment_batch_seq"] = seeded["recruitment_unknown_probability_seq"]
    resp = client.post("/v1/runs", json=body)
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "inclusion_probability_unknown"
    assert "S902" in err["details"]["persons"]


def test_comparison_is_not_offered(client, seeded):
    body = _run_body(seeded, note="compare")
    body["config"] = {"allow_program_comparison": True}
    resp = client.post("/v1/runs", json=body)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "cross_program_comparison_not_supported"


# ---------------------------------------------------------------------------
# Missing vs. confirmed-none
# ---------------------------------------------------------------------------
def test_unweighted_diary_average_overstates_youth_programming(client, seeded):
    """The documented hazard: averaging returned diaries alone inflates the youth show.

    Young listeners return diaries far more often, so a naive per-diary mean is
    pulled toward their heavy listening. Calibrated weights correct for it.
    """
    run_id, detail = _publish_run(client, seeded, note="naive-vs-weighted")
    assert detail["status"] == "published"
    report = client.get(f"/v1/runs/{run_id}/report").json()["report"]
    programs = {p["program_key"]: p for p in report["programs"]}

    # Recompute the naive mean straight from the stored rows.
    import psycopg

    from tests.conftest import TEST_DB_URL

    with psycopg.connect(TEST_DB_URL) as conn:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT pp.program_key,
                   AVG(pp.minutes) AS naive_mean,
                   SUM(pp.minutes * p.final_weight) / SUM(p.final_weight) AS weighted_mean
              FROM "{client.app.state.db.schema}".run_person_program pp
              JOIN "{client.app.state.db.schema}".run_persons p
                ON p.run_id = pp.run_id AND p.person_id = pp.person_id
             WHERE pp.run_id = %s
             GROUP BY 1
            """,
            (run_id,),
        )
        rows = {r[0]: (float(r[1]), float(r[2])) for r in cur.fetchall()}
    naive, weighted = rows["youth_drive"]
    assert naive > weighted * 1.3, (naive, weighted)
    assert programs["youth_drive"]["estimates"]["mean_minutes_per_person"] == pytest.approx(
        weighted, rel=1e-4
    )
    # Older-skewing programs move the other way, so the bias is not a constant.
    naive_news, weighted_news = rows["midnight_news"]
    assert naive_news < weighted_news


def test_missing_and_no_listening_are_handled_differently(client, seeded):
    run_id, detail = _publish_run(client, seeded, note="missing-vs-zero")
    assert detail["status"] == "published"

    exclusions = client.get(f"/v1/runs/{run_id}/exclusions").json()
    missing = exclusions["by_code"]["missing_diary"]
    assert missing["count"] >= 1
    person = missing["persons"][0]
    assert person["detail"]["treat_missing_as_zero_listening"] is True or (
        person["detail"].get("treat_missing_as_zero_listening") is False
    )
    assert "unknown" in person["detail"]["reason"].lower()

    # A filled diary with no listening is in the sample with zero minutes.
    days = client.get(f"/v1/runs/{run_id}/days?person_id=S404").json()
    assert days
    for d in days:
        assert float(d["listening_minutes"]) == 0.0
        assert float(d["no_listening_minutes"]) == 1440.0
        assert float(d["uncovered_minutes"]) == 0.0

    # The person with one unfilled hour keeps that hour as unknown, not silent.
    s601 = client.get(f"/v1/runs/{run_id}/persons?eligible=false&limit=500").json()
    codes = {p["person_id"]: p["exclusion_code"] for p in s601}
    assert codes.get("S601") == "incomplete_diary"
    s601_days = client.get(f"/v1/runs/{run_id}/days?person_id=S601").json()
    day2 = next(d for d in s601_days if d["survey_day"] == "2026-09-02")
    assert float(day2["uncovered_minutes"]) == 60.0
    assert float(day2["no_listening_minutes"]) == 1380.0


def test_four_person_household_is_one_bootstrap_cluster(client, seeded):
    run_id, detail = _publish_run(client, seeded, note="cluster")
    boot = client.get(f"/v1/runs/{run_id}/bootstrap").json()
    summary = boot["summary"]
    assert summary["resampling_unit"] == "household"
    assert summary["duplicate_identifier_copies_deduplicated"] is False
    persons = client.get(f"/v1/runs/{run_id}/persons?limit=1000").json()
    household = {p["person_id"] for p in persons if p["household_id"] == "H04"}
    assert household == {"S401", "S402", "S403", "S404"}
    # Four household members are one household in the bootstrap, not four.
    assert summary["households"] < len(persons)


def test_cross_midnight_is_split_between_survey_days(client, seeded):
    run_id, _ = _publish_run(client, seeded, note="midnight")
    days = client.get(f"/v1/runs/{run_id}/days?person_id=S301").json()
    by_day = {d["survey_day"]: float(d["listening_minutes"]) for d in days}
    assert by_day["2026-09-01"] == 30.0
    assert by_day["2026-09-02"] == 20.0
    assert by_day["2026-09-03"] == 0.0


def test_conflict_is_unresolved_in_report(client, seeded):
    run_id, _ = _publish_run(client, seeded, note="conflict")
    conflicts = client.get(f"/v1/runs/{run_id}/conflicts").json()
    assert any(c["person_id"] == "S501" and c["status"] == "pending" for c in conflicts)
    report = client.get(f"/v1/runs/{run_id}/report").json()["report"]
    assert any(c["person_id"] == "S501" for c in report["conflicts"]["pending"])


def test_conflict_resolution_applies_to_future_runs_only(client, seeded, database):
    first_id, _ = _publish_run(client, seeded, note="conflict-before")
    with database.scoped() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT person_id, station_a, station_b, start_utc, end_utc FROM run_conflicts WHERE run_id=%s AND person_id='S501'",
            (first_id,),
        )
        row = cur.fetchone()
    payload = {
        "person_id": row["person_id"],
        "station_a": row["station_a"],
        "station_b": row["station_b"],
        "start": row["start_utc"].isoformat(),
        "end": row["end_utc"].isoformat(),
        "resolution": "confirmed_station_a",
        "note": "phone call with the diarist",
    }
    resp = client.post("/v1/conflicts/resolutions", json=payload)
    if resp.status_code == 409:
        # Already registered by an earlier test run in this session.
        return
    assert resp.status_code == 201
    assert resp.json()["applies_to"] == "future runs only"

    first_report = client.get(f"/v1/runs/{first_id}/report").json()
    again = client.get(f"/v1/runs/{first_id}/report").json()
    assert first_report["report_hash"] == again["report_hash"]

    second_id, detail = _publish_run(client, seeded, note="conflict-after")
    # The person now joins the sample or is no longer flagged for the conflict.
    persons = client.get(f"/v1/runs/{second_id}/persons?limit=1000").json()
    s501 = next(p for p in persons if p["person_id"] == "S501")
    assert s501["exclusion_code"] in (None, "incomplete_diary")


# ---------------------------------------------------------------------------
# Coverage gaps and comparison refusal
# ---------------------------------------------------------------------------
def test_undercovered_groups_are_reported(client, seeded):
    run_id, detail = _publish_run(client, seeded, note="gaps")
    gaps = client.get(f"/v1/runs/{run_id}/coverage-gaps").json()["cells"]
    assert gaps
    under = [g for g in gaps if g["status"] == "undercovered"]
    assert under, gaps
    for g in under:
        assert g["representation_ratio"] < g["ratio_floor"]
        assert g["comparison_warning"]
    report = client.get(f"/v1/runs/{run_id}/report").json()["report"]
    assert report["coverage_assessment"]["undercovered_groups"]
    assert report["comparison"]["supported"] is False
    assert "not" in report["comparison"]["reason"].lower()


def test_report_never_ranks_programs(client, seeded):
    run_id, _ = _publish_run(client, seeded, note="no-ranking")
    report = client.get(f"/v1/runs/{run_id}/report").json()["report"]
    serialized = str(report).lower()
    for forbidden in ("best program", "ranking", "winner", "outperform"):
        assert forbidden not in serialized


def test_cross_program_comparison_is_refused_with_reason(client, seeded):
    run_id, _ = _publish_run(client, seeded, note="compare-endpoint")
    resp = client.get(
        f"/v1/runs/{run_id}/compare?program_a=youth_drive&program_b=midnight_news"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["supported"] is False
    assert body["difference_estimate"] is None
    assert body["interval"] is None
    assert "not compare" in body["reason"]


def test_within_program_group_difference_has_interval(client, seeded):
    run_id, detail = _publish_run(client, seeded, note="group-diff")
    assert detail["status"] == "published"
    resp = client.get(
        f"/v1/runs/{run_id}/programs/youth_drive/differences"
        "?dimension=age_band&group_a=18-24&group_b=45-64"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["metric"] == "mean_minutes_per_person"
    diff = body["difference"]
    assert diff["estimate"] == pytest.approx(
        body["values"]["18-24"] - body["values"]["45-64"], abs=1e-6
    )
    # With a healthy bootstrap the interval brackets the point estimate.
    assert diff["low"] is not None and diff["high"] is not None
    assert diff["low"] <= diff["estimate"] <= diff["high"]
    assert body["cross_program_comparison"]["supported"] is False


def test_group_difference_rejects_unknown_group(client, seeded):
    run_id, _ = _publish_run(client, seeded, note="group-diff-bad")
    resp = client.get(
        f"/v1/runs/{run_id}/programs/youth_drive/differences"
        "?dimension=age_band&group_a=18-24&group_b=does-not-exist"
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "group_unknown"


def test_stratum_diagnostics_are_exposed(client, seeded):
    run_id, _ = _publish_run(client, seeded, note="strata")
    detail = client.get(f"/v1/runs/{run_id}").json()
    build = next(s for s in detail["stages"] if s["stage"] == "build_sample")
    strata = build["payload"]["strata"]
    assert "by_stratum" in strata
    assert strata["by_stratum"]
    assert "strata_without_eligible_sample" in strata
    for name, s in strata["by_stratum"].items():
        assert s["frame_persons"] >= s["eligible_persons"]


def test_empty_sample_stratum_is_reported_not_filled(client, seeded):
    resp = client.post("/v1/recruitment/batches", json=fixtures.stratum_empty_frame())
    assert resp.status_code == 201
    frame_seq = resp.json()["batch_seq"]
    body = _run_body(seeded, note="empty-stratum")
    body["inputs"]["recruitment_batch_seq"] = frame_seq
    created = client.post("/v1/runs", json=body)
    assert created.status_code == 201, created.text
    run_id = created.json()["run"]["run_id"]
    _execute(client, run_id)
    detail = client.get(f"/v1/runs/{run_id}").json()
    build = next(s for s in detail["stages"] if s["stage"] == "build_sample")
    strata = build["payload"]["strata"]
    assert "rural-remote" in strata["strata_without_eligible_sample"]
    assert strata["by_stratum"]["rural-remote"]["frame_persons"] > 0
    assert strata["by_stratum"]["rural-remote"]["eligible_persons"] == 0
    # No synthetic person appears: the person count matches the frame.
    persons = client.get(f"/v1/runs/{run_id}/persons?limit=1000").json()
    assert not any(p["person_id"].startswith("SYNTH") for p in persons)
