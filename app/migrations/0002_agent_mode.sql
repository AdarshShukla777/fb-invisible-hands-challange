-- 0002_agent_mode: browser-use style agent tests with per-test run memory.

-- 'plan': validated structured plan (strict). 'agent': an LLM agent explores the live app.
ALTER TABLE test_case_revisions ADD COLUMN mode TEXT NOT NULL DEFAULT 'plan' CHECK (mode IN ('plan', 'agent'));

-- Who decided the outcome: 'assertions' (plan steps), 'agent' (agent's verdict) or 'replay'
-- (remembered path and remembered checks replayed without the model).
ALTER TABLE test_runs ADD COLUMN verdict_source TEXT;

-- Which mechanism produced a step: plan, replay (memory), agent, agent_assist (agent filled a gap in replay).
ALTER TABLE step_results ADD COLUMN origin TEXT;

-- One row per finished agent run: what worked, what was missing, what to look for next time.
-- Append-only, scoped to the project and test case. Never contains values typed into sensitive fields.
CREATE TABLE agent_memories (
    id                TEXT PRIMARY KEY,
    project_id        TEXT NOT NULL,
    test_case_id      TEXT NOT NULL,
    revision_id       TEXT NOT NULL,
    test_run_id       TEXT NOT NULL UNIQUE,
    outcome           TEXT NOT NULL CHECK (outcome IN ('passed', 'failed', 'blocked', 'error', 'cancelled')),
    summary           TEXT,
    verified          INTEGER NOT NULL DEFAULT 0,
    trajectory_json   TEXT NOT NULL DEFAULT '[]',
    notes_json        TEXT NOT NULL DEFAULT '[]',
    missing_json      TEXT NOT NULL DEFAULT '[]',
    -- agent-chosen values, dead ends, replay result, per-step reasoning (all redacted)
    details_json      TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL,
    UNIQUE (project_id, id),
    FOREIGN KEY (project_id, test_case_id) REFERENCES test_cases (project_id, id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, test_run_id) REFERENCES test_runs (project_id, id) ON DELETE CASCADE
);
CREATE INDEX idx_agent_memories_test ON agent_memories (project_id, test_case_id, created_at DESC);
CREATE INDEX idx_agent_memories_passed ON agent_memories (project_id, test_case_id, created_at DESC)
    WHERE outcome = 'passed';

CREATE TRIGGER trg_agent_memories_immutable BEFORE UPDATE ON agent_memories
BEGIN
    SELECT RAISE(ABORT, 'agent_memories are immutable');
END;
