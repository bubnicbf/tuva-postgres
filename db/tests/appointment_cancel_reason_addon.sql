-- db/tests/appointment_cancel_reason_addon.sql
-- Soft check: normalized cancellation reason codes must exist in terminology.
-- Expects psql vars: :"schema", :"terminology_schema"
SET search_path TO :"schema", public;

SELECT 'appt_norm_cancel_reason_known' AS test,
       COUNT(*) = 0 AS pass,
       COUNT(*) AS unknown_code_count
FROM appointment a
LEFT JOIN :"terminology_schema".appointment_cancellation_reason t
  ON a.normalized_cancellation_reason_code = t.code
WHERE a.normalized_cancellation_reason_code IS NOT NULL
  AND t.code IS NULL;
