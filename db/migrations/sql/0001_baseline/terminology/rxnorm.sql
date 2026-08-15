-- RxNorm concept table (minimal fields for enrichment)
CREATE TABLE IF NOT EXISTS :"terminology_schema".rxnorm (
  rxnorm_code          varchar PRIMARY KEY,  -- RxCUI
  preferred_name       varchar,
  term_type            varchar,              -- e.g., 'SCD','SBD','IN','PIN'
  suppress             varchar,              -- 'Y' if suppressed
  rxnorm_version       varchar,
  effective_start_date date,
  effective_end_date   date
);

CREATE INDEX IF NOT EXISTS rxnorm_name_idx ON :"terminology_schema".rxnorm (preferred_name);
