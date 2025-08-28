-- db/terminology/diagnosis_code.sql
-- Big dictionary; do NOT seed. Hydrate from public cloud storage per adapter.
-- Indexed by (code_type, code) for fast lookups.
CREATE TABLE IF NOT EXISTS :"terminology_schema".diagnosis_code (
  code_type   varchar NOT NULL,     -- FK to diagnosis_code_type.code
  code        varchar NOT NULL,
  description varchar,
  effective_start_date date,
  effective_end_date   date,
  terminology_source   varchar,     -- e.g., 'ICD10CM_NCHS'
  terminology_version  varchar,     -- e.g., '2025-10-01'

  PRIMARY KEY (code_type, code)
);
