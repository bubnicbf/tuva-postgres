CREATE TABLE IF NOT EXISTS :"terminology_schema".place_of_service (
  code    varchar PRIMARY KEY,     -- CMS POS code, e.g., '11', '21', '23'
  display varchar NOT NULL,
  system  varchar                  -- e.g., 'https://www.cms.gov/Medicare/Coding/place-of-service-codes'
);
