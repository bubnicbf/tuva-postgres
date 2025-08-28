-- Row count presence
SELECT 'patient_has_rows' AS test, COUNT(*) > 0 AS pass FROM tuva.patient;
SELECT 'claim_has_rows'   AS test, COUNT(*) > 0 AS pass FROM tuva.claim;

-- Referential integrity sanity
SELECT 'encounter_patient_fk' AS test, COUNT(*)=0 AS pass
FROM tuva.encounter e
LEFT JOIN tuva.patient p ON e.patient_id=p.patient_id
WHERE p.patient_id IS NULL;

-- Non-negative amounts
SELECT 'line_amounts_nonneg' AS test, COUNT(*)=0 AS pass
FROM tuva.claim_line
WHERE (charge_amt < 0 OR paid_amt < 0);
