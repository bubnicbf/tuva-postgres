-- db/terminology/gender.sql
-- Terminology: Gender (single-column value set).
-- Uses psql var :"terminology_schema"

-- 1) Target table
CREATE TABLE IF NOT EXISTS :"terminology_schema".gender (
  gender varchar PRIMARY KEY
);

-- 2) Optional backfill from legacy :"terminology_schema".sex
DO $$
DECLARE
  has_sex_table  boolean;
  has_col_code   boolean;
  has_col_sex    boolean;
BEGIN
  SELECT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema = :'terminology_schema' AND table_name = 'sex'
  ) INTO has_sex_table;

  IF has_sex_table THEN
    SELECT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = :'terminology_schema' AND table_name = 'sex' AND column_name = 'code'
    ) INTO has_col_code;

    SELECT EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = :'terminology_schema' AND table_name = 'sex' AND column_name = 'sex'
    ) INTO has_col_sex;

    IF has_col_code THEN
      EXECUTE format($f$
        INSERT INTO %I.gender(gender)
        SELECT DISTINCT NULLIF(BTRIM(code),'')
        FROM %I.sex
        WHERE code IS NOT NULL AND BTRIM(code) <> ''
        ON CONFLICT (gender) DO NOTHING
      $f$, :'terminology_schema', :'terminology_schema');
    END IF;

    IF has_col_sex THEN
      EXECUTE format($f$
        INSERT INTO %I.gender(gender)
        SELECT DISTINCT NULLIF(BTRIM(sex),'')
        FROM %I.sex
        WHERE sex IS NOT NULL AND BTRIM(sex) <> ''
        ON CONFLICT (gender) DO NOTHING
      $f$, :'terminology_schema', :'terminology_schema');
    END IF;
  END IF;
END$$;

-- 3) Comments
COMMENT ON TABLE  :"terminology_schema".gender IS 'Terminology: gender codes (single column).';
COMMENT ON COLUMN :"terminology_schema".gender.gender IS 'The gender code.';
