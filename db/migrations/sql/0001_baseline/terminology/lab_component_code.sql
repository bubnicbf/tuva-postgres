-- Large dictionary of lab COMPONENT (analyte) codes (often LOINC "component" or local).
CREATE TABLE IF NOT EXISTS :"terminology_schema".lab_component_code (
  code_type            varchar,              -- free text per spec (may be NULL/LOCAL)
  code                 varchar NOT NULL,
  description          varchar,
  effective_start_date date,
  effective_end_date   date,
  terminology_source   varchar,
  terminology_version  varchar,
  PRIMARY KEY (code, COALESCE(code_type,''))
);
