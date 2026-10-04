-- 0003_bugs: application bugs found by test runs, kept per test so every later run knows them.

-- One row per distinct bug (test case + signature of the failing check or step). A bug found
-- again updates the same row; a later run whose same check passes marks it fixed; a fixed bug
-- found again is reopened as a regression. The tester can mark a bug as not a bug (dismissed).
CREATE TABLE bugs (
    id                   TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL,
    test_case_id         TEXT NOT NULL,
    signature            TEXT NOT NULL,
    source               TEXT NOT NULL CHECK (source IN ('check', 'agent', 'step')),
    title                TEXT NOT NULL,
    action               TEXT,           -- what the test did right before the failure
    check_text           TEXT,           -- the check or step that failed
    expected             TEXT,
    actual               TEXT,
    page_url             TEXT,
    screenshot_url       TEXT,
    status               TEXT NOT NULL CHECK (status IN ('open', 'fixed', 'dismissed')),
    regressed            INTEGER NOT NULL DEFAULT 0,
    occurrences          INTEGER NOT NULL DEFAULT 1,
    first_test_run_id    TEXT NOT NULL,
    last_test_run_id     TEXT NOT NULL,
    last_run_id          TEXT NOT NULL,
    fixed_test_run_id    TEXT,
    note                 TEXT,           -- the tester's note (e.g. why it is not a bug)
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL,
    updated_at           TEXT NOT NULL,
    UNIQUE (project_id, id),
    UNIQUE (project_id, test_case_id, signature),
    FOREIGN KEY (project_id, test_case_id) REFERENCES test_cases (project_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_bugs_list ON bugs (project_id, last_seen_at DESC, id DESC);
CREATE INDEX idx_bugs_status ON bugs (project_id, status, last_seen_at DESC, id DESC);
CREATE INDEX idx_bugs_test ON bugs (project_id, test_case_id, status);
