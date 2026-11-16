"""Run orchestration: pin inputs, build the sample, calibrate, bootstrap, report.

Stages are persisted so an interrupted run resumes with the same pinned inputs
and configuration. Published reports are written once and can never be
updated; a late diary or a corrected target produces a *new* run.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .analysis import (
    analyze_person,
    build_frame,
    day_windows,
    program_exposure,
    survey_days,
)
from .analytics.estimate import (
    point_estimates,
    percentile_interval,
    stratified_household_bootstrap,
)
from .analytics.raking import RakingConfig, rake
from .config import Settings
from .db import Database
from .errors import conflict, invalid, not_found
from .ingest import load_programs, load_recruitment, load_submissions, load_targets
from .jsonutil import canonical_hash, jb, loads
from .timeutil import UTC, iso

STAGES = [
    "validate_inputs",
    "build_sample",
    "program_exposure",
    "calibrate",
    "bootstrap",
    "estimate",
    "publish_report",
]

DEFAULT_DIARY_WINDOW = {"start_local": "00:00", "end_local": "24:00"}


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------
def serialize_run(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["run_id"] = str(out["run_id"])
    for key in ("cutoffs", "survey_period", "config", "failure", "calibration", "bootstrap"):
        if key in out:
            out[key] = loads(out[key])
    for key in ("created_at", "updated_at", "started_at", "finished_at", "published_at"):
        if key in out and out[key] is not None:
            out[key] = iso(out[key])
    if out.get("parent_run_id"):
        out["parent_run_id"] = str(out["parent_run_id"])
    return out


def fetch_run(cur, run_id) -> dict[str, Any]:
    cur.execute(
        """
        SELECT run_id, status, seed, cutoffs, survey_period, config, config_hash,
               input_hash, parent_run_id, note, created_by, created_at, updated_at,
               started_at, finished_at, failure, calibration, bootstrap, interval_status,
               published_at
          FROM runs WHERE run_id = %s
        """,
        (str(run_id),),
    )
    row = cur.fetchone()
    if row is None:
        raise not_found("run_not_found", "run does not exist")
    return serialize_run(row)


def _run_row(cur, run_id) -> dict[str, Any]:
    cur.execute("SELECT * FROM runs WHERE run_id = %s", (str(run_id),))
    row = cur.fetchone()
    if row is None:
        raise not_found("run_not_found", "run does not exist")
    return row


def _config(run_row) -> dict[str, Any]:
    return loads(run_row["config"])


def _tz(run_row) -> ZoneInfo:
    tz_name = _config(run_row)["timezone"]
    try:
        return ZoneInfo(tz_name)
    except ZoneInfoNotFoundError as exc:  # pragma: no cover - validated at creation
        raise invalid("timezone_unknown", "unknown timezone", {"timezone": tz_name}) from exc


def _inputs(cur, run_id) -> dict[str, Any]:
    cur.execute("SELECT kind, payload FROM run_inputs WHERE run_id = %s", (str(run_id),))
    return {row["kind"]: loads(row["payload"]) for row in cur.fetchall()}


def _load_resolutions(cur, person_ids: list[str]) -> dict[tuple, dict[str, Any]]:
    if not person_ids:
        return {}
    cur.execute(
        """
        SELECT resolution_id, person_id, station_a, station_b, start_utc, end_utc, resolution
          FROM conflict_resolutions
         WHERE person_id = ANY(%s)
        """,
        (person_ids,),
    )
    out: dict[tuple, dict[str, Any]] = {}
    for r in cur.fetchall():
        stations = sorted((r["station_a"], r["station_b"]))
        key = (r["person_id"], stations[0], stations[1], r["start_utc"], r["end_utc"])
        out[key] = {
            "resolution_id": r["resolution_id"],
            "status": r["resolution"],
            "resolution": r["resolution"],
        }
    return out


def _load_segments(cur, run_id, person_id) -> dict[date, dict[str, Any]]:
    cur.execute(
        """
        SELECT survey_day, listening_minutes, no_listening_minutes, unknown_minutes,
               uncovered_minutes, covered_minutes, window_minutes
          FROM run_person_days WHERE run_id=%s AND person_id=%s ORDER BY survey_day
        """,
        (str(run_id), person_id),
    )
    return {r["survey_day"]: r for r in cur.fetchall()}


# ---------------------------------------------------------------------------
# Run creation
# ---------------------------------------------------------------------------
def create_run(db: Database, settings: Settings, body: dict[str, Any], actor: str) -> dict[str, Any]:
    inputs = body["inputs"]
    # Direct callers may pass a partial config; fill in the same defaults the
    # API schema applies so the pinned config is always complete.
    from .models import RunConfigIn

    config = RunConfigIn.model_validate(body.get("config") or {}).model_dump(mode="json")
    seed = int(body.get("seed") if body.get("seed") is not None else config["bootstrap"]["seed"])
    with db.scoped() as conn:
        cur = conn.cursor()
        recruitment = load_recruitment(cur, inputs["recruitment_batch_seq"])
        targets = load_targets(cur, inputs["target_batch_seq"])
        programs = load_programs(cur, inputs["program_batch_seq"])

        program_tz = programs["timezone"]
        if config.get("timezone") and config["timezone"] != program_tz:
            raise invalid(
                "timezone_mismatch",
                "run timezone must match the program batch timezone",
                {"run_timezone": config["timezone"], "program_timezone": program_tz},
            )
        config = {
            **config,
            "timezone": program_tz,
            "diary_window": DEFAULT_DIARY_WINDOW,
        }
        period_start = recruitment["survey_period_start"]
        period_end = recruitment["survey_period_end"]
        expected = survey_days(period_start, period_end)
        outside = sorted(
            {a["air_date"] for a in programs["airings"]}
            - {a["air_date"] for a in programs["airings"] if period_start <= a["air_date"] <= period_end}
        )
        if outside:
            raise invalid(
                "program_outside_survey_period",
                "program airings fall outside the recruitment survey period",
                {"air_dates": [d.isoformat() for d in outside]},
            )

        target_margins = loads(targets["dimensions"])
        for dim in config["raking"]["dimensions"]:
            if dim not in target_margins:
                raise invalid("target_dimension_missing", f"target batch has no margin for {dim}")

        if config.get("allow_program_comparison"):
            raise invalid(
                "cross_program_comparison_not_supported",
                "cross-program performance comparison is refused: coverage gaps differ by program and by group",
            )
        unknown_probability = [
            r["person_id"] for r in recruitment["rows"] if r["inclusion_probability"] is None
        ]
        if unknown_probability:
            raise invalid(
                "inclusion_probability_unknown",
                "unknown inclusion probability blocks a formal population estimate; "
                "supply a known sampling probability or use a different frame batch",
                {"persons": unknown_probability, "batch_seq": inputs["recruitment_batch_seq"]},
            )

        seq_cutoff = int(inputs["diary_seq_cutoff"])
        cur.execute("SELECT COALESCE(MAX(submission_seq), 0) AS m FROM diary_submissions")
        max_seq = cur.fetchone()["m"]
        if seq_cutoff > max_seq:
            raise invalid(
                "diary_cutoff_beyond_available",
                "diary_seq_cutoff exceeds available submissions",
                {"cutoff": seq_cutoff, "max_available": max_seq},
            )
        if body.get("parent_run_id"):
            cur.execute("SELECT 1 FROM runs WHERE run_id = %s", (str(body["parent_run_id"]),))
            if cur.fetchone() is None:
                raise not_found("parent_run_not_found", "parent run does not exist")

        margins_for_hash = {
            dim: {cat: float(v) for cat, v in cats.items()} for dim, cats in target_margins.items()
        }
        input_hash = canonical_hash(
            {
                "inputs": inputs,
                "config": config,
                "seed": seed,
                "recruitment": [
                    {
                        "person_id": r["person_id"],
                        "household_id": r["household_id"],
                        "strata": r["strata"],
                        "age_band": r["age_band"],
                        "region": r["region"],
                        "sex": r["sex"],
                        "p": None if r["inclusion_probability"] is None else float(r["inclusion_probability"]),
                    }
                    for r in recruitment["rows"]
                ],
                "targets": {"seq": inputs["target_batch_seq"], "margins": margins_for_hash},
                "programs": [
                    {
                        "key": a["program_key"],
                        "station": a["station"],
                        "air_date": a["air_date"].isoformat(),
                        "start": a["start_local"],
                        "end": a["end_local"],
                    }
                    for a in programs["airings"]
                ],
                "diary_rows": _diary_hash_rows(cur, seq_cutoff),
            }
        )
        config_hash = canonical_hash(config)
        run_id = uuid.uuid4()

        cur.execute(
            """
            INSERT INTO runs
                (run_id, status, seed, cutoffs, survey_period, config, config_hash,
                 input_hash, parent_run_id, note, created_by)
            VALUES (%s,'created',%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                str(run_id),
                seed,
                jb({"diary_seq": seq_cutoff}),
                jb(
                    {
                        "start": period_start.isoformat(),
                        "end": period_end.isoformat(),
                        "days": len(expected),
                    }
                ),
                jb(config),
                config_hash,
                input_hash,
                str(body["parent_run_id"]) if body.get("parent_run_id") else None,
                body.get("note"),
                actor,
            ),
        )
        payloads = {
            "recruitment": {
                "batch_seq": inputs["recruitment_batch_seq"],
                "survey_period": {"start": period_start.isoformat(), "end": period_end.isoformat()},
                "row_count": len(recruitment["rows"]),
            },
            "target": {
                "batch_seq": inputs["target_batch_seq"],
                "population_total": targets["population_total"],
                "margins": margins_for_hash,
            },
            "program": {
                "batch_seq": inputs["program_batch_seq"],
                "timezone": program_tz,
                "airing_count": len(programs["airings"]),
            },
            "diary": {"seq_cutoff": seq_cutoff, "max_available_at_creation": max_seq},
        }
        for kind, payload in payloads.items():
            cur.execute(
                "INSERT INTO run_inputs (run_id, kind, seq, payload) VALUES (%s,%s,%s,%s)",
                (str(run_id), kind, int(payload.get("batch_seq", payload.get("seq_cutoff", 0))), jb(payload)),
            )
        for stage in STAGES:
            cur.execute(
                "INSERT INTO run_stages (run_id, stage, status) VALUES (%s,%s,'pending')",
                (str(run_id), stage),
            )
        cur.execute(
            "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES (%s,%s,'run',%s,%s)",
            (actor, "run.create", str(run_id), jb({"inputs": inputs, "seed": seed, "input_hash": input_hash})),
        )
        conn.commit()
        run_row = fetch_run(cur, run_id)
        duplicate = None
        cur.execute(
            "SELECT run_id, status FROM runs WHERE input_hash=%s AND run_id<>%s ORDER BY created_at LIMIT 1",
            (input_hash, str(run_id)),
        )
        match = cur.fetchone()
        if match:
            duplicate = {"run_id": str(match["run_id"]), "status": match["status"]}

    return {
        "run": run_row,
        "deterministic_inputs": {
            "input_hash": input_hash,
            "config_hash": config_hash,
            "duplicate_of_existing_equivalent_run": duplicate,
        },
        "limitations": {
            "formal_population_estimate": True,
            "missing_diary_is_unknown_not_zero_listening": True,
            "cross_program_comparison_supported": False,
            "cross_program_comparison_reason": (
                "Coverage gaps mean each program is estimated from a different, incompletely represented "
                "population, so differences between programs are not comparable performance evidence."
            ),
        },
    }


