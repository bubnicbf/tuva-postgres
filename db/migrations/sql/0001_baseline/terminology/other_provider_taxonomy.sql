-- db/terminology/other_provider_taxonomy.sql
-- Terminology: NPI ↔ taxonomy mappings (with Medicare specialty and primary flag)
-- Uses psql var :"terminology_schema"

-- 1) Target table
CREATE TABLE IF NOT EXISTS :"terminology_schema".other_provider_taxonomy (
  npi                      varchar NOT NULL,  -- 10-digit NPI (digits only)
  taxonomy_code            varchar NOT NULL,  -- provider taxonomy code
  medicare_specialty_code  varchar,          -- Medicare specialty code
  description              varchar,          -- taxonomy description
  primary_flag             integer,          -- 1 = primary, 0 = not primary
  CONSTRAINT opt_primary_01 CHECK (primary_flag IS NULL OR primary_flag IN (0,1)),
  CONSTRAINT opt_pk PRIMARY KEY (npi, taxonomy_code)
);

-- 2) Legacy-safe alignment (no-op if columns already exist)
ALTER TABLE :"terminology_schema".other_provider_taxonomy
  ADD COLUMN IF NOT EXISTS npi                     varchar,
  ADD COLUMN IF NOT EXISTS taxonomy_code           varchar,
  ADD COLUMN IF NOT EXISTS medicare_specialty_code varchar,
  ADD COLUMN IF NOT EXISTS description             varchar,
  ADD COLUMN IF NOT EXISTS primary_flag            integer;

-- 3) Helpful indexes
CREATE INDEX IF NOT EXISTS opt_npi_idx           ON :"terminology_schema".other_provider_taxonomy (npi);
CREATE INDEX IF NOT EXISTS opt_taxonomy_idx      ON :"terminology_schema".other_provider_taxonomy (taxonomy_code);

-- Enforce at most one primary taxonomy per NPI (partial unique)
CREATE UNIQUE INDEX IF NOT EXISTS opt_one_primary_per_npi
  ON :"terminology_schema".other_provider_taxonomy (npi)
  WHERE primary_flag = 1;

-- 4) Documentation
COMMENT ON TABLE  :"terminology_schema".other_provider_taxonomy
  IS 'Terminology: NPI to provider taxonomy mapping with Medicare specialty and primary flag.';
COMMENT ON COLUMN :"terminology_schema".other_provider_taxonomy.npi
  IS 'The National Provider Identifier (NPI).';
COMMENT ON COLUMN :"terminology_schema".other_provider_taxonomy.taxonomy_code
  IS 'The provider taxonomy code.';
COMMENT ON COLUMN :"terminology_schema".other_provider_taxonomy.medicare_specialty_code
  IS 'The Medicare specialty code.';
COMMENT ON COLUMN :"terminology_schema".other_provider_taxonomy.description
  IS 'Description of the provider taxonomy.';
COMMENT ON COLUMN :"terminology_schema".other_provider_taxonomy.primary_flag
  IS 'Indicates if this is the primary taxonomy (1) or not (0).';
