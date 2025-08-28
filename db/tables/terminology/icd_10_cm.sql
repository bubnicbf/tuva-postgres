-- db/terminology/icd_10_cm.sql
-- ICD-10-CM: diagnosis codes with header flag.
CREATE TABLE IF NOT EXISTS :"terminology_schema".icd_10_cm (
  icd_10_cm          varchar PRIMARY KEY,
  header_flag        varchar,  -- '0' or '1' (per spec)
  short_description  varchar,
  long_description   varchar,
  CONSTRAINT icd10cm_header_flag_01 CHECK (header_flag IS NULL OR header_flag IN ('0','1'))
);

ALTER TABLE :"terminology_schema".icd_10_cm
  ADD COLUMN IF NOT EXISTS icd_10_cm         varchar,
  ADD COLUMN IF NOT EXISTS header_flag       varchar,
  ADD COLUMN IF NOT EXISTS short_description varchar,
  ADD COLUMN IF NOT EXISTS long_description  varchar;

-- Optional legacy backfill
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_schema = :'terminology_schema' AND table_name = 'diagnosis_code')
  THEN
    EXECUTE format($f$
      INSERT INTO %I.icd_10_cm (icd_10_cm, header_flag, short_description, long_description)
      SELECT DISTINCT NULLIF(BTRIM(code), ''),
             NULLIF(BTRIM(header_flag), ''),
             NULLIF(BTRIM(short_description), ''),
             NULLIF(BTRIM(long_description), '')
      FROM %I.diagnosis_code
      WHERE code_type IN ('ICD-10-CM','ICD10CM')
        AND code IS NOT NULL AND BTRIM(code) <> ''
      ON CONFLICT (icd_10_cm) DO UPDATE
        SET header_flag       = COALESCE(EXCLUDED.header_flag,       %I.icd_10_cm.header_flag),
            short_description = COALESCE(EXCLUDED.short_description, %I.icd_10_cm.short_description),
            long_description  = COALESCE(EXCLUDED.long_description,  %I.icd_10_cm.long_description);
    $f$, :'terminology_schema', :'terminology_schema', :'terminology_schema', :'terminology_schema');
  END IF;
END$$;

CREATE INDEX IF NOT EXISTS icd_10_cm_short_idx ON :"terminology_schema".icd_10_cm (short_description);
COMMENT ON TABLE  :"terminology_schema".icd_10_cm IS 'ICD-10-CM diagnosis terminology (header_flag ''1'' = header/non-billable).';
COMMENT ON COLUMN :"terminology_schema".icd_10_cm.icd_10_cm IS 'Alpha-Numeric ICD-10-CM Code.';
COMMENT ON COLUMN :"terminology_schema".icd_10_cm.header_flag IS '1 = header (non-billable), 0 = billable.';
