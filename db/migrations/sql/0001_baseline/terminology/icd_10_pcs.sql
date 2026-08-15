-- db/terminology/icd_10_pcs.sql
-- ICD-10-PCS: procedure codes (code → description).
CREATE TABLE IF NOT EXISTS :"terminology_schema".icd_10_pcs (
  icd_10_pcs  varchar PRIMARY KEY,
  description varchar
);

ALTER TABLE :"terminology_schema".icd_10_pcs
  ADD COLUMN IF NOT EXISTS icd_10_pcs  varchar,
  ADD COLUMN IF NOT EXISTS description varchar;

-- Optional legacy backfill
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = :'terminology_schema' AND table_name = 'diagnosis_code')
  THEN
    EXECUTE format($f$
      INSERT INTO %I.icd_10_pcs (icd_10_pcs, description)
      SELECT DISTINCT NULLIF(BTRIM(code), ''), NULLIF(BTRIM(long_description), '')
      FROM %I.diagnosis_code
      WHERE code_type IN ('ICD-10-PCS','ICD10PCS')
        AND code IS NOT NULL AND BTRIM(code) <> ''
      ON CONFLICT (icd_10_pcs) DO UPDATE
        SET description = COALESCE(EXCLUDED.description, %I.icd_10_pcs.description);
    $f$, :'terminology_schema', :'terminology_schema', :'terminology_schema');
  END IF;
END$$;

CREATE INDEX IF NOT EXISTS icd_10_pcs_desc_idx ON :"terminology_schema".icd_10_pcs (description);
COMMENT ON TABLE  :"terminology_schema".icd_10_pcs IS 'ICD-10-PCS procedure terminology.';
COMMENT ON COLUMN :"terminology_schema".icd_10_pcs.icd_10_pcs IS 'The ICD-10-PCS code.';
