-- db/terminology/icd_9_cm.sql
-- ICD-9-CM: diagnosis codes (code → short/long).
-- Uses psql var :"terminology_schema"

CREATE TABLE IF NOT EXISTS :"terminology_schema".icd_9_cm (
  icd_9_cm           varchar PRIMARY KEY,
  long_description   varchar,
  short_description  varchar
);

-- Align columns (safe re-run)
ALTER TABLE :"terminology_schema".icd_9_cm
  ADD COLUMN IF NOT EXISTS icd_9_cm          varchar,
  ADD COLUMN IF NOT EXISTS long_description  varchar,
  ADD COLUMN IF NOT EXISTS short_description varchar;

-- Optional legacy backfill from diagnosis_code(code_type='ICD-9-CM')
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = :'terminology_schema' AND table_name = 'diagnosis_code')
  THEN
    EXECUTE format($f$
      INSERT INTO %I.icd_9_cm (icd_9_cm, long_description, short_description)
      SELECT DISTINCT NULLIF(BTRIM(code), ''),
             NULLIF(BTRIM(long_description), ''),
             NULLIF(BTRIM(short_description), '')
      FROM %I.diagnosis_code
      WHERE code_type IN ('ICD-9-CM','ICD9CM')
        AND code IS NOT NULL AND BTRIM(code) <> ''
      ON CONFLICT (icd_9_cm) DO UPDATE
        SET long_description  = COALESCE(EXCLUDED.long_description,  %I.icd_9_cm.long_description),
            short_description = COALESCE(EXCLUDED.short_description, %I.icd_9_cm.short_description);
    $f$, :'terminology_schema', :'terminology_schema', :'terminology_schema', :'terminology_schema');
  END IF;
END$$;

CREATE INDEX IF NOT EXISTS icd_9_cm_short_idx ON :"terminology_schema".icd_9_cm (short_description);
COMMENT ON TABLE  :"terminology_schema".icd_9_cm IS 'ICD-9-CM diagnosis terminology.';
COMMENT ON COLUMN :"terminology_schema".icd_9_cm.icd_9_cm IS 'The ICD-9-CM code.';
