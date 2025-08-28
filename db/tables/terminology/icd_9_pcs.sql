-- db/terminology/icd_9_pcs.sql
-- ICD-9-PCS: procedure codes (code → short/long).
CREATE TABLE IF NOT EXISTS :"terminology_schema".icd_9_pcs (
  icd_9_pcs          varchar PRIMARY KEY,
  long_description   varchar,
  short_description  varchar
);

ALTER TABLE :"terminology_schema".icd_9_pcs
  ADD COLUMN IF NOT EXISTS icd_9_pcs         varchar,
  ADD COLUMN IF NOT EXISTS long_description  varchar,
  ADD COLUMN IF NOT EXISTS short_description varchar;

-- Optional legacy backfill
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = :'terminology_schema' AND table_name = 'diagnosis_code')
  THEN
    EXECUTE format($f$
      INSERT INTO %I.icd_9_pcs (icd_9_pcs, long_description, short_description)
      SELECT DISTINCT NULLIF(BTRIM(code), ''),
             NULLIF(BTRIM(long_description), ''),
             NULLIF(BTRIM(short_description), '')
      FROM %I.diagnosis_code
      WHERE code_type IN ('ICD-9-PCS','ICD9PCS')
        AND code IS NOT NULL AND BTRIM(code) <> ''
      ON CONFLICT (icd_9_pcs) DO UPDATE
        SET long_description  = COALESCE(EXCLUDED.long_description,  %I.icd_9_pcs.long_description),
            short_description = COALESCE(EXCLUDED.short_description, %I.icd_9_pcs.short_description);
    $f$, :'terminology_schema', :'terminology_schema', :'terminology_schema', :'terminology_schema');
  END IF;
END$$;

CREATE INDEX IF NOT EXISTS icd_9_pcs_short_idx ON :"terminology_schema".icd_9_pcs (short_description);
COMMENT ON TABLE  :"terminology_schema".icd_9_pcs IS 'ICD-9-PCS procedure terminology.';
COMMENT ON COLUMN :"terminology_schema".icd_9_pcs.icd_9_pcs IS 'The ICD-9-PCS code.';
