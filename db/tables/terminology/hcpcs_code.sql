-- Big CPT/HCPCS dictionary; hydrate from cloud storage per adapter.
CREATE TABLE IF NOT EXISTS :"terminology_schema".hcpcs_code (
  code        varchar PRIMARY KEY,     -- CPT or HCPCS Level II
  description varchar,
  category    varchar,                 -- e.g., 'CPT', 'HCPCS', 'CPT-cat-II', etc.
  effective_start_date date,
  effective_end_date   date,
  terminology_source   varchar,        -- e.g., 'AMA_CPT', 'CMS_HCPCS'
  terminology_version  varchar
);
