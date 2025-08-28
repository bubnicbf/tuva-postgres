-- db/tests/zz_results.sql
-- Standardized storage for all SQL test outputs + summary views.
-- Uses psql var :"schema"
SET search_path TO :"schema", public;

CREATE TABLE IF NOT EXISTS :"schema".test_results (
  run_id      text        NOT NULL,         -- set by runner
  suite       text        NOT NULL,         -- test file path/basename
  test        text        NOT NULL,         -- test name emitted by SELECT
  pass        boolean     NOT NULL,         -- pass/fail
  payload     jsonb,                         -- all other columns from the row
  executed_at timestamp without time zone DEFAULT now()
);

-- Helpful indexes
CREATE INDEX IF NOT EXISTS test_results_run_id_idx      ON :"schema".test_results (run_id);
CREATE INDEX IF NOT EXISTS test_results_run_id_pass_idx ON :"schema".test_results (run_id, pass);
CREATE INDEX IF NOT EXISTS test_results_suite_idx       ON :"schema".test_results (suite);

-- Rollup view: pass/fail counts by suite for a run
CREATE OR REPLACE VIEW :"schema".v_test_summary AS
SELECT
  run_id,
  suite,
  COUNT(*)                             AS total,
  SUM(CASE WHEN pass THEN 1 ELSE 0 END) AS passed,
  SUM(CASE WHEN NOT pass THEN 1 ELSE 0 END) AS failed,
  ROUND(100.0 * SUM(CASE WHEN pass THEN 1 ELSE 0 END) / NULLIF(COUNT(*),0), 2) AS pass_rate_pct
FROM :"schema".test_results
GROUP BY run_id, suite
ORDER BY run_id DESC, suite;

-- Failures only (handy for CI logs)
CREATE OR REPLACE VIEW :"schema".v_test_failures AS
SELECT run_id, suite, test, pass, payload, executed_at
FROM :"schema".test_results
WHERE NOT pass
ORDER BY run_id DESC, suite, test;