def _diary_hash_rows(cur, seq_cutoff: int) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT s.person_id, s.submission_seq, s.revision, s.replacement_scope, s.coverage,
               e.station, e.source_serial, e.start_utc, e.end_utc
          FROM diary_submissions s
          LEFT JOIN diary_entries e ON e.submission_seq = s.submission_seq
         WHERE s.submission_seq <= %s
         ORDER BY s.submission_seq, e.entry_id
        """,
        (seq_cutoff,),
    )
    return [
        {
            "person_id": r["person_id"],
            "submission_seq": r["submission_seq"],
            "revision": r["revision"],
            "scope": loads(r["replacement_scope"]),
            "coverage": loads(r["coverage"]),
            "station": r["station"],
            "source_serial": r["source_serial"],
            "start": iso(r["start_utc"]),
            "end": iso(r["end_utc"]),
        }
        for r in cur.fetchall()
    ]


# ---------------------------------------------------------------------------
# Staged execution
# ---------------------------------------------------------------------------
def _stage_status(cur, run_id, stage: str) -> str:
    cur.execute("SELECT status FROM run_stages WHERE run_id = %s AND stage = %s", (str(run_id), stage))
    row = cur.fetchone()
    return row["status"] if row else "pending"


def _set_stage(conn, cur, run_id, stage: str, status: str, payload=None, error=None) -> None:
    now = datetime.now(UTC)
    cur.execute(
        """
        UPDATE run_stages SET status = %s,
               started_at = COALESCE(started_at, %s),
               finished_at = CASE WHEN %s IN ('done','failed') THEN %s ELSE finished_at END,
               payload = COALESCE(%s, payload),
               error = %s
         WHERE run_id = %s AND stage = %s
        """,
        (
            status,
            now,
            status,
            now,
            None if payload is None else jb(payload),
            None if error is None else jb(error),
            str(run_id),
            stage,
        ),
    )
    conn.commit()


def _fail_run(conn, cur, run_id, stage: str, exc: Exception) -> dict[str, Any]:
    conn.rollback()
    failure = {
        "stage": stage,
        "error_type": type(exc).__name__,
        "message": str(exc),
        "code": getattr(exc, "code", None),
        "details": getattr(exc, "details", None),
    }
    cur.execute(
        "UPDATE runs SET status='failed', failure=%s, finished_at=now(), updated_at=now() WHERE run_id=%s",
        (jb(failure), str(run_id)),
    )
    cur.execute(
        "UPDATE run_stages SET status='failed', error=%s, started_at=COALESCE(started_at,now()), finished_at=now() WHERE run_id=%s AND stage=%s",
        (jb(failure), str(run_id), stage),
    )
    cur.execute(
        "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES ('system','run.failed','run',%s,%s)",
        (str(run_id), jb(failure)),
    )
    conn.commit()
    return failure


def execute_run(db: Database, run_id, max_steps: int = 20) -> dict[str, Any]:
    """Advance the run through pending stages. Idempotent and resumable."""
    with db.scoped() as conn:
        cur = conn.cursor()
        run = fetch_run(cur, run_id)
        if run["status"] in ("published", "rejected"):
            return {"run": run, "executed": [], "message": "run already finalised"}
        if run["status"] == "failed":
            raise conflict(
                "run_failed",
                "run failed; create a new run with the same inputs to retry",
                run.get("failure"),
            )
        cur.execute(
            "UPDATE runs SET status='running', started_at=COALESCE(started_at, now()), updated_at=now() WHERE run_id=%s",
            (str(run_id),),
        )
        conn.commit()
        executed: list[str] = []
        steps = 0
        while steps < max_steps:
            if fetch_run(cur, run_id)["status"] in ("rejected", "published", "failed"):
                break
            pending = [s for s in STAGES if _stage_status(cur, run_id, s) in ("pending", "running")]
            if not pending:
                break
            stage = pending[0]
            executed.append(stage)
            _set_stage(conn, cur, run_id, stage, "running")
            try:
                payload = _run_stage(db, conn, cur, run_id, stage)
                _set_stage(conn, cur, run_id, stage, "done", payload=payload)
            except Exception as exc:  # noqa: BLE001 - failure is persisted for diagnosis
                _fail_run(conn, cur, run_id, stage, exc)
                # Re-read so the caller sees the rejected/failed status even
                # when the DB row survived the stage-level exception.
                final = fetch_run(cur, run_id)
                if final["status"] == "failed":
                    raise conflict("run_stage_failed", f"stage {stage} failed", final.get("failure")) from exc
                raise
            steps += 1
        final = fetch_run(cur, run_id)
        if final["status"] == "rejected":
            cur.execute(
                """
                UPDATE run_stages
                   SET status='done', started_at=COALESCE(started_at, now()), finished_at=now(),
                       payload=COALESCE(payload, %s)
                 WHERE run_id=%s AND status='pending'
                """,
                (jb({"skipped": True, "reason": "run rejected"}), str(run_id)),
            )
            conn.commit()
        run = fetch_run(cur, run_id)
    return {"run": run, "executed": executed}


def _run_stage(db: Database, conn, cur, run_id, stage: str) -> dict[str, Any]:
    handlers = {
        "validate_inputs": _stage_validate,
        "build_sample": _stage_build_sample,
        "program_exposure": _stage_program_exposure,
        "calibrate": _stage_calibrate,
        "bootstrap": _stage_bootstrap,
        "estimate": _stage_estimate,
        "publish_report": _stage_publish,
    }
    return handlers[stage](db, conn, cur, run_id)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def _stage_validate(db, conn, cur, run_id) -> dict[str, Any]:
    run = _run_row(cur, run_id)
    config = _config(run)
    inputs = _inputs(cur, run_id)
    recruitment = load_recruitment(cur, inputs["recruitment"]["batch_seq"])
    targets = load_targets(cur, inputs["target"]["batch_seq"])
    programs = load_programs(cur, inputs["program"]["batch_seq"])
    margins = loads(targets["dimensions"])
    dimension_totals = loads(targets["dimension_totals"])
    total = float(targets["population_total"])

    inconsistent = {
        dim: float(value)
        for dim, value in dimension_totals.items()
        if abs(float(value) - total) > 1e-6
    }
    if inconsistent:
        raise invalid(
            "target_totals_inconsistent",
            "dimension margins point at different populations; calibration is stopped before any weight is computed",
            {"population_total": total, "dimension_totals": dimension_totals, "conflicting": inconsistent},
        )
    if config["allow_program_comparison"]:
        raise invalid(
            "cross_program_comparison_not_supported",
            "cross-program performance comparison is refused because coverage gaps differ by program",
        )

    frame_categories = {
        "age_band": {r["age_band"] for r in recruitment["rows"]},
        "region": {r["region"] for r in recruitment["rows"]},
        "sex": {r["sex"] for r in recruitment["rows"]},
    }
    empty_cells = []
    for dim, cats in margins.items():
        for cat, target in cats.items():
            if float(target) > 0 and cat not in frame_categories.get(dim, set()):
                empty_cells.append({"dimension": dim, "category": cat, "target": float(target)})

    days = survey_days(recruitment["survey_period_start"], recruitment["survey_period_end"])
    if len(days) != int(loads(run["survey_period"])["days"]):
        raise invalid("survey_period_changed", "pinned survey period no longer matches recruitment batch")
    program_keys = {a["program_key"] for a in programs["airings"]}
    return {
        "survey_period": {
            "start": recruitment["survey_period_start"].isoformat(),
            "end": recruitment["survey_period_end"].isoformat(),
            "days": len(days),
        },
        "timezone": str(_tz(run)),
        "population_total": targets["population_total"],
        "dimension_totals": dimension_totals,
        "target_categories": {dim: sorted(cats) for dim, cats in margins.items()},
        "frame_categories": {dim: sorted(cats) for dim, cats in frame_categories.items()},
        "target_cells_without_frame_samples": empty_cells,
        "programs": sorted(program_keys),
        "frame_persons": len(recruitment["rows"]),
        "households": len({r["household_id"] for r in recruitment["rows"]}),
    }


# ---------------------------------------------------------------------------
# Sample construction
# ---------------------------------------------------------------------------
def _stage_build_sample(db, conn, cur, run_id) -> dict[str, Any]:
    run = _run_row(cur, run_id)
    config = _config(run)
    inputs = _inputs(cur, run_id)
    tz = _tz(run)
    recruitment = load_recruitment(cur, inputs["recruitment"]["batch_seq"])
    submissions = load_submissions(cur, int(inputs["diary"]["seq_cutoff"]))
    frame = build_frame(recruitment, submissions, config)
    person_ids = [r["person_id"] for r in recruitment["rows"]]
    resolutions = _load_resolutions(cur, person_ids)
    expected = survey_days(recruitment["survey_period_start"], recruitment["survey_period_end"])

    cur.execute("DELETE FROM run_persons WHERE run_id=%s", (str(run_id),))
    cur.execute("DELETE FROM run_person_days WHERE run_id=%s", (str(run_id),))
    cur.execute("DELETE FROM run_diary_usage WHERE run_id=%s", (str(run_id),))
    cur.execute("DELETE FROM run_conflicts WHERE run_id=%s", (str(run_id),))

    eligible = 0
    excluded: dict[str, int] = {}
    with_diary = 0
    pending_conflicts = 0
    for fp in frame["persons"]:
        subs = submissions.get(fp.person_id, [])
        if subs:
            with_diary += 1
        analysis = analyze_person(fp, subs, expected, config, resolutions, tz)
        resolution = analysis["resolved"]
        if resolution["pending_conflicts"]:
            pending_conflicts += len(resolution["pending_conflicts"])
            for c in resolution["pending_conflicts"]:
                cur.execute(
                    """
                    INSERT INTO run_conflicts
                        (run_id, person_id, station_a, station_b, start_utc, end_utc, entry_a, entry_b, status)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending')
                    """,
                    (
                        str(run_id),
                        fp.person_id,
                        c["station_a"],
                        c["station_b"],
                        c["start"],
                        c["end"],
                        c.get("entry_a"),
                        c.get("entry_b"),
                    ),
                )
        if fp.exclusion_code is None and resolution["pending_conflicts"]:
            # Unresolved cross-station conflicts make this person's relevant
            # minutes unknown; including them would silently impute zero. This
            # check runs before completeness so the more specific reason wins.
            fp.exclusion_code = "unresolved_conflict"
            fp.exclusion_detail = {
                "reason": "diary reports two stations simultaneously; minutes are unknown until an analyst resolves them",
                "conflicts": [
                    {
                        "stations": [c["station_a"], c["station_b"]],
                        "start": iso(c["start"]),
                        "end": iso(c["end"]),
                        "minutes": c["minutes"],
                    }
                    for c in resolution["pending_conflicts"]
                ],
                "imputed_zero": False,
            }
        if fp.exclusion_code is None and not analysis["completeness"]["satisfied"]:
            fp.exclusion_code = "incomplete_diary"
            fp.exclusion_detail = {
                "reason": "pre-declared complete-recall condition not met; selection is independent of program performance",
                "policy": analysis["completeness"]["policy"],
                "complete_days": analysis["completeness"]["complete_days"],
                "per_day": analysis["completeness"]["per_day"],
            }
        if fp.exclusion_code is None:
            fp.eligible = True
            eligible += 1
        else:
            excluded[fp.exclusion_code] = excluded.get(fp.exclusion_code, 0) + 1

        cur.execute(
            """
            INSERT INTO run_persons
                (run_id, person_id, household_id, strata, strata_effective, age_band, region, sex,
                 inclusion_probability, base_weight, final_weight, eligible, exclusion_code,
                 exclusion_detail, diary_last_seq, completeness)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s,%s,%s,%s)
            """,
            (
                str(run_id),
                fp.person_id,
                fp.household_id,
                fp.strata,
                fp.strata_effective,
                fp.age_band,
                fp.region,
                fp.sex,
                fp.inclusion_probability,
                fp.base_weight,
                fp.eligible,
                fp.exclusion_code,
                jb(fp.exclusion_detail),
                analysis["reconstruction"]["last_submission_seq"],
                jb(analysis["completeness"]),
            ),
        )
        for rep in analysis["day_reports"]:
            cur.execute(
                """
                INSERT INTO run_person_days
                    (run_id, person_id, survey_day, window_start, window_end, covered_minutes,
                     listening_minutes, unknown_minutes, no_listening_minutes, uncovered_minutes, segments)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(run_id),
                    fp.person_id,
                    rep["survey_day"],
                    rep["window_start"],
                    rep["window_end"],
                    rep["covered_minutes"],
                    rep["listening_minutes"],
                    rep["unknown_minutes"],
                    rep["no_listening_minutes"],
                    rep["uncovered_minutes"],
                    jb(
                        {
                            k: [
                                {"start": s.isoformat(), "end": e.isoformat()}
                                for s, e in v
                            ]
                            for k, v in rep["segments"].items()
                        }
                    ),
                ),
            )
        for item in analysis["usage"]:
            if item.get("entry_id") is not None:
                cur.execute(
                    """
                    INSERT INTO run_diary_usage
                        (run_id, person_id, entry_id, submission_seq, disposition, reason, start_utc,
                         end_utc, station, source_serial, revision)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        str(run_id),
                        fp.person_id,
                        item["entry_id"],
                        item.get("submission_seq"),
                        item["disposition"],
                        item["reason"],
                        item.get("start_utc"),
                        item.get("end_utc"),
                        item.get("station"),
                        item.get("source_serial"),
                        item.get("revision"),
                    ),
                )

    payload = {
        "frame_persons": len(frame["persons"]),
        "with_diary": with_diary,
        "eligible_persons": eligible,
        "excluded_by_code": excluded,
        "diary_only_persons": frame["diary_only_persons"],
        "strata_merges_applied": frame["strata_merges"],
        "unknown_probability_persons": frame["unknown_probability_persons"],
        "formal_frame_possible": frame["formal_frame_possible"],
        "inclusion_probability_range": frame["probability_range"],
        "household_count": len({p.household_id for p in frame["persons"]}),
        "pending_conflicts": pending_conflicts,
        "missing_diary_treated_as_zero_listening": False,
        "strata": _stratum_diagnostics(cur, run_id),
    }
    return payload


def _stratum_diagnostics(cur, run_id) -> dict[str, Any]:
    """Per-stratum sample counts, so an empty stratum is visible up front."""
    cur.execute(
        """
        SELECT strata_effective,
               COUNT(*) AS frame_persons,
               COUNT(*) FILTER (WHERE eligible) AS eligible,
               COUNT(DISTINCT household_id) FILTER (WHERE eligible) AS eligible_households
          FROM run_persons WHERE run_id=%s GROUP BY strata_effective ORDER BY strata_effective
        """,
        (str(run_id),),
    )
    rows = cur.fetchall()
    empty = [r["strata_effective"] for r in rows if r["eligible"] == 0]
    return {
        "by_stratum": {
            r["strata_effective"]: {
                "frame_persons": r["frame_persons"],
                "eligible_persons": r["eligible"],
                "eligible_households": r["eligible_households"],
            }
            for r in rows
        },
        "strata_without_eligible_sample": empty,
        "note": (
            "An empty stratum is reported and left empty. No synthetic respondent is added to make "
            "calibration succeed."
        ),
    }


# ---------------------------------------------------------------------------
# Program exposure
# ---------------------------------------------------------------------------
def _eligible_rows(cur, run_id) -> list[dict[str, Any]]:
    cur.execute(
        """
        SELECT person_id, household_id, strata, strata_effective, age_band, region, sex,
               base_weight, final_weight
          FROM run_persons WHERE run_id=%s AND eligible ORDER BY person_id
        """,
        (str(run_id),),
    )
    return cur.fetchall()


def _stage_program_exposure(db, conn, cur, run_id) -> dict[str, Any]:
    run = _run_row(cur, run_id)
    config = _config(run)
    inputs = _inputs(cur, run_id)
    tz = _tz(run)
    programs = load_programs(cur, inputs["program"]["batch_seq"])
    program_map: dict[str, dict[str, Any]] = {}
    for a in programs["airings"]:
        program_map.setdefault(a["program_key"], {"title": a["title"], "airings": []})["airings"].append(a)

    recruitment = load_recruitment(cur, inputs["recruitment"]["batch_seq"])
    submissions = load_submissions(cur, int(inputs["diary"]["seq_cutoff"]))
    person_ids = [r["person_id"] for r in recruitment["rows"]]
    resolutions = _load_resolutions(cur, person_ids)
    expected = survey_days(recruitment["survey_period_start"], recruitment["survey_period_end"])
    windows = day_windows(expected, config, tz)
    threshold = float(config["thresholds"]["reach_minutes"])

    rows = _eligible_rows(cur, run_id)
    cur.execute("DELETE FROM run_person_program WHERE run_id=%s", (str(run_id),))
    listening_rows = 0
    person_program: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        subs = submissions.get(row["person_id"], [])
        analysis = analyze_person(
            _frame_stub(row), subs, expected, config, resolutions, tz
        )
        resolution = analysis["resolved"]
        exposure = program_exposure(
            resolution["station_intervals"],
            resolution["unknown_ranges"],
            resolution["confirmed_nonlisten_ranges"],
            program_map,
            expected,
            windows,
            threshold,
        )
        person_program[row["person_id"]] = exposure
        for key, item in exposure.items():
            if item["minutes"] > 0:
                listening_rows += 1
            cur.execute(
                """
                INSERT INTO run_person_program
                    (run_id, person_id, program_key, minutes, reached, by_day, by_airing)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                """,
                (
                    str(run_id),
                    row["person_id"],
                    key,
                    item["minutes"],
                    item["reached"],
                    jb(item["by_day_ranges"]),
                    jb(item["by_airing"]),
                ),
            )
    return {
        "programs": len(program_map),
        "person_program_rows_with_listening": listening_rows,
        "eligible_persons": len(rows),
        "reach_threshold_minutes": threshold,
    }


def _frame_stub(row) -> Any:
    from .analysis import FramePerson

    return FramePerson(
        person_id=row["person_id"],
        household_id=row["household_id"],
        strata=row["strata"],
        strata_effective=row["strata_effective"],
        age_band=row["age_band"],
        region=row["region"],
        sex=row["sex"],
        inclusion_probability=None,
        base_weight=float(row["base_weight"]) if row["base_weight"] is not None else None,
        eligible=True,
    )


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------
def _raking_config(config: dict[str, Any]) -> RakingConfig:
    r = config["raking"]
    return RakingConfig(
        tolerance=float(r["tolerance"]),
        max_iterations=int(r["max_iterations"]),
        lower_factor=float(r["lower_factor"]),
        upper_factor=float(r["upper_factor"]),
        dimensions=tuple(r["dimensions"]),
    )


def _stage_calibrate(db, conn, cur, run_id) -> dict[str, Any]:
    run = _run_row(cur, run_id)
    config = _config(run)
    inputs = _inputs(cur, run_id)
    targets = load_targets(cur, inputs["target"]["batch_seq"])
    margins = loads(targets["dimensions"])
    rows = _eligible_rows(cur, run_id)

    unknown = [r["person_id"] for r in rows if r["base_weight"] is None]
    if unknown:
        raise invalid(
            "inclusion_probability_unknown",
            "a formal population estimate requires a known inclusion probability for every included person",
            {"persons": unknown},
        )
    cur.execute("DELETE FROM run_calibration_cells WHERE run_id=%s", (str(run_id),))
    cur.execute("DELETE FROM run_calibration_rounds WHERE run_id=%s", (str(run_id),))

    if not rows:
        summary = {
            "status": "sample_empty",
            "success": False,
            "reason": "no eligible persons; residuals are not resampled",
            "empty_cells": [],
        }
        cur.execute("UPDATE runs SET calibration=%s, updated_at=now() WHERE run_id=%s", (jb(summary), str(run_id)))
        return summary

    persons = [
        {
            "age_band": r["age_band"],
            "region": r["region"],
            "sex": r["sex"],
            "strata_effective": r["strata_effective"],
        }
        for r in rows
    ]
    base = [float(r["base_weight"]) for r in rows]
    rc = _raking_config(config)
    result = rake(persons, base, margins, rc)
    _persist_calibration(cur, run_id, result, rounds_replicate=-1)
    if result.success:
        for row, w in zip(rows, result.weights):
            cur.execute(
                "UPDATE run_persons SET final_weight=%s WHERE run_id=%s AND person_id=%s",
                (round(w, 8), str(run_id), row["person_id"]),
            )
    summary = result.summary()
    summary.update(
        {
            "tolerance": rc.tolerance,
            "max_iterations": rc.max_iterations,
            "lower_factor": rc.lower_factor,
            "upper_factor": rc.upper_factor,
            "bound_scale": "per-person, relative to that person's design weight (1/p)",
            "mean_base_weight": round(result.mean_base_weight, 6),
            "weight_range": (
                {
                    "min": round(min(result.weights), 6),
                    "max": round(max(result.weights), 6),
                }
                if result.weights
                else None
            ),
            "publishable": bool(result.success),
        }
    )
    cur.execute("UPDATE runs SET calibration=%s, updated_at=now() WHERE run_id=%s", (jb(summary), str(run_id)))
    return summary


def _persist_calibration(cur, run_id, result, rounds_replicate: int) -> None:
    cur.execute(
        "DELETE FROM run_calibration_cells WHERE run_id=%s AND replicate=%s",
        (str(run_id), rounds_replicate),
    )
    for cell in result.cells:
        cur.execute(
            """
            INSERT INTO run_calibration_cells
                (run_id, replicate, dimension, category, target, initial_sum, final_sum,
                 residual, relative_residual, clamped_persons)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                str(run_id),
                rounds_replicate,
                cell["dimension"],
                cell["category"],
                cell["target"],
                cell["initial_sum"],
                cell["final_sum"],
                cell["residual"],
                cell["relative_residual"],
                cell.get("clamped_persons", 0),
            ),
        )
    for rnd in result.rounds:
        cur.execute(
            """
            INSERT INTO run_calibration_rounds (run_id, replicate, round_no, max_rel_residual, clamped_persons)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            """,
            (str(run_id), rounds_replicate, rnd["round_no"], rnd["max_rel_residual"], rnd["clamped_persons"]),
        )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def _stage_bootstrap(db, conn, cur, run_id) -> dict[str, Any]:
    run = _run_row(cur, run_id)
    config = _config(run)
    inputs = _inputs(cur, run_id)
    targets = load_targets(cur, inputs["target"]["batch_seq"])
    margins = loads(targets["dimensions"])
    rows = _eligible_rows(cur, run_id)
    requested = int(config["bootstrap"]["replicates"])
    failure_limit = float(config["bootstrap"]["max_calibration_failure_rate"])

    cur.execute("DELETE FROM run_bootstrap_replicates WHERE run_id=%s", (str(run_id),))
    cur.execute("DELETE FROM run_calibration_replicates WHERE run_id=%s", (str(run_id),))
    if not rows:
        summary = {
            "requested": requested,
            "success": 0,
            "failed": requested,
            "failure_rate": 1.0,
            "failure_codes": {"sample_empty": requested},
            "max_calibration_failure_rate": failure_limit,
            "intervals_allowed": False,
            "interval_status": "suppressed_no_eligible_sample",
            "resampling_unit": "household",
        }
        cur.execute("UPDATE runs SET bootstrap=%s, interval_status=%s, updated_at=now() WHERE run_id=%s",
                    (jb(summary), summary["interval_status"], str(run_id)))
        return summary

    households_by_stratum: dict[str, dict[str, list[int]]] = {}
    for idx, r in enumerate(rows):
        households_by_stratum.setdefault(r["strata_effective"], {}).setdefault(r["household_id"], []).append(idx)
    bh = {s: [members for _, members in sorted(h.items())] for s, h in households_by_stratum.items()}
    reps = stratified_household_bootstrap(bh, requested, int(run["seed"]))
    persons = [
        {"age_band": r["age_band"], "region": r["region"], "sex": r["sex"]}
        for r in rows
    ]
    base = [float(r["base_weight"]) for r in rows]
    rc = _raking_config(config)
    threshold = float(config["thresholds"]["reach_minutes"])

    # Pull each person's program minutes once.
    cur.execute("SELECT person_id, program_key, minutes FROM run_person_program WHERE run_id=%s", (str(run_id),))
    pmap: dict[str, dict[str, float]] = {}
    for r in cur.fetchall():
        pmap.setdefault(r["person_id"], {})[r["program_key"]] = float(r["minutes"])

    successes = 0
    failures: dict[str, int] = {}
    for rep_no, picks in enumerate(reps):
        sub_persons = [persons[i] for i in picks]
        sub_base = [base[i] for i in picks]
        res = rake(sub_persons, sub_base, margins, rc)
        if not res.success:
            failures[res.status] = failures.get(res.status, 0) + 1
            cur.execute(
                """
                INSERT INTO run_bootstrap_replicates (run_id, replicate, success, failure_code, results)
                VALUES (%s,%s,false,%s,%s)
                """,
                (str(run_id), rep_no, res.status, jb(res.summary())),
            )
            cur.execute(
                """
                INSERT INTO run_calibration_replicates
                    (run_id, replicate, success, status, rounds, max_rel_residual, weights, cells)
                VALUES (%s,%s,false,%s,%s,%s,%s,%s)
                """,
                (
                    str(run_id), rep_no, res.status, len(res.rounds), res.max_rel_residual,
                    jb([]), jb(res.cells),
                ),
            )
            continue
        successes += 1
        total_w = sum(res.weights)
        prog_min: dict[str, float] = {}
        prog_listener_w: dict[str, float] = {}
        prog_reach: dict[str, float] = {}
        # Replicate estimates per (program, dimension, category). These are what
        # a legitimate within-program subgroup interval is built from; they are
        # never used to rank programs against each other.
        domain_stats: dict[str, dict[str, dict[str, dict[str, float]]]] = {}
        for idx, person_idx in enumerate(picks):
            row = rows[person_idx]
            pid = row["person_id"]
            w = res.weights[idx]
            for key, minutes in pmap.get(pid, {}).items():
                prog_min[key] = prog_min.get(key, 0.0) + w * minutes
                reached = minutes >= threshold
                if reached:
                    prog_listener_w[key] = prog_listener_w.get(key, 0.0) + w
                    prog_reach[key] = prog_reach.get(key, 0.0) + w
                for dim in ("age_band", "region", "sex"):
                    cell = (
                        domain_stats.setdefault(key, {})
                        .setdefault(dim, {})
                        .setdefault(
                            row[dim],
                            {"w": 0.0, "minutes": 0.0, "reached": 0.0, "n": 0},
                        )
                    )
                    cell["w"] += w
                    cell["minutes"] += w * minutes
                    cell["reached"] += w if reached else 0.0
                    cell["n"] += 1
        estimates = {}
        for key in prog_min:
            m = prog_min[key]
            lw = prog_listener_w.get(key, 0.0)
            domain_rendered = {}
            for dim, cats in domain_stats.get(key, {}).items():
                domain_rendered[dim] = {
                    cat: {
                        "mean_minutes_per_person": round(v["minutes"] / v["w"], 6) if v["w"] else 0.0,
                        "reach_persons": round(v["reached"], 6),
                        "reach_rate": round(v["reached"] / v["w"], 6) if v["w"] else 0.0,
                        "listener_cell_weight": round(v["reached"], 6),
                    }
                    for cat, v in cats.items()
                }
            estimates[key] = {
                "mean_minutes_per_person": round(m / total_w, 6) if total_w else 0.0,
                "mean_minutes_per_listener": round(m / lw, 6) if lw else None,
                "reach_persons": round(prog_reach.get(key, 0.0), 6),
                "reach_rate": round(prog_reach.get(key, 0.0) / total_w, 6) if total_w else 0.0,
                "domain": domain_rendered,
            }
        cur.execute(
            "INSERT INTO run_bootstrap_replicates (run_id, replicate, success, results) VALUES (%s,%s,true,%s)",
            (str(run_id), rep_no, jb(estimates)),
        )
        cur.execute(
            """
            INSERT INTO run_calibration_replicates
                (run_id, replicate, success, status, rounds, max_rel_residual, weights, cells)
            VALUES (%s,%s,true,%s,%s,%s,%s,%s)
            """,
            (
                str(run_id), rep_no, res.status, len(res.rounds), res.max_rel_residual,
                jb([round(w, 8) for w in res.weights]), jb(res.cells),
            ),
        )

    failure_rate = (requested - successes) / requested if requested else 1.0
    intervals_allowed = requested > 0 and failure_rate <= failure_limit
    summary = {
        "requested": requested,
        "success": successes,
        "failed": requested - successes,
        "failure_rate": round(failure_rate, 6),
        "failure_codes": failures,
        "max_calibration_failure_rate": failure_limit,
        "households": sum(len(v) for v in bh.values()),
        "households_by_stratum": {s: len(v) for s, v in bh.items()},
        "seed": int(run["seed"]),
        "intervals_allowed": intervals_allowed,
        "interval_status": None if intervals_allowed else "suppressed_calibration_failure_rate",
        "resampling_unit": "household",
        "resampling_design": "with_replacement_within_effective_stratum",
        "replicate_household_draws": len(reps[0]) // max(1, len(bh)) if reps else 0,
        "duplicate_identifier_copies_deduplicated": False,
    }
    cur.execute(
        "UPDATE runs SET bootstrap=%s, interval_status=%s, updated_at=now() WHERE run_id=%s",
        (jb(summary), summary["interval_status"], str(run_id)),
    )
    return summary


