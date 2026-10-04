-- 0001_initial: project-isolated schema for the UI testing agent.
--
-- Isolation: every project-owned table carries project_id. Child tables reference
-- parents through composite keys (project_id, parent_id) -> parent(project_id, id),
-- so the database itself rejects a row that points into another project.
--
-- History: revisions, step results, healing attempts and reports are append-only
-- (UPDATE is blocked by triggers). A finished test run cannot be modified.

CREATE TABLE projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200),
    base_url    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX idx_projects_list ON projects (created_at DESC, id DESC);

CREATE TABLE messages (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('user', 'system')),
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_messages_list ON messages (project_id, created_at DESC, id DESC);

-- Test definitions -----------------------------------------------------------

CREATE TABLE test_cases (
    id                   TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    title                TEXT NOT NULL,
    current_revision_id  TEXT,
    last_run_status      TEXT NOT NULL DEFAULT 'not_run'
                         CHECK (last_run_status IN ('not_run', 'queued', 'running', 'passed', 'failed', 'error', 'cancelled')),
    last_run_at          TEXT,
    archived_at          TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE (project_id, id),
    FOREIGN KEY (id, current_revision_id) REFERENCES test_case_revisions (test_case_id, id)
);
CREATE INDEX idx_test_cases_list ON test_cases (project_id, created_at DESC, id DESC);
CREATE INDEX idx_test_cases_updated ON test_cases (project_id, updated_at DESC, id DESC);
CREATE INDEX idx_test_cases_status ON test_cases (project_id, last_run_status, created_at DESC, id DESC);

