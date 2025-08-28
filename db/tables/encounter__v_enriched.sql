-- db/tables/encounter__v_enriched.sql
-- Presents friendly labels for coded fields by joining terminology.
-- Uses :"schema" and :"terminology_schema".

CREATE OR REPLACE VIEW :"schema".v_encounter_enriched AS
SELECT
  e.*,
  et.display  AS encounter_type_display,
  asrc.display AS admit_source_display,
  atyp.display AS admit_type_display,
  ddisp.display AS discharge_disposition_display,
  dtyp.display AS primary_diagnosis_code_type_display,
  ddesc.description AS primary_diagnosis_display
FROM :"schema".encounter e
LEFT JOIN :"terminology_schema".encounter_type        et   ON e.encounter_type = et.code
LEFT JOIN :"terminology_schema".admit_source          asrc ON e.admit_source_code = asrc.code
LEFT JOIN :"terminology_schema".admit_type            atyp ON e.admit_type_code = atyp.code
LEFT JOIN :"terminology_schema".discharge_disposition ddisp ON e.discharge_disposition_code = ddisp.code
LEFT JOIN :"terminology_schema".diagnosis_code_type   dtyp ON e.primary_diagnosis_code_type = dtyp.code
LEFT JOIN :"terminology_schema".diagnosis_code        ddesc
       ON e.primary_diagnosis_code_type = ddesc.code_type
      AND e.primary_diagnosis_code      = ddesc.code;