# ---------------------------------------------------------------------------
# Estimation
# ---------------------------------------------------------------------------
def _domain_weight_totals(rows: list[dict[str, Any]]) -> dict[tuple[str, str], float]:
    totals: dict[tuple[str, str], float] = {}
    for r in rows:
        w = float(r["final_weight"] or 0.0)
        for dim in ("age_band", "region", "sex"):
            totals[(dim, r[dim])] = totals.get((dim, r[dim]), 0.0) + w
    return totals


def _by_domain(
    rows: list[dict[str, Any]],
    pp: dict[str, dict[str, dict[str, Any]]],
    weights: dict[str, float],
    program_key: str,
    threshold: float,
    domain_totals: dict[tuple[str, str], float],
) -> dict[str, Any]:
    """Weighted estimate per domain category, with coverage diagnostics.

    ``unweighted_n`` is reported alongside, because a weighted number built on
    a handful of diarists is not evidence of population coverage.
    """
    out: dict[str, Any] = {}
    for dim in ("age_band", "region", "sex"):
        cats: dict[str, dict[str, float]] = {}
        for r in rows:
            cat = r[dim]
            bucket = cats.setdefault(
                cat,
                {"w": 0.0, "minutes": 0.0, "reached_w": 0.0, "n": 0, "listeners": 0},
            )
            w = weights.get(r["person_id"], 0.0)
            item = pp.get(r["person_id"], {}).get(program_key)
            minutes = item["minutes"] if item else 0.0
            bucket["w"] += w
            bucket["minutes"] += w * minutes
            bucket["n"] += 1
            if minutes >= threshold:
                bucket["reached_w"] += w
                bucket["listeners"] += 1
        rendered = {}
        for cat, b in sorted(cats.items()):
            total_w = b["w"]
            rendered[cat] = {
                "reach_persons": round(b["reached_w"], 3),
                "reach_rate": round(b["reached_w"] / total_w, 6) if total_w else None,
                "mean_minutes_per_person": round(b["minutes"] / total_w, 3) if total_w else None,
                "mean_minutes_per_listener": round(b["minutes"] / b["reached_w"], 3)
                if b["reached_w"]
                else None,
                "unweighted_n": b["n"],
                "listener_unweighted_n": b["listeners"],
                "sample_weight": round(total_w, 3),
                "population_target": round(domain_totals.get((dim, cat), 0.0), 3),
                "reliability": "low" if b["n"] < 5 else "adequate",
            }
        out[dim] = rendered
    return out


