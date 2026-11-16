-- Audience estimation service schema.
-- All tables live in the schema named by AUDIENCE_DB_SCHEMA (default: audience).
-- Timestamps are stored as timestamptz (UTC). Raw diary strings are preserved.

CREATE TABLE IF NOT EXISTS audit_events (
    event_id      BIGSERIAL PRIMARY KEY,
    occurred_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor         TEXT NOT NULL,
    action        TEXT NOT NULL,
    entity        TEXT NOT NULL,
    entity_id     TEXT,
    details       JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
-- Recruitment (frame) inputs. Append-only batch versions; each upload is a
-- complete, immutable batch. A run pins a batch_seq cutoff.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS recruitment_batches (
    batch_seq           BIGSERIAL PRIMARY KEY,
    batch_ref           TEXT UNIQUE,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor               TEXT NOT NULL,
    survey_period_start DATE NOT NULL,
    survey_period_end   DATE NOT NULL,
    row_count           INTEGER NOT NULL,
    CONSTRAINT recruitment_period_order CHECK (survey_period_end >= survey_period_start)
);

CREATE TABLE IF NOT EXISTS recruitment_rows (
    row_id               BIGSERIAL PRIMARY KEY,
    batch_seq            BIGINT NOT NULL REFERENCES recruitment_batches(batch_seq),
    household_id         TEXT NOT NULL,
    person_id            TEXT NOT NULL,
    strata               TEXT NOT NULL,
    age_band             TEXT NOT NULL,
    region               TEXT NOT NULL,
    sex                  TEXT NOT NULL,
    inclusion_probability NUMERIC(18,10),
    household_size       INTEGER,
    notes                TEXT,
    UNIQUE (batch_seq, person_id),
    CONSTRAINT inclusion_probability_range
        CHECK (inclusion_probability IS NULL OR (inclusion_probability > 0 AND inclusion_probability <= 1))
);

CREATE INDEX IF NOT EXISTS recruitment_rows_person_idx ON recruitment_rows (person_id, batch_seq DESC);
CREATE INDEX IF NOT EXISTS recruitment_rows_household_idx ON recruitment_rows (household_id);

-- ---------------------------------------------------------------------------
-- Population target margins. Each batch is one complete, consistent target
-- specification. Dimensions must all sum to the same population total.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS target_batches (
    batch_seq        BIGSERIAL PRIMARY KEY,
    batch_ref        TEXT UNIQUE,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor            TEXT NOT NULL,
    population_total BIGINT NOT NULL,
    dimensions       JSONB NOT NULL,
    dimension_totals JSONB NOT NULL,
    CONSTRAINT population_total_positive CHECK (population_total > 0)
);

-- ---------------------------------------------------------------------------
-- Program schedule. Each batch is one complete schedule version.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS program_batches (
    batch_seq   BIGSERIAL PRIMARY KEY,
    batch_ref   TEXT UNIQUE,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor       TEXT NOT NULL,
    timezone    TEXT NOT NULL,
    row_count   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS program_airings (
    airing_id   BIGSERIAL PRIMARY KEY,
    batch_seq   BIGINT NOT NULL REFERENCES program_batches(batch_seq),
    program_key TEXT NOT NULL,
    title       TEXT NOT NULL,
    station     TEXT NOT NULL,
    air_date    DATE NOT NULL,
    start_local TEXT NOT NULL,
    end_local   TEXT NOT NULL,
    start_utc   TIMESTAMPTZ NOT NULL,
    end_utc     TIMESTAMPTZ NOT NULL,
    UNIQUE (batch_seq, program_key, station, air_date, start_local),
    CONSTRAINT airing_interval_order CHECK (end_utc > start_utc)
);

CREATE INDEX IF NOT EXISTS program_airings_batch_idx ON program_airings (batch_seq, program_key);

-- ---------------------------------------------------------------------------
-- Diary submissions. submission_seq is the diary cutoff serial. Submission
-- payload is immutable; replaced entries keep their rows and are marked with
-- the superseding submission.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS diary_submissions (
    submission_seq   BIGSERIAL PRIMARY KEY,
    person_id        TEXT NOT NULL,
    household_id     TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    replacement_scope JSONB NOT NULL,
    coverage         JSONB NOT NULL,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_at     TIMESTAMPTZ NOT NULL,
    actor            TEXT NOT NULL,
    raw              JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS diary_submissions_person_idx
    ON diary_submissions (person_id, submission_seq);

CREATE TABLE IF NOT EXISTS diary_entries (
    entry_id                   BIGSERIAL PRIMARY KEY,
    submission_seq             BIGINT NOT NULL REFERENCES diary_submissions(submission_seq),
    person_id                  TEXT NOT NULL,
    household_id               TEXT NOT NULL,
    start_raw                  TEXT NOT NULL,
    end_raw                    TEXT NOT NULL,
    start_utc                  TIMESTAMPTZ NOT NULL,
    end_utc                    TIMESTAMPTZ NOT NULL,
    station                    TEXT NOT NULL,
    source_serial              TEXT NOT NULL,
    revision                   INTEGER NOT NULL,
    superseded_by_submission_seq BIGINT REFERENCES diary_submissions(submission_seq),
    CONSTRAINT diary_entry_order CHECK (end_utc > start_utc)
);

CREATE INDEX IF NOT EXISTS diary_entries_submission_idx ON diary_entries (submission_seq);
CREATE INDEX IF NOT EXISTS diary_entries_person_idx ON diary_entries (person_id, start_utc);

CREATE TABLE IF NOT EXISTS diary_coverage_windows (
    window_id      BIGSERIAL PRIMARY KEY,
    submission_seq BIGINT NOT NULL REFERENCES diary_submissions(submission_seq),
    start_utc      TIMESTAMPTZ NOT NULL,
    end_utc        TIMESTAMPTZ NOT NULL,
    CONSTRAINT coverage_window_order CHECK (end_utc > start_utc)
);

-- Raw rows superseded by replacement scopes. Kept for history; the current
-- calculation never reads them.
CREATE INDEX IF NOT EXISTS diary_entries_active_idx
    ON diary_entries (person_id, start_utc) WHERE superseded_by_submission_seq IS NULL;

-- ---------------------------------------------------------------------------
-- Runs. Inputs and configuration are pinned at creation. Results and audit
-- records are append/replaceable only in run-owned result tables; a published
-- report row is never updated or deleted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS runs (
    run_id           UUID PRIMARY KEY,
    status           TEXT NOT NULL,
    seed             BIGINT NOT NULL,
    cutoffs          JSONB NOT NULL,
    survey_period    JSONB NOT NULL,
    config           JSONB NOT NULL,
    config_hash      TEXT NOT NULL,
    input_hash       TEXT NOT NULL,
    parent_run_id    UUID REFERENCES runs(run_id),
    note             TEXT,
    created_by       TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at       TIMESTAMPTZ,
    finished_at      TIMESTAMPTZ,
    failure          JSONB,
    calibration      JSONB,
    bootstrap        JSONB,
    interval_status  TEXT,
    published_at     TIMESTAMPTZ,
    CHECK (status IN (
        'created','running','published','rejected','failed'
    ))
);

CREATE TABLE IF NOT EXISTS run_stages (
    run_id      UUID NOT NULL REFERENCES runs(run_id),
    stage       TEXT NOT NULL,
    status      TEXT NOT NULL,
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    payload     JSONB,
    error       JSONB,
    PRIMARY KEY (run_id, stage),
    CHECK (status IN ('pending','running','done','failed'))
);

CREATE TABLE IF NOT EXISTS run_inputs (
    run_id   UUID NOT NULL REFERENCES runs(run_id),
    kind     TEXT NOT NULL,
    seq      BIGINT NOT NULL,
    payload  JSONB NOT NULL,
    PRIMARY KEY (run_id, kind)
);

CREATE TABLE IF NOT EXISTS run_persons (
    run_id            UUID NOT NULL REFERENCES runs(run_id),
    person_id         TEXT NOT NULL,
    household_id      TEXT NOT NULL,
    strata            TEXT NOT NULL,
    strata_effective  TEXT NOT NULL,
    age_band          TEXT NOT NULL,
    region            TEXT NOT NULL,
    sex               TEXT NOT NULL,
    inclusion_probability NUMERIC(18,10),
    base_weight       NUMERIC(20,8),
    final_weight      NUMERIC(20,8),
    eligible          BOOLEAN NOT NULL,
    exclusion_code    TEXT,
    exclusion_detail  JSONB,
    diary_last_seq    BIGINT,
    completeness      JSONB,
    PRIMARY KEY (run_id, person_id)
);

CREATE TABLE IF NOT EXISTS run_person_days (
    run_id       UUID NOT NULL REFERENCES runs(run_id),
    person_id    TEXT NOT NULL,
    survey_day   DATE NOT NULL,
    window_start TIMESTAMPTZ NOT NULL,
    window_end   TIMESTAMPTZ NOT NULL,
    covered_minutes NUMERIC(12,3) NOT NULL,
    listening_minutes NUMERIC(12,3) NOT NULL,
    unknown_minutes NUMERIC(12,3) NOT NULL,  -- filled but contradictory (unconfirmed) minutes
    no_listening_minutes NUMERIC(12,3) NOT NULL,
    uncovered_minutes NUMERIC(12,3) NOT NULL,
    segments     JSONB NOT NULL,
    PRIMARY KEY (run_id, person_id, survey_day)
);

CREATE TABLE IF NOT EXISTS run_diary_usage (
    run_id            UUID NOT NULL REFERENCES runs(run_id),
    person_id         TEXT NOT NULL,
    entry_id          BIGINT,
    submission_seq    BIGINT,
    disposition       TEXT NOT NULL,
    reason            TEXT,
    start_utc         TIMESTAMPTZ,
    end_utc           TIMESTAMPTZ,
    station           TEXT,
    source_serial     TEXT,
    revision          INTEGER,
    PRIMARY KEY (run_id, person_id, entry_id)
);

CREATE TABLE IF NOT EXISTS run_conflicts (
    conflict_id   BIGSERIAL PRIMARY KEY,
    run_id        UUID NOT NULL REFERENCES runs(run_id),
    person_id     TEXT NOT NULL,
    station_a     TEXT NOT NULL,
    station_b     TEXT NOT NULL,
    start_utc     TIMESTAMPTZ NOT NULL,
    end_utc       TIMESTAMPTZ NOT NULL,
    entry_a       BIGINT,
    entry_b       BIGINT,
    status        TEXT NOT NULL DEFAULT 'pending',
    resolution_source TEXT,
    CONSTRAINT conflict_order CHECK (end_utc > start_utc)
);

CREATE INDEX IF NOT EXISTS run_conflicts_run_idx ON run_conflicts (run_id, person_id);

-- Analyst decisions on cross-station conflicts. The registry is append-only
-- input: a later run may use a resolution, but earlier published reports are
-- never recalculated.
CREATE TABLE IF NOT EXISTS conflict_resolutions (
    resolution_id BIGSERIAL PRIMARY KEY,
    person_id     TEXT NOT NULL,
    station_a     TEXT NOT NULL,
    station_b     TEXT NOT NULL,
    start_utc     TIMESTAMPTZ NOT NULL,
    end_utc       TIMESTAMPTZ NOT NULL,
    resolution    TEXT NOT NULL,
    note          TEXT,
    actor         TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (person_id, station_a, station_b, start_utc, end_utc),
    CONSTRAINT conflict_resolution_enum CHECK (
        resolution IN ('confirmed_station_a','confirmed_station_b','confirmed_neither','dismissed')
    ),
    CONSTRAINT conflict_resolution_order CHECK (end_utc > start_utc)
);

CREATE TABLE IF NOT EXISTS run_calibration_cells (
    run_id           UUID NOT NULL REFERENCES runs(run_id),
    replicate        INTEGER NOT NULL DEFAULT -1,  -- -1 = point estimate
    dimension        TEXT NOT NULL,
    category         TEXT NOT NULL,
    target           NUMERIC(20,4) NOT NULL,
    initial_sum      NUMERIC(20,8),
    final_sum        NUMERIC(20,8),
    residual         NUMERIC(20,8),
    relative_residual NUMERIC(20,10),
    clamped_persons  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, replicate, dimension, category)
);

CREATE TABLE IF NOT EXISTS run_calibration_rounds (
    run_id        UUID NOT NULL REFERENCES runs(run_id),
    replicate     INTEGER NOT NULL DEFAULT -1,
    round_no      INTEGER NOT NULL,
    max_rel_residual NUMERIC(20,10) NOT NULL,
    clamped_persons  INTEGER NOT NULL,
    PRIMARY KEY (run_id, replicate, round_no)
);

CREATE TABLE IF NOT EXISTS run_person_program (
    run_id       UUID NOT NULL REFERENCES runs(run_id),
    person_id    TEXT NOT NULL,
    program_key  TEXT NOT NULL,
    minutes      NUMERIC(14,3) NOT NULL,
    reached      BOOLEAN NOT NULL,
    by_day       JSONB NOT NULL,
    by_airing    JSONB NOT NULL,
    PRIMARY KEY (run_id, person_id, program_key)
);

CREATE TABLE IF NOT EXISTS run_bootstrap_replicates (
    run_id       UUID NOT NULL REFERENCES runs(run_id),
    replicate    INTEGER NOT NULL,
    success      BOOLEAN NOT NULL,
    failure_code TEXT,
    results      JSONB,
    PRIMARY KEY (run_id, replicate)
);

CREATE TABLE IF NOT EXISTS run_calibration_replicates (
    run_id       UUID NOT NULL REFERENCES runs(run_id),
    replicate    INTEGER NOT NULL,
    success      BOOLEAN NOT NULL,
    status       TEXT NOT NULL,
    rounds       INTEGER,
    max_rel_residual NUMERIC(20,10),
    weights      JSONB NOT NULL,
    cells        JSONB NOT NULL,
    PRIMARY KEY (run_id, replicate)
);

CREATE TABLE IF NOT EXISTS run_program_estimates (
    run_id        UUID NOT NULL REFERENCES runs(run_id),
    program_key   TEXT NOT NULL,
    title         TEXT NOT NULL,
    estimates     JSONB NOT NULL,
    intervals     JSONB,
    by_domain     JSONB NOT NULL,
    lineage_summary JSONB NOT NULL,
    PRIMARY KEY (run_id, program_key)
);

CREATE TABLE IF NOT EXISTS run_coverage_gaps (
    run_id        UUID NOT NULL REFERENCES runs(run_id),
    dimension     TEXT NOT NULL,
    category      TEXT NOT NULL,
    payload       JSONB NOT NULL,
    PRIMARY KEY (run_id, dimension, category)
);

CREATE TABLE IF NOT EXISTS run_reports (
    run_id        UUID PRIMARY KEY REFERENCES runs(run_id),
    report        JSONB NOT NULL,
    report_hash   TEXT NOT NULL,
    published_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Reports are immutable once inserted.
CREATE OR REPLACE FUNCTION forbid_report_mutation() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'published reports are immutable';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS run_reports_no_update ON run_reports;
CREATE TRIGGER run_reports_no_update
    BEFORE UPDATE OR DELETE ON run_reports
    FOR EACH ROW EXECUTE FUNCTION forbid_report_mutation();

-- ---------------------------------------------------------------------------
-- Disclosure control for external audience tables.
--
-- publication_requests: one disclosure-checked release plan per request.
--   The plan is internal: it records which cells are published/suppressed
--   and the exact feasible ranges of the protected cells, never the true
--   values of suppressed cells.
-- published_tables: the public catalog. Every table that has been exported
--   stays here forever; withdrawn tables keep constraining future checks
--   (the audience is not assumed to forget).
-- catalog_state: a singleton revision counter. Every export bumps it inside
--   the same transaction that registers the tables, so concurrent releases
--   are serialised and a later table can never bypass an earlier one.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS catalog_state (
    singleton   BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    revision    BIGINT NOT NULL DEFAULT 0
);
INSERT INTO catalog_state (singleton, revision) VALUES (TRUE, 0) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS publication_requests (
    request_id      UUID PRIMARY KEY,
    run_id          UUID NOT NULL REFERENCES runs(run_id),
    status          TEXT NOT NULL,
    spec            JSONB NOT NULL,
    spec_hash       TEXT NOT NULL,
    policy          JSONB NOT NULL,
    plan            JSONB,
    blocking        JSONB,
    catalog_revision_checked BIGINT,
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    decided_note    TEXT,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status IN ('awaiting_approval','blocked','approved','exported','rejected','stale'))
);

CREATE INDEX IF NOT EXISTS publication_requests_run_idx ON publication_requests (run_id);

CREATE TABLE IF NOT EXISTS published_tables (
    table_id        UUID PRIMARY KEY,
    request_id      UUID NOT NULL REFERENCES publication_requests(request_id),
    run_id          UUID NOT NULL REFERENCES runs(run_id),
    table_index     INTEGER NOT NULL,
    name            TEXT NOT NULL,
    spec            JSONB NOT NULL,
    public_cells    JSONB NOT NULL,
    hidden_cells    JSONB NOT NULL,
    catalog_revision BIGINT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'published',
    published_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    withdrawn_at    TIMESTAMPTZ,
    withdrawn_by    TEXT,
    CHECK (status IN ('published','withdrawn'))
);

CREATE INDEX IF NOT EXISTS published_tables_run_idx ON published_tables (run_id);

CREATE TABLE IF NOT EXISTS publication_exports (
    export_id        UUID PRIMARY KEY,
    request_id       UUID NOT NULL REFERENCES publication_requests(request_id),
    catalog_revision BIGINT NOT NULL,
    document         JSONB NOT NULL,
    exported_by      TEXT NOT NULL,
    exported_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
