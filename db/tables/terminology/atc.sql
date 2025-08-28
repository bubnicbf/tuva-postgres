-- WHO ATC classification (A.. level 1-5)
CREATE TABLE IF NOT EXISTS :"terminology_schema".atc (
  code                 varchar PRIMARY KEY,  -- e.g., 'C07AB02'
  description          varchar,
  level                integer,              -- 1..5 (optional if you load full tree)
  who_version          varchar,
  effective_start_date date,
  effective_end_date   date
);

CREATE INDEX IF NOT EXISTS atc_desc_idx ON :"terminology_schema".atc (description);