def _persist_coverage_gaps(cur, run_id, config, rows, weights) -> list[dict[str, Any]]:
    """Assess which target groups the achieved diary sample under-represents.

    Coverage is measured on the *unweighted* analysis sample, not on the
    calibrated weights. Calibration makes the weighted totals agree with the
    margins by construction, so it can never reveal that a group largely
    failed to return diaries. What matters for "which groups are we missing"
    is how many eligible diarists each group actually contributed.
    """
    inputs = _inputs(cur, run_id)
    targets = load_targets(cur, inputs["target"]["batch_seq"])
    margins = loads(targets["dimensions"])
    min_cell = int(config["thresholds"]["min_cell_persons"])
    ratio_floor = float(config["thresholds"]["under_representation_ratio"])

    sample_n: dict[tuple[str, str], int] = {}
    for r in rows:
        for dim in ("age_band", "region", "sex"):
            key = (dim, r[dim])
            sample_n[key] = sample_n.get(key, 0) + 1
    sample_total = len(rows)

    # Effective sample contribution: Kish-style share of the calibrated weights
    # per cell, so a group propped up entirely by very large weights is visible.
    weight_share: dict[tuple[str, str], float] = {}
    total_weight = sum(float(weights.get(r["person_id"], 0.0)) for r in rows)
    if total_weight:
        for r in rows:
            w = float(weights.get(r["person_id"], 0.0))
            for dim in ("age_band", "region", "sex"):
                key = (dim, r[dim])
                weight_share[key] = weight_share.get(key, 0.0) + w / total_weight

    cur.execute("DELETE FROM run_coverage_gaps WHERE run_id=%s", (str(run_id),))
    for dim, cats in margins.items():
        dim_target = sum(float(v) for v in cats.values())
        for cat, target in cats.items():
            n = sample_n.get((dim, cat), 0)
            sample_share = n / sample_total if sample_total else 0.0
            target_share = float(target) / dim_target if dim_target else 0.0
            ratio = sample_share / target_share if target_share else 0.0
            if n == 0:
                status = "no_sample"
            elif ratio < ratio_floor or n < min_cell:
                status = "undercovered"
            else:
                status = "acceptable"
            payload = {
                "dimension": dim,
                "category": cat,
                "population_target": float(target),
                "population_share": round(target_share, 6),
                "sample_persons": n,
                "sample_share": round(sample_share, 6),
                "representation_ratio": round(ratio, 6),
                "ratio_floor": ratio_floor,
                "calibrated_weight_share": round(weight_share.get((dim, cat), 0.0), 6),
                "min_cell_persons": min_cell,
                "status": status,
                "comparison_warning": "do not compare program performance across groups",
                "note": (
                    "Coverage is judged on eligible diarists, not on calibrated weights: "
                    "raking forces weighted totals onto the margins and would otherwise hide a group that never returned diaries."
                ),
            }
            cur.execute(
                "INSERT INTO run_coverage_gaps (run_id, dimension, category, payload) VALUES (%s,%s,%s,%s)",
                (str(run_id), dim, cat, jb(payload)),
            )
    cur.execute(
        "SELECT payload FROM run_coverage_gaps WHERE run_id=%s AND payload->>'status' <> 'acceptable'",
        (str(run_id),),
    )
    return [loads(r["payload"]) for r in cur.fetchall()]


