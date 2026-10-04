-- 0004_layout: where each acted-on element was, to report elements that moved or resized.

-- layout_key identifies "the same step on the same element" across runs of a test: page identity,
-- action and the element's identity (plan mode: the step target's fingerprint). layout_json holds
-- the element's box (page and region coordinates, viewport, region) and the comparison with the
-- last earlier run of the same test that measured the same step (measured before acting, so a step
-- whose action failed afterwards still has a valid position).
ALTER TABLE step_results ADD COLUMN layout_key TEXT;
ALTER TABLE step_results ADD COLUMN layout_json TEXT;
CREATE INDEX idx_step_results_layout ON step_results (project_id, layout_key, finished_at DESC)
    WHERE layout_key IS NOT NULL;
