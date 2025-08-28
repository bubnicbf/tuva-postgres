-- db/tables/person_lookup__v.sql
-- Unified lookup surface for typical resolution patterns.

CREATE OR REPLACE VIEW :"schema".v_person_lookup AS
SELECT
  person_id,
  patient_id,
  member_id,
  payer,
  plan,
  data_source
FROM :"schema".person_id_crosswalk;