def _stage_estimate(db, conn, cur, run_id) -> dict[str, Any]:
    run = _run_row(cur, run_id)
    config = _config(run)
    inputs = _inputs(cur, run_id)
    calibration = loads(run["calibration"]) if run["calibration"] else None
    bootstrap = loads(run["bootstrap"]) if run["bootstrap"] else {}
    threshold = float(config["thresholds"]["reach_minutes"])

    if calibration is None or not calibration.get("success"):
        cur.execute(
            "UPDATE runs SET status='rejected', failure=%s, finished_at=now(), updated_at=now() WHERE run_id=%s",
            (
                jb(
                    {
                        "code": "calibration_not_successful",
                        "message": "calibration residuals remain; report is not published",
                        "calibration": calibration,
                    }
                ),
                str(run_id),
            ),
        )
        cur.execute(
            "UPDATE run_stages SET status='done', finished_at=now(), payload=%s WHERE run_id=%s AND stage='estimate'",
            (
                jb(
                    {
                        "published": False,
                        "reason": "calibration_not_successful",
                        "calibration_status": calibration.get("status") if calibration else None,
                    }
                ),
                str(run_id),
            ),
        )
        conn.commit()
        return {
            "published": False,
            "reason": "calibration_not_successful",
            "calibration": calibration,
        }

    rows = _eligible_rows(cur, run_id)
    cur.execute(
        "SELECT person_id, program_key, minutes, by_day, by_airing, reached FROM run_person_program WHERE run_id=%s",
        (str(run_id),),
    )
    pp: dict[str, dict[str, dict[str, Any]]] = {}
    for r in cur.fetchall():
        pp.setdefault(r["person_id"], {})[r["program_key"]] = {
            "minutes": float(r["minutes"]),
            "by_day": loads(r["by_day"]),
            "by_airing": loads(r["by_airing"]),
            "reached": r["reached"],
        }
    weights = {r["person_id"]: float(r["final_weight"]) for r in rows}
    ordered = [r["person_id"] for r in rows]
    input_rows = [
        {"by_program": {k: v["minutes"] for k, v in pp.get(pid, {}).items()}} for pid in ordered
    ]
    wlist = [weights[pid] for pid in ordered]
    estimates = point_estimates(input_rows, wlist, threshold)
    domain_totals = _domain_weight_totals(rows)
    by_domain_map = {
        key: _by_domain(rows, pp, weights, key, threshold, domain_totals) for key in estimates
    }

    cur.execute(
        "SELECT program_key, MIN(title) AS title FROM program_airings WHERE batch_seq=%s GROUP BY 1",
        (inputs["program"]["batch_seq"],),
    )
    titles = {r["program_key"]: r["title"] for r in cur.fetchall()}

    intervals: dict[str, Any] = {}
    if bootstrap.get("intervals_allowed"):
        cur.execute(
            "SELECT results FROM run_bootstrap_replicates WHERE run_id=%s AND success ORDER BY replicate",
            (str(run_id),),
        )
        reach_rep: dict[str, list[float]] = {}
        mean_rep: dict[str, list[float]] = {}
        listener_rep: dict[str, list[float]] = {}
        domain_rep: dict[tuple[str, str, str, str], list[float]] = {}
        domain_reach_rep: dict[tuple[str, str, str, str], list[float]] = {}
        rep_count = 0
        for r in cur.fetchall():
            data = loads(r["results"]) or {}
            rep_count += 1
            for key, val in data.items():
                reach_rep.setdefault(key, []).append(val["reach_persons"])
                mean_rep.setdefault(key, []).append(val["mean_minutes_per_person"])
                if val["mean_minutes_per_listener"] is not None:
                    listener_rep.setdefault(key, []).append(val["mean_minutes_per_listener"])
                for dim, cats in (val.get("domain") or {}).items():
                    for cat, cell in cats.items():
                        dkey = (key, dim, cat)
                        domain_rep.setdefault(dkey, []).append(cell["mean_minutes_per_person"])
                        domain_reach_rep.setdefault(dkey, []).append(cell["reach_persons"])
        for key in estimates:
            domain_intervals: dict[str, dict[str, Any]] = {}
            for dim, cats in by_domain_map.get(key, {}).items():
                domain_intervals[dim] = {}
                for cat in cats:
                    dkey = (key, dim, cat)
                    domain_intervals[dim][cat] = {
                        "mean_minutes_per_person": percentile_interval(domain_rep.get(dkey, [])),
                        "reach_persons": percentile_interval(domain_reach_rep.get(dkey, [])),
                    }
            intervals[key] = {
                "reach_persons": percentile_interval(reach_rep.get(key, [])),
                "mean_minutes_per_person": percentile_interval(mean_rep.get(key, [])),
                "mean_minutes_per_listener": percentile_interval(listener_rep.get(key, [])),
                "by_domain": domain_intervals,
                "replicates_used": rep_count,
                "level": 0.95,
                "method": "percentile_household_bootstrap_recalibrated",
            }

    cur.execute("DELETE FROM run_program_estimates WHERE run_id=%s", (str(run_id),))
    domain_totals = _domain_weight_totals(rows)
    for key, est in estimates.items():
        est["title"] = titles.get(key, key)
        if key in intervals:
            est["interval_available"] = True
        else:
            est["interval_available"] = False
            est["interval_suppressed_reason"] = bootstrap.get("interval_status") or "bootstrap_not_run"
        est["by_domain"] = by_domain_map.get(key, {})
        by_domain_payload = est.pop("by_domain")
        cur.execute(
            """
            INSERT INTO run_program_estimates
                (run_id, program_key, title, estimates, intervals, by_domain, lineage_summary)
            VALUES (%s,%s,%s,%s,%s,%s,%s)
            """,
            (
                str(run_id),
                key,
                est["title"],
                jb(est),
                jb(intervals.get(key)),
                jb(by_domain_payload),
                jb(
                    {
                        "eligible_persons": len(ordered),
                        "diary_seq_cutoff": inputs["diary"]["seq_cutoff"],
                        "seed": int(run["seed"]),
                        "calibration_status": calibration["status"],
                        "reach_threshold_minutes": threshold,
                        "bootstrap": {
                            "requested": bootstrap.get("requested"),
                            "success": bootstrap.get("success"),
                            "failure_rate": bootstrap.get("failure_rate"),
                            "intervals_allowed": bootstrap.get("intervals_allowed"),
                        },
                    }
                ),
            ),
        )
    intervals_emitted = bool(intervals)
    _persist_coverage_gaps(cur, run_id, config, rows, weights)
    cur.execute(
        "UPDATE runs SET interval_status=%s, updated_at=now() WHERE run_id=%s",
        (None if intervals_emitted else (bootstrap.get("interval_status") or "no_bootstrap"), str(run_id)),
    )
    return {
        "programs_estimated": len(estimates),
        "intervals_emitted": intervals_emitted,
        "interval_status": None if intervals_emitted else (bootstrap.get("interval_status") or "no_bootstrap"),
        "bootstrap": bootstrap,
    }


