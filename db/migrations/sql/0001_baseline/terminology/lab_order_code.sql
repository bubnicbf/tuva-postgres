-- Large dictionary of lab ORDER codes (often LOINC "order" or local catalog).
CREATE TABLE IF NOT EXISTS :"terminology_schema".lab_order_code (
  code_type            varchar NOT NULL,     -- FK (soft) -> lab_order_code_type.code
  code                 varchar NOT NULL,
  description          varchar,
  effective_start_date date,
  effective_end_date   date,
  terminology_source   varchar,              -- e.g., 'LOINC', 'adapter://...'
  terminology_version  varchar,
  PRIMARY KEY (code_type, code)
);
