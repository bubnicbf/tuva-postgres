-- db/tables/medication__v_enriched.sql
-- Adds friendly labels for source_code_type and dictionary-driven canon names.
-- Uses :"schema" and :"terminology_schema".

CREATE OR REPLACE VIEW :"schema".v_medication_enriched AS
SELECT
  m.*,
  mct.display                    AS source_code_type_display,

  -- Canonical dictionary descriptions (if loaded)
  n.description                  AS ndc_description_term,
  r.preferred_name               AS rxnorm_preferred_name,
  a.description                  AS atc_description_term
FROM :"schema".medication m
LEFT JOIN :"terminology_schema".medication_code_type mct
       ON m.source_code_type = mct.code
LEFT JOIN :"terminology_schema".ndc n
       ON REGEXP_REPLACE(m.ndc_code, '\D', '', 'g') = n.ndc11
LEFT JOIN :"terminology_schema".rxnorm r
       ON m.rxnorm_code = r.rxnorm_code
LEFT JOIN :"terminology_schema".atc a
       ON m.atc_code = a.code;