# ---------------------------------------------------------------------------
# Report publication
# ---------------------------------------------------------------------------
def _stage_publish(db, conn, cur, run_id) -> dict[str, Any]:
    from .report import build_report

    run = _run_row(cur, run_id)
    if run["status"] == "rejected":
        raise invalid("run_rejected", "rejected runs cannot publish a report")

    cur.execute("SELECT COUNT(*) AS c FROM run_program_estimates WHERE run_id=%s", (str(run_id),))
    if cur.fetchone()["c"] == 0:
        raise invalid("no_estimates", "nothing to publish")

    report = build_report(cur, run_id)
    report_hash = canonical_hash(report)
    cur.execute(
        """
        INSERT INTO run_reports (run_id, report, report_hash) VALUES (%s,%s,%s)
        ON CONFLICT (run_id) DO NOTHING
        """,
        (str(run_id), jb(report), report_hash),
    )
    cur.execute(
        "UPDATE runs SET status='published', published_at=now(), updated_at=now() WHERE run_id=%s",
        (str(run_id),),
    )
    cur.execute(
        "INSERT INTO audit_events (actor, action, entity, entity_id, details) VALUES ('system','run.publish','run',%s,%s)",
        (str(run_id), jb({"report_hash": report_hash})),
    )
    return {"report_hash": report_hash, "published": True}
