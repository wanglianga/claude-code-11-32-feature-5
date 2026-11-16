"""Shared pytest fixtures: isolated schema, app client and seeded data."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))

os.environ.setdefault("AUDIENCE_DATABASE_URL", "postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev")

from app.config import Settings  # noqa: E402
from app.db import Database  # noqa: E402
from app.main import create_app  # noqa: E402

TEST_DB_URL = os.environ.get(
    "AUDIENCE_TEST_DATABASE_URL", "postgresql://ccdev:ccdev@127.0.0.1:5432/ccdev"
)


def _settings(schema: str) -> Settings:
    return Settings(
        database_url=TEST_DB_URL,
        admin_api_key="test-admin-key",
        analyst_api_key="test-analyst-key",
        default_schema=schema,
        service_timezone="Asia/Shanghai",
    )


@pytest.fixture(scope="session")
def schema_name() -> str:
    return f"test_aud_{uuid.uuid4().hex[:10]}"


@pytest.fixture(scope="session")
def database(schema_name):
    db = Database(_settings(schema_name))
    db.open()
    yield db
    with db.scoped() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s", (schema_name,)
        )
        tables = [r["tablename"] for r in cur.fetchall()]
        if tables:
            cur.execute(
                "DROP TABLE IF EXISTS "
                + ", ".join(f'"{t}"' for t in tables)
                + " CASCADE"
            )
        cur.execute(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        conn.commit()
    db.close()


@pytest.fixture(scope="session")
def settings(schema_name):
    return _settings(schema_name)


@pytest.fixture(scope="session")
def app(settings, database):
    application = create_app(settings=settings, db=database)
    return application


@pytest.fixture(scope="session")
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        c.headers.update({"X-API-Key": "test-analyst-key"})
        yield c


@pytest.fixture(scope="session")
def seeded(database, client):
    """Seed the fictional dataset once for the whole session."""
    from app import ingest
    from demo import fixtures
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Shanghai")
    rec = ingest.create_recruitment_batch(
        database, fixtures.recruitment_batch(include_unknown_probability=False), "pytest"
    )
    # A second frame batch that carries one person with an unknown inclusion
    # probability, used to prove that no formal population estimate can be made.
    rec_unknown = ingest.create_recruitment_batch(
        database,
        fixtures.recruitment_batch(include_unknown_probability=True, batch_ref="frame-unknown-p"),
        "pytest",
    )
    tgt = ingest.create_target_batch(database, fixtures.target_batch(), "pytest")
    prg = ingest.create_program_batch(database, fixtures.program_batch(), "pytest")
    for sub in fixtures.diary_submissions():
        ingest.create_diary_submission(database, sub, "pytest", tz)
    with database.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(submission_seq),0) AS m FROM diary_submissions")
        cutoff_before_resub = cur.fetchone()["m"]
    ingest.create_diary_submission(database, fixtures.S701_RESUBMISSION, "pytest", tz)
    with database.scoped() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(submission_seq),0) AS m FROM diary_submissions")
        latest = cur.fetchone()["m"]
    return {
        "recruitment_batch_seq": rec["batch_seq"],
        "recruitment_unknown_probability_seq": rec_unknown["batch_seq"],
        "target_batch_seq": tgt["batch_seq"],
        "program_batch_seq": prg["batch_seq"],
        "cutoff_before_resub": cutoff_before_resub,
        "latest_cutoff": latest,
    }
