-- db/tables/patient__v_enriched.sql
-- Convenience view: core patient enriched with terminology displays.
-- Uses :"schema" and :"terminology_schema".

CREATE OR REPLACE VIEW :"schema".v_patient_enriched AS
SELECT
  p.*,
  sx.display  AS sex_display,
  rc.display  AS race_display,
  et.display  AS ethnicity_display
FROM :"schema".patient p
LEFT JOIN :"terminology_schema".sex       sx ON p.sex       = sx.code
LEFT JOIN :"terminology_schema".race      rc ON p.race      = rc.code
LEFT JOIN :"terminology_schema".ethnicity et ON p.ethnicity = et.code;
