-- db/terminology/ndc.sql
-- Uses psql var :"terminology_schema".
CREATE TABLE IF NOT EXISTS :"terminology_schema".ndc (
  ndc11                 varchar PRIMARY KEY,     -- 11-digit normalized NDC (no dashes)
  ndc10                 varchar,                 -- original 10-digit format if applicable
  description           varchar,
  generic_name          varchar,
  brand_name            varchar,
  strength              varchar,
  dosage_form           varchar,
  route                 varchar,
  labeler_name          varchar,
  market_start_date     date,
  market_end_date       date,
  terminology_source    varchar,                 -- e.g., "FDA_NDC", "adapter://..."
  terminology_version   varchar
);

CREATE INDEX IF NOT EXISTS ndc_brand_idx   ON :"terminology_schema".ndc (brand_name);
CREATE INDEX IF NOT EXISTS ndc_generic_idx ON :"terminology_schema".ndc (generic_name);
