"""FastAPI application for the audience estimation service."""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import ingest
from .config import Settings, get_settings
from .db import Database
from .errors import ServiceError, conflict, invalid, not_found
from .jsonutil import loads
from .models import (
    DecisionNoteIn,
    DiarySubmissionIn,
    ExecuteIn,
    ProgramBatchIn,
    PublicationRequestIn,
    RecruitmentBatchIn,
    RunCreateIn,
    TargetBatchIn,
)
from .runs import create_run, execute_run, fetch_run
from .timeutil import iso


def _error(status: int, code: str, message: str, details: Any = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message, "details": details}},
    )


def create_app(settings: Settings | None = None, db: Database | None = None) -> FastAPI:
    settings = settings or get_settings()
    owns_db = db is None
    db = db or Database(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        db.open()
        yield
        if owns_db:
            db.close()

    app = FastAPI(
        title="Broadcast Audience Estimation Service",
        version="1.0.0",
        description=(
            "Weighted program reach and mean listening minutes from fictional household "
            "recruitment and personal diaries. Calibration is bounded raking; uncertainty "
            "comes from a household bootstrap that is recalibrated on every replicate. "
            "Missing diary information is never imputed as no listening, and cross-program "
            "performance comparison is explicitly unsupported."
        ),
        lifespan=lifespan,
    )
    app.state.db = db
    app.state.settings = settings

    @app.exception_handler(ServiceError)
    async def _service_error(request: Request, exc: ServiceError):
        return _error(exc.status, exc.code, exc.message, exc.details)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        return _error(422, "request_invalid", "request body failed validation", jsonable_encoder(exc.errors()))

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        if isinstance(exc, ServiceError):
            return await _service_error(request, exc)
        from .timeutil import TimeRangeError as TRE

        if isinstance(exc, TRE):
            return _error(422, "time_range_invalid", str(exc))
        return _error(500, "internal_error", "unexpected server error", {"type": type(exc).__name__})

    def require_key(x_api_key: str | None = Header(default=None), role: str = "any") -> None:
        if x_api_key is None or x_api_key.strip() == "":
            raise invalid("api_key_required", "provide the X-API-Key header")
        if x_api_key == settings.admin_api_key:
            return
        if x_api_key == settings.analyst_api_key:
            if role == "any":
                return
            return
        raise invalid("api_key_invalid", "unknown API key")

    def analyst(x_api_key: str | None = Header(default=None)) -> str:
        require_key(x_api_key, role="any")
        return "analyst" if x_api_key == settings.analyst_api_key else "admin"

    def director(x_api_key: str | None = Header(default=None)) -> str:
        require_key(x_api_key, role="any")
        if x_api_key != settings.admin_api_key:
            raise ServiceError(
                403,
                "director_key_required",
                "this action is reserved to the research director (admin API key)",
            )
        return "director"

    # ---------------- meta ----------------
    @app.get("/health", tags=["meta"])
    def health():
        with db.scoped() as conn:
            conn.cursor().execute("SELECT 1")
        return {"status": "ok", "schema": db.schema, "database": "reachable"}

    @app.get("/v1/meta/config", tags=["meta"])
    def meta_config(actor: str = Depends(analyst)):
        return {
            "service_timezone": settings.service_timezone,
            "dimensions": ["age_band", "region", "sex"],
            "diary_window_default": {"start_local": "00:00", "end_local": "24:00"},
            "calibration": {
                "method": "bounded_raking",
                "dimensions_raced": ["age_band", "region", "sex"],
                "bounds": "per person: [0.3 x design weight, 4.0 x design weight]",
                "design_weight": "1 / inclusion_probability",
            },
            "bootstrap": {
                "design": "whole household, with replacement, within effective stratum, recalibrated each replicate",
                "deduplicate_identical_ids": False,
                "interval": "95% percentile",
            },
            "policies": {
                "missing_diary": "excluded person, never zero listening",
                "uncovered_diary_window": "unknown, never zero listening",
                "cross_station_overlap": "pending conflict, never resolved by upload order",
                "cross_program_comparison": "not supported",
            },
        }

    # ---------------- ingestion ----------------
    @app.post("/v1/recruitment/batches", status_code=201, tags=["ingestion"])
    def post_recruitment(body: RecruitmentBatchIn, actor: str = Depends(analyst)):
        payload = body.model_dump(mode="json")
        return ingest.create_recruitment_batch(db, payload, actor)

    @app.get("/v1/recruitment/batches", tags=["ingestion"])
    def list_recruitment(actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT batch_seq, batch_ref, survey_period_start, survey_period_end, row_count, received_at FROM recruitment_batches ORDER BY batch_seq"
            )
            return [
                {**r, "survey_period_start": iso(r["survey_period_start"]), "survey_period_end": iso(r["survey_period_end"]), "received_at": iso(r["received_at"])}
                for r in cur.fetchall()
            ]

    @app.post("/v1/targets/batches", status_code=201, tags=["ingestion"])
    def post_targets(body: TargetBatchIn, actor: str = Depends(analyst)):
        return ingest.create_target_batch(db, body.model_dump(mode="json"), actor)

    @app.get("/v1/targets/batches", tags=["ingestion"])
    def list_targets(actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT batch_seq, population_total, dimension_totals, received_at FROM target_batches ORDER BY batch_seq"
            )
            return [
                {**r, "dimension_totals": loads(r["dimension_totals"]), "received_at": iso(r["received_at"])}
                for r in cur.fetchall()
            ]

    @app.post("/v1/programs/batches", status_code=201, tags=["ingestion"])
    def post_programs(body: ProgramBatchIn, actor: str = Depends(analyst)):
        return ingest.create_program_batch(db, body.model_dump(mode="json"), actor)

    @app.get("/v1/programs/batches", tags=["ingestion"])
    def list_programs(actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT batch_seq, timezone, row_count, received_at FROM program_batches ORDER BY batch_seq"
            )
            return [
                {**r, "received_at": iso(r["received_at"])} for r in cur.fetchall()
            ]

    @app.get("/v1/programs/batches/{batch_seq}", tags=["ingestion"])
    def get_programs(batch_seq: int, actor: str = Depends(analyst)):
        with db.scoped() as conn:
            out = ingest.load_programs(conn.cursor(), batch_seq)
        return jsonable_encoder(out)

    @app.post("/v1/diary/submissions", status_code=201, tags=["ingestion"])
    def post_diary(body: DiarySubmissionIn, actor: str = Depends(analyst)):
        from zoneinfo import ZoneInfo

        payload = body.model_dump(mode="json")
        return ingest.create_diary_submission(db, payload, actor, ZoneInfo(settings.service_timezone))

    @app.get("/v1/diary/submissions", tags=["ingestion"])
    def list_diary(
        person_id: str | None = Query(default=None),
        actor: str = Depends(analyst),
    ):
        with db.scoped() as conn:
            cur = conn.cursor()
            if person_id:
                cur.execute(
                    "SELECT submission_seq, person_id, household_id, revision, replacement_scope, submitted_at FROM diary_submissions WHERE person_id=%s ORDER BY submission_seq",
                    (person_id,),
                )
            else:
                cur.execute(
                    "SELECT submission_seq, person_id, household_id, revision, replacement_scope, submitted_at FROM diary_submissions ORDER BY submission_seq"
                )
            return [
                {**r, "replacement_scope": loads(r["replacement_scope"]), "submitted_at": iso(r["submitted_at"])}
                for r in cur.fetchall()
            ]

    @app.get("/v1/diary/cutoff", tags=["ingestion"])
    def diary_cutoff(actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COALESCE(MAX(submission_seq),0) AS m FROM diary_submissions")
            return {"available_max_submission_seq": cur.fetchone()["m"]}

    @app.get("/v1/diary/submissions/{submission_seq}", tags=["ingestion"])
    def get_diary(submission_seq: int, actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM diary_submissions WHERE submission_seq=%s", (submission_seq,))
            sub = cur.fetchone()
            if sub is None:
                raise not_found("diary_submission_not_found", "submission does not exist")
            cur.execute("SELECT * FROM diary_entries WHERE submission_seq=%s ORDER BY entry_id", (submission_seq,))
            entries = cur.fetchall()
        return jsonable_encoder(
            {
                **sub,
                "replacement_scope": loads(sub["replacement_scope"]),
                "coverage": loads(sub["coverage"]),
                "raw": loads(sub["raw"]),
                "entries": entries,
            }
        )

    # ---------------- runs ----------------
    @app.post("/v1/runs", status_code=201, tags=["runs"])
    def post_run(body: RunCreateIn, actor: str = Depends(analyst)):
        return create_run(db, settings, body.model_dump(mode="json"), actor)

    @app.get("/v1/runs", tags=["runs"])
    def list_runs(actor: str = Depends(analyst), limit: int = Query(default=50, le=500)):
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT run_id, status, seed, parent_run_id, note, created_at, published_at, interval_status
                  FROM runs ORDER BY created_at DESC LIMIT %s
                """,
                (limit,),
            )
            return [
                {
                    **r,
                    "run_id": str(r["run_id"]),
                    "parent_run_id": str(r["parent_run_id"]) if r["parent_run_id"] else None,
                    "created_at": iso(r["created_at"]),
                    "published_at": iso(r["published_at"]),
                }
                for r in cur.fetchall()
            ]

    @app.get("/v1/runs/{run_id}", tags=["runs"])
    def get_run(run_id: str, actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            run = fetch_run(cur, run_id)
            cur.execute(
                "SELECT stage, status, started_at, finished_at, payload, error FROM run_stages WHERE run_id=%s ORDER BY stage",
                (run_id,),
            )
            stages = [
                {
                    **s,
                    "started_at": iso(s["started_at"]),
                    "finished_at": iso(s["finished_at"]),
                    "payload": loads(s["payload"]),
                    "error": loads(s["error"]),
                }
                for s in cur.fetchall()
            ]
            cur.execute("SELECT kind, seq, payload FROM run_inputs WHERE run_id=%s", (run_id,))
            inputs = {r["kind"]: {"seq": r["seq"], "payload": loads(r["payload"])} for r in cur.fetchall()}
        return {**run, "stages": stages, "inputs": inputs}

    @app.post("/v1/runs/{run_id}/execute", tags=["runs"])
    def post_execute(run_id: str, body: ExecuteIn, actor: str = Depends(analyst)):
        return execute_run(db, run_id, body.max_steps)

    @app.get("/v1/runs/{run_id}/status", tags=["runs"])
    def run_status(run_id: str, actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            run = fetch_run(cur, run_id)
            cur.execute("SELECT stage, status FROM run_stages WHERE run_id=%s ORDER BY stage", (run_id,))
            return {
                "run_id": run["run_id"],
                "status": run["status"],
                "stages": cur.fetchall(),
                "calibration_status": (run["calibration"] or {}).get("status"),
                "bootstrap": run["bootstrap"],
                "interval_status": run["interval_status"],
            }

    @app.get("/v1/runs/{run_id}/report", tags=["reports"])
    def get_report(run_id: str, actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute("SELECT report, report_hash, published_at FROM run_reports WHERE run_id=%s", (run_id,))
            row = cur.fetchone()
            if row is None:
                raise not_found(
                    "report_not_published",
                    "no immutable report exists for this run (it may be rejected, failed, or still running)",
                )
            return {
                "run_id": run_id,
                "report_hash": row["report_hash"],
                "published_at": iso(row["published_at"]),
                "report": loads(row["report"]),
            }

    @app.get("/v1/runs/{run_id}/programs/{program_key}", tags=["lineage"])
    def program_lineage(run_id: str, program_key: str, actor: str = Depends(analyst)):
        from .lineage import program_lineage as build_lineage

        with db.scoped() as conn:
            return build_lineage(conn.cursor(), run_id, program_key)

    @app.get("/v1/runs/{run_id}/programs/{program_key}/sample", tags=["lineage"])
    def program_sample(
        run_id: str,
        program_key: str,
        actor: str = Depends(analyst),
        limit: int = Query(default=200, le=2000),
        offset: int = Query(default=0, ge=0),
    ):
        from .lineage import program_sample as build_sample

        with db.scoped() as conn:
            return build_sample(conn.cursor(), run_id, program_key, limit, offset)

    @app.get("/v1/runs/{run_id}/exclusions", tags=["lineage"])
    def exclusions(run_id: str, actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT person_id, exclusion_code, exclusion_detail FROM run_persons WHERE run_id=%s AND NOT eligible ORDER BY exclusion_code, person_id",
                (run_id,),
            )
            rows = cur.fetchall()
        grouped: dict[str, list[dict[str, Any]]] = {}
        for r in rows:
            grouped.setdefault(r["exclusion_code"] or "unknown", []).append(
                {"person_id": r["person_id"], "detail": loads(r["exclusion_detail"])}
            )
        return {
            "run_id": run_id,
            "total_excluded": len(rows),
            "by_code": {k: {"count": len(v), "persons": v} for k, v in grouped.items()},
        }

    @app.get("/v1/runs/{run_id}/calibration", tags=["lineage"])
    def calibration(run_id: str, actor: str = Depends(analyst), replicate: int | None = None):
        with db.scoped() as conn:
            cur = conn.cursor()
            run = fetch_run(cur, run_id)
            target_replicate = -1 if replicate is None else replicate
            cur.execute(
                "SELECT round_no, max_rel_residual, clamped_persons FROM run_calibration_rounds WHERE run_id=%s AND replicate=%s ORDER BY round_no",
                (run_id, target_replicate),
            )
            rounds = cur.fetchall()
            cur.execute(
                "SELECT dimension, category, target, initial_sum, final_sum, residual, relative_residual, clamped_persons FROM run_calibration_cells WHERE run_id=%s AND replicate=%s ORDER BY dimension, category",
                (run_id, target_replicate),
            )
            cells = cur.fetchall()
        return {
            "run_id": run_id,
            "summary": run["calibration"],
            "replicate": target_replicate,
            "rounds": rounds,
            "cells": cells,
        }

    @app.get("/v1/runs/{run_id}/bootstrap", tags=["lineage"])
    def bootstrap_detail(run_id: str, actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            run = fetch_run(cur, run_id)
            cur.execute(
                "SELECT replicate, success, failure_code FROM run_bootstrap_replicates WHERE run_id=%s ORDER BY replicate",
                (run_id,),
            )
            reps = cur.fetchall()
        failed = [r for r in reps if not r["success"]]
        return {
            "run_id": run_id,
            "summary": run["bootstrap"],
            "replicates": len(reps),
            "failed_replicates": [
                {"replicate": r["replicate"], "failure_code": r["failure_code"]} for r in failed
            ],
        }

    @app.get("/v1/runs/{run_id}/conflicts", tags=["lineage"])
    def conflicts(run_id: str, actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT conflict_id, person_id, station_a, station_b, start_utc, end_utc, entry_a, entry_b, status FROM run_conflicts WHERE run_id=%s ORDER BY person_id, start_utc",
                (run_id,),
            )
            rows = cur.fetchall()
        return [
            {**r, "start_utc": iso(r["start_utc"]), "end_utc": iso(r["end_utc"])} for r in rows
        ]

    @app.post("/v1/conflicts/resolutions", status_code=201, tags=["lineage"])
    def post_resolution(body: dict[str, Any], actor: str = Depends(analyst)):
        """Register an analyst decision. New runs use it; published reports never change."""
        required = ("person_id", "station_a", "station_b", "start", "end", "resolution")
        missing = [k for k in required if not body.get(k)]
        if missing:
            raise invalid("resolution_invalid", "missing fields", {"missing": missing})
        from .timeutil import parse_aware

        start = parse_aware(body["start"], "start")
        end = parse_aware(body["end"], "end")
        if end <= start:
            raise invalid("resolution_invalid", "end must be after start")
        resolution = body["resolution"]
        if resolution not in ("confirmed_station_a", "confirmed_station_b", "confirmed_neither", "dismissed"):
            raise invalid("resolution_invalid", "unknown resolution")
        with db.scoped() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO conflict_resolutions
                        (person_id, station_a, station_b, start_utc, end_utc, resolution, note, actor)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                    RETURNING resolution_id
                    """,
                    (
                        body["person_id"],
                        body["station_a"],
                        body["station_b"],
                        start,
                        end,
                        resolution,
                        body.get("note"),
                        actor,
                    ),
                )
            except Exception as exc:  # unique violation
                conn.rollback()
                raise conflict(
                    "resolution_conflict",
                    "this conflict already has a registered resolution",
                ) from exc
            resolution_id = cur.fetchone()["resolution_id"]
            conn.commit()
        return {"resolution_id": resolution_id, "applies_to": "future runs only", "published_reports_changed": False}

    @app.get("/v1/runs/{run_id}/diary-usage", tags=["lineage"])
    def diary_usage(
        run_id: str,
        actor: str = Depends(analyst),
        person_id: str | None = None,
        disposition: str | None = None,
    ):
        clauses = ["run_id=%s"]
        params: list[Any] = [run_id]
        if person_id:
            clauses.append("person_id=%s")
            params.append(person_id)
        if disposition:
            clauses.append("disposition=%s")
            params.append(disposition)
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute(
                f"SELECT person_id, entry_id, submission_seq, disposition, reason, start_utc, end_utc, station, source_serial, revision FROM run_diary_usage WHERE {' AND '.join(clauses)} ORDER BY person_id, start_utc",
                params,
            )
            rows = cur.fetchall()
        return [
            {**r, "start_utc": iso(r["start_utc"]), "end_utc": iso(r["end_utc"])} for r in rows
        ]

    @app.get("/v1/runs/{run_id}/coverage-gaps", tags=["lineage"])
    def coverage_gaps(run_id: str, actor: str = Depends(analyst)):
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT payload FROM run_coverage_gaps WHERE run_id=%s ORDER BY dimension, category",
                (run_id,),
            )
            return {"run_id": run_id, "cells": [loads(r["payload"]) for r in cur.fetchall()]}

    @app.get("/v1/runs/{run_id}/programs/{program_key}/differences", tags=["lineage"])
    def group_difference(
        run_id: str,
        program_key: str,
        group_a: str,
        group_b: str,
        dimension: str = Query(default="age_band"),
        metric: str = Query(default="mean_minutes_per_person"),
        actor: str = Depends(analyst),
    ):
        from .differences import program_group_difference

        with db.scoped() as conn:
            return program_group_difference(
                conn.cursor(), run_id, program_key, dimension, group_a, group_b, metric
            )

    @app.get("/v1/runs/{run_id}/compare", tags=["lineage"])
    def compare_programs(
        run_id: str,
        program_a: str,
        program_b: str,
        actor: str = Depends(analyst),
    ):
        from .differences import cross_program_comparison

        with db.scoped() as conn:
            return cross_program_comparison(conn.cursor(), run_id, program_a, program_b)

    @app.get("/v1/runs/{run_id}/days", tags=["lineage"])
    def days(run_id: str, actor: str = Depends(analyst), person_id: str | None = None):
        with db.scoped() as conn:
            cur = conn.cursor()
            if person_id:
                cur.execute(
                    "SELECT * FROM run_person_days WHERE run_id=%s AND person_id=%s ORDER BY survey_day",
                    (run_id, person_id),
                )
            else:
                cur.execute("SELECT * FROM run_person_days WHERE run_id=%s ORDER BY survey_day", (run_id,))
            rows = cur.fetchall()
        return [
            {
                **r,
                "window_start": iso(r["window_start"]),
                "window_end": iso(r["window_end"]),
                "segments": loads(r["segments"]),
            }
            for r in rows
        ]

    @app.get("/v1/runs/{run_id}/persons", tags=["lineage"])
    def persons(
        run_id: str,
        actor: str = Depends(analyst),
        eligible: bool | None = None,
        limit: int = Query(default=200, le=2000),
        offset: int = Query(default=0, ge=0),
    ):
        clauses = ["run_id=%s"]
        params: list[Any] = [run_id]
        if eligible is not None:
            clauses.append("eligible=%s")
            params.append(eligible)
        params.extend([limit, offset])
        with db.scoped() as conn:
            cur = conn.cursor()
            cur.execute(
                f"""
                SELECT person_id, household_id, strata, strata_effective, age_band, region, sex,
                       inclusion_probability, base_weight, final_weight, eligible, exclusion_code, exclusion_detail
                  FROM run_persons WHERE {' AND '.join(clauses)}
                 ORDER BY person_id LIMIT %s OFFSET %s
                """,
                params,
            )
            rows = cur.fetchall()
        return [
            {**r, "exclusion_detail": loads(r["exclusion_detail"])}
            for r in rows
        ]

    # ---------------- disclosure control ----------------
    @app.post("/v1/disclosure/requests", status_code=201, tags=["disclosure"])
    def post_publication_request(body: PublicationRequestIn, actor: str = Depends(analyst)):
        """Create a publication request; the disclosure check runs immediately."""
        from . import disclosure

        return disclosure.create_request(db, body.model_dump(mode="json"), actor)

    @app.get("/v1/disclosure/requests", tags=["disclosure"])
    def list_publication_requests(run_id: str | None = Query(default=None), actor: str = Depends(analyst)):
        from . import disclosure

        return disclosure.list_requests(db, run_id)

    @app.get("/v1/disclosure/requests/{request_id}", tags=["disclosure"])
    def get_publication_request(request_id: str, actor: str = Depends(analyst)):
        from . import disclosure

        return disclosure.get_request(db, request_id)

    @app.post("/v1/disclosure/requests/{request_id}/recheck", tags=["disclosure"])
    def recheck_publication_request(request_id: str, actor: str = Depends(analyst)):
        """Re-run the check with the pinned spec against the current catalog."""
        from . import disclosure

        return disclosure.recheck_request(db, request_id, actor)

    @app.post("/v1/disclosure/requests/{request_id}/approve", tags=["disclosure"])
    def approve_publication_request(
        request_id: str,
        body: DecisionNoteIn | None = None,
        director_actor: str = Depends(director),
    ):
        """Research director confirms the plan (suppression vs readability loss)."""
        from . import disclosure

        return disclosure.approve_request(db, request_id, director_actor, (body or DecisionNoteIn()).note)

    @app.post("/v1/disclosure/requests/{request_id}/reject", tags=["disclosure"])
    def reject_publication_request(
        request_id: str,
        body: DecisionNoteIn | None = None,
        director_actor: str = Depends(director),
    ):
        from . import disclosure

        return disclosure.reject_request(db, request_id, director_actor, (body or DecisionNoteIn()).note)

    @app.post("/v1/disclosure/requests/{request_id}/export", tags=["disclosure"])
    def export_publication_request(request_id: str, actor: str = Depends(analyst)):
        """Export strictly the approved cells and register the tables in the catalog."""
        from . import disclosure

        return disclosure.export_request(db, request_id, actor)

    @app.get("/v1/disclosure/requests/{request_id}/export", tags=["disclosure"])
    def get_publication_export(request_id: str, actor: str = Depends(analyst)):
        from . import disclosure

        return disclosure.get_export(db, request_id)

    @app.get("/v1/disclosure/catalog", tags=["disclosure"])
    def get_catalog(run_id: str | None = Query(default=None), actor: str = Depends(analyst)):
        from . import disclosure

        return disclosure.list_catalog(db, run_id)

    @app.post("/v1/disclosure/catalog/{table_id}/withdraw", tags=["disclosure"])
    def withdraw_catalog_table(
        table_id: str,
        body: DecisionNoteIn | None = None,
        director_actor: str = Depends(director),
    ):
        """Withdraw a public table; its cells keep constraining future checks."""
        from . import disclosure

        return disclosure.withdraw_table(db, table_id, director_actor, (body or DecisionNoteIn()).note)

    return app


app = create_app()
