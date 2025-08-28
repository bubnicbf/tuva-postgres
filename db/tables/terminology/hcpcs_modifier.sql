CREATE TABLE IF NOT EXISTS :"terminology_schema".hcpcs_modifier (
  code    varchar PRIMARY KEY,     -- e.g., '59','25','LT','RT','TC','26'
  display varchar NOT NULL,
  system  varchar
);
