-- CDC CVX vaccine codes (large; hydrate via adapter/public source).
CREATE TABLE IF NOT EXISTS :"terminology_schema".cvx (
  cvx_code              varchar PRIMARY KEY,  -- e.g., '207'
  short_description     varchar,
  full_description      varchar,
  status                varchar,              -- e.g., 'Active','Inactive'
  notes                 varchar,
  cvx_version           varchar,
  effective_start_date  date,
  effective_end_date    date
);

CREATE INDEX IF NOT EXISTS cvx_desc_idx ON :"terminology_schema".cvx (short_description);
