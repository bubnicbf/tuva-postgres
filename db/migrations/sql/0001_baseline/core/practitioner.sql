-- db/tables/practitioner.sql
-- Core model: one record per practitioner in your datasets.
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".practitioner (
  practitioner_id    varchar PRIMARY KEY,         -- internal stable id
  npi                varchar,                     -- may be NULL or non-unique (e.g., facility vs individual use)
  provider_first_name varchar,
  provider_last_name  varchar,
  practice_affiliation varchar,
  specialty           varchar,
  sub_specialty       varchar,
  data_source         varchar,                    -- provenance tag, adapter/source name
  tuva_last_run       timestamp without time zone -- local time where dbt ran (pretty_time)
);

-- Helpful index for lookups by NPI (not unique by design)
CREATE INDEX IF NOT EXISTS practitioner_npi_idx ON :"schema".practitioner (npi);
