-- db/terminology/snomed_ct__v_descendants.sql
-- Helper view: list all descendants for each SNOMED ancestor.
-- Uses psql var :"terminology_schema"

CREATE OR REPLACE VIEW :"terminology_schema".v_snomed_descendants AS
SELECT
  tc.parent_snomed_code        AS ancestor_code,
  p.description                AS ancestor_description,
  tc.child_snomed_code         AS descendant_code,
  c.description                AS descendant_description,
  c.is_active                  AS descendant_is_active
FROM :"terminology_schema".snomed_ct_transitive_closure tc
LEFT JOIN :"terminology_schema".snomed_ct p
       ON p.snomed_ct = tc.parent_snomed_code
LEFT JOIN :"terminology_schema".snomed_ct c
       ON c.snomed_ct = tc.child_snomed_code;