CREATE TABLE test_case_revisions (
    id               TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL,
    test_case_id     TEXT NOT NULL,
    revision_number  INTEGER NOT NULL CHECK (revision_number >= 1),
    instruction      TEXT NOT NULL,
    plan_json        TEXT,
    plan_status      TEXT NOT NULL CHECK (plan_status IN ('ready', 'needs_clarification', 'invalid')),
    issues_json      TEXT NOT NULL DEFAULT '[]',
    planner          TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (project_id, id),
    UNIQUE (test_case_id, id),
    UNIQUE (test_case_id, revision_number),
    FOREIGN KEY (project_id, test_case_id) REFERENCES test_cases (project_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_revisions_list ON test_case_revisions (project_id, test_case_id, revision_number DESC);

CREATE TRIGGER trg_revisions_immutable BEFORE UPDATE ON test_case_revisions
BEGIN
    SELECT RAISE(ABORT, 'test_case_revisions are immutable');
END;

-- Runs -----------------------------------------------------------------------

-- A run is one user request ("run these tests"); it groups one test_run per test.
CREATE TABLE runs (
    id               TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    status           TEXT NOT NULL
                     CHECK (status IN ('queued', 'running', 'passed', 'failed', 'error', 'cancelled')),
    idempotency_key  TEXT,
    request_hash     TEXT NOT NULL,
    skipped_json     TEXT NOT NULL DEFAULT '[]',
    created_at       TEXT NOT NULL,
    finished_at      TEXT,
    UNIQUE (project_id, id)
);
CREATE UNIQUE INDEX uq_runs_idempotency ON runs (project_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_runs_list ON runs (project_id, created_at DESC, id DESC);
CREATE INDEX idx_runs_status ON runs (project_id, status, created_at DESC, id DESC);

CREATE TABLE test_runs (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    test_case_id      TEXT NOT NULL,
    revision_id       TEXT NOT NULL,
    position          INTEGER NOT NULL,
    status            TEXT NOT NULL
                      CHECK (status IN ('queued', 'running', 'passed', 'failed', 'error', 'cancelled')),
    error_kind        TEXT,
    error_source      TEXT CHECK (error_source IN ('application', 'agent', 'browser', 'infrastructure')),
    error_message     TEXT,
    cancel_requested  INTEGER NOT NULL DEFAULT 0,
    worker_id         TEXT,
    heartbeat_at      TEXT,
    healed_steps      INTEGER NOT NULL DEFAULT 0,
    queued_at         TEXT NOT NULL,
    started_at        TEXT,
    finished_at       TEXT,
    duration_ms       INTEGER,
    UNIQUE (project_id, id),
    UNIQUE (run_id, position),
    FOREIGN KEY (project_id, run_id) REFERENCES runs (project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, test_case_id) REFERENCES test_cases (project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (test_case_id, revision_id) REFERENCES test_case_revisions (test_case_id, id) ON DELETE CASCADE
);
-- At most one active (queued/running) execution per test case: blocks duplicate runs
-- even if two requests race past the application-level check.
CREATE UNIQUE INDEX uq_test_runs_one_active ON test_runs (test_case_id)
    WHERE status IN ('queued', 'running');
CREATE INDEX idx_test_runs_queue ON test_runs (queued_at, position, id) WHERE status = 'queued';
CREATE INDEX idx_test_runs_running ON test_runs (heartbeat_at) WHERE status = 'running';
CREATE INDEX idx_test_runs_list ON test_runs (project_id, queued_at DESC, id DESC);
CREATE INDEX idx_test_runs_by_test ON test_runs (project_id, test_case_id, queued_at DESC, id DESC);
CREATE INDEX idx_test_runs_by_status ON test_runs (project_id, status, queued_at DESC, id DESC);
CREATE INDEX idx_test_runs_by_revision ON test_runs (project_id, revision_id, queued_at DESC, id DESC);

CREATE TRIGGER trg_test_runs_final BEFORE UPDATE ON test_runs
WHEN OLD.status IN ('passed', 'failed', 'error', 'cancelled')
  OR NEW.project_id IS NOT OLD.project_id
  OR NEW.run_id IS NOT OLD.run_id
  OR NEW.test_case_id IS NOT OLD.test_case_id
  OR NEW.revision_id IS NOT OLD.revision_id
BEGIN
    SELECT RAISE(ABORT, 'finished test runs and their identity are immutable');
END;

CREATE TABLE run_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    test_run_id  TEXT,
    at           TEXT NOT NULL,
    level        TEXT NOT NULL DEFAULT 'info' CHECK (level IN ('info', 'warning', 'error')),
    message      TEXT NOT NULL,
    FOREIGN KEY (project_id, run_id) REFERENCES runs (project_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_run_events_run ON run_events (project_id, run_id, id);

-- Page history -----------------------------------------------------------------

CREATE TABLE pages (
    id                 TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL REFERENCES projects (id) ON DELETE CASCADE,
    url_key            TEXT NOT NULL,
    title              TEXT,
    latest_version_id  TEXT,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL,
    UNIQUE (project_id, id),
    UNIQUE (project_id, url_key)
);
CREATE INDEX idx_pages_recent ON pages (project_id, last_seen_at DESC, id DESC);

-- One row per distinct element structure of a page (deduplicated by content_hash).
CREATE TABLE page_versions (
    id             TEXT PRIMARY KEY,
    project_id     TEXT NOT NULL,
    page_id        TEXT NOT NULL,
    version        INTEGER NOT NULL,
    content_hash   TEXT NOT NULL,
    title          TEXT,
    elements_json  TEXT NOT NULL,
    element_count  INTEGER NOT NULL,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    UNIQUE (project_id, id),
    UNIQUE (page_id, version),
    UNIQUE (page_id, content_hash),
    FOREIGN KEY (project_id, page_id) REFERENCES pages (project_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_page_versions_page ON page_versions (project_id, page_id, version DESC);

-- Every time a run saw a page: links the page version to the run and step.
CREATE TABLE page_observations (
    id               TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL,
    page_id          TEXT NOT NULL,
    page_version_id  TEXT NOT NULL,
    test_run_id      TEXT,
    step_index       INTEGER,
    url              TEXT NOT NULL,
    observed_at      TEXT NOT NULL,
    FOREIGN KEY (project_id, page_id) REFERENCES pages (project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, page_version_id) REFERENCES page_versions (project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, test_run_id) REFERENCES test_runs (project_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_page_obs_page ON page_observations (project_id, page_id, observed_at DESC);
CREATE INDEX idx_page_obs_run ON page_observations (project_id, test_run_id, step_index);

-- Step results -----------------------------------------------------------------

CREATE TABLE step_results (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    test_run_id         TEXT NOT NULL,
    step_index          INTEGER NOT NULL,
    step_key            TEXT NOT NULL,
    action              TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('action', 'assertion')),
    description         TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'error', 'skipped')),
    target_fingerprint  TEXT,
    locator_json        TEXT,
    healed              INTEGER NOT NULL DEFAULT 0,
    expected            TEXT,
    actual              TEXT,
    detail              TEXT,
    error_kind          TEXT,
    error_message       TEXT,
    page_id             TEXT,
    page_url            TEXT,
    element_json        TEXT,
    started_at          TEXT,
    finished_at         TEXT,
    duration_ms         INTEGER,
    UNIQUE (project_id, id),
    UNIQUE (test_run_id, step_index),
    FOREIGN KEY (project_id, test_run_id) REFERENCES test_runs (project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, page_id) REFERENCES pages (project_id, id)
);
CREATE INDEX idx_step_results_run ON step_results (project_id, test_run_id, step_index);
-- Healing reference lookup: last clean success for the same target on the same page.
CREATE INDEX idx_step_results_reference ON step_results (project_id, target_fingerprint, page_id, finished_at DESC)
    WHERE status = 'passed' AND healed = 0 AND element_json IS NOT NULL;

CREATE TRIGGER trg_step_results_immutable BEFORE UPDATE ON step_results
BEGIN
    SELECT RAISE(ABORT, 'step_results are immutable');
END;

-- Locator healing ---------------------------------------------------------------

CREATE TABLE healing_attempts (
    id                     TEXT PRIMARY KEY,
    project_id             TEXT NOT NULL,
    test_run_id            TEXT NOT NULL,
    step_index             INTEGER NOT NULL,
    step_key               TEXT NOT NULL,
    target_fingerprint     TEXT NOT NULL,
    original_locator_json  TEXT NOT NULL,
    failure_reason         TEXT NOT NULL,
    candidates_json        TEXT NOT NULL,
    selected_json          TEXT,
    confidence             REAL,
    selection_reason       TEXT NOT NULL,
    outcome                TEXT NOT NULL CHECK (outcome IN
                           ('healed', 'ambiguous', 'no_confident_candidate', 'retry_failed', 'budget_exhausted')),
    evidence_json          TEXT NOT NULL,
    created_at             TEXT NOT NULL,
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, test_run_id) REFERENCES test_runs (project_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_healing_run ON healing_attempts (project_id, test_run_id, step_index);
CREATE INDEX idx_healing_list ON healing_attempts (project_id, created_at DESC, id DESC);

CREATE TRIGGER trg_healing_attempts_immutable BEFORE UPDATE ON healing_attempts
BEGIN
    SELECT RAISE(ABORT, 'healing_attempts are immutable');
END;

-- Replacement locators produced by healing. They start 'provisional' and are only
-- used ahead of the original locator once a reviewer marks them 'validated'.
CREATE TABLE locators (
    id                  TEXT PRIMARY KEY,
    project_id          TEXT NOT NULL,
    test_case_id        TEXT NOT NULL,
    target_fingerprint  TEXT NOT NULL,
    step_key            TEXT NOT NULL,
    original_json       TEXT NOT NULL,
    locator_json        TEXT NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('provisional', 'validated', 'rejected', 'superseded')),
    confidence          REAL NOT NULL,
    healing_attempt_id  TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    reviewed_at         TEXT,
    review_note         TEXT,
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, test_case_id) REFERENCES test_cases (project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, healing_attempt_id) REFERENCES healing_attempts (project_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_locators_lookup ON locators (project_id, test_case_id, target_fingerprint, status, created_at DESC);
CREATE INDEX idx_locators_list ON locators (project_id, created_at DESC, id DESC);
CREATE UNIQUE INDEX uq_locators_one_validated ON locators (test_case_id, target_fingerprint)
    WHERE status = 'validated';

-- Evidence and reports -----------------------------------------------------------

CREATE TABLE artifacts (
    id            TEXT PRIMARY KEY,
    project_id    TEXT NOT NULL,
    test_run_id   TEXT NOT NULL,
    step_index    INTEGER,
    kind          TEXT NOT NULL CHECK (kind IN ('screenshot', 'trace', 'video')),
    label         TEXT,
    rel_path      TEXT NOT NULL,
    content_type  TEXT NOT NULL,
    size_bytes    INTEGER,
    created_at    TEXT NOT NULL,
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, test_run_id) REFERENCES test_runs (project_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_artifacts_run ON artifacts (project_id, test_run_id, created_at, id);

CREATE TABLE reports (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    test_run_id  TEXT NOT NULL UNIQUE,
    outcome      TEXT NOT NULL,
    report_json  TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, test_run_id) REFERENCES test_runs (project_id, id) ON DELETE CASCADE
);

CREATE TRIGGER trg_reports_immutable BEFORE UPDATE ON reports
BEGIN
    SELECT RAISE(ABORT, 'reports are immutable');
END;
