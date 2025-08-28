-- Single table with level for flexibility (1/2)
CREATE TABLE IF NOT EXISTS :"terminology_schema".service_category (
  level   integer NOT NULL,        -- 1 or 2
  code    varchar NOT NULL,
  display varchar NOT NULL,
  system  varchar,
  PRIMARY KEY (level, code)
);
