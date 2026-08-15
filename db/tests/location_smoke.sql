-- db/tests/location_smoke.sql
-- Expects psql var: :"schema"
SET search_path TO :"schema", public;

-- Helper: NPI validator (Luhn on '80840' + NPI(10))
-- Safe to redefine; used by practitioner/location tests.
CREATE OR REPLACE FUNCTION validate_npi_10(npi_in text)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
  digits text;
  full   text;
  len    int;
  i      int;
  d      int;
  pos    int;
  sum    int := 0;
  dbl    int;
BEGIN
  IF npi_in IS NULL THEN
    RETURN NULL;
  END IF;

  digits := regexp_replace(npi_in, '\D', '', 'g');
  IF length(digits) <> 10 THEN
    RETURN FALSE;
  END IF;

  full := '80840' || digits;         -- 15-digit string for Luhn
  len  := length(full);

  FOR i IN REVERSE 1..len LOOP
    d := substr(full, i, 1)::int;
    pos := len - i + 1;              -- 1 = rightmost
    IF (pos % 2) = 0 THEN            -- double every 2nd from right
      dbl := d * 2;
      IF dbl > 9 THEN dbl := dbl - 9; END IF;
      sum := sum + dbl;
    ELSE
      sum := sum + d;
    END IF;
  END LOOP;

  RETURN (sum % 10 = 0);
END;
$$;

-- 1) table has rows
SELECT 'loc_has_rows' AS test, COUNT(*) > 0 AS pass, COUNT(*) AS row_count
FROM location;

-- 2) PK not null
SELECT 'loc_pk_not_null' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM location
WHERE location_id IS NULL;

-- 3) PK unique
SELECT 'loc_pk_unique' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS dup_key_count
FROM (
  SELECT location_id FROM location GROUP BY location_id HAVING COUNT(*) > 1
) d;

-- 4) NPI format: digits-only length 10 (when present)
SELECT 'loc_npi_digits_len_10' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM location
WHERE npi IS NOT NULL
  AND npi !~ '^\d{10}$';

-- 5) NPI Luhn valid (when present)
SELECT 'loc_npi_luhn_valid' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM location
WHERE npi IS NOT NULL
  AND NOT validate_npi_10(npi);

-- 6) Geo ranges: latitude in [-90, 90] (when present)
SELECT 'loc_lat_range' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM location
WHERE latitude IS NOT NULL
  AND (latitude < -90 OR latitude > 90);

-- 7) Geo ranges: longitude in [-180, 180] (when present)
SELECT 'loc_lon_range' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM location
WHERE longitude IS NOT NULL
  AND (longitude < -180 OR longitude > 180);

-- 8) ZIP normalization: 5 or 9 digits (hyphen optional) (when present)
SELECT 'loc_zip_pattern' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM location
WHERE zip_code IS NOT NULL
  AND zip_code !~ '^\d{5}(-?\d{4})?$';

-- 9) ZIP not all zeros (when present)
SELECT 'loc_zip_not_all_zero' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM location
WHERE zip_code IS NOT NULL
  AND (regexp_replace(zip_code, '\D', '', 'g') ~ '^[0]+$');

-- 10) State normalization: exactly two UPPER letters (when present)
SELECT 'loc_state_two_upper' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM location
WHERE state IS NOT NULL
  AND state !~ '^[A-Z]{2}$';

-- 11) State in valid USPS set (when present) [includes DC + territories]
WITH valid(state) AS (
  VALUES
    ('AL'),('AK'),('AZ'),('AR'),('CA'),('CO'),('CT'),('DE'),('FL'),('GA'),
    ('HI'),('ID'),('IL'),('IN'),('IA'),('KS'),('KY'),('LA'),('ME'),('MD'),
    ('MA'),('MI'),('MN'),('MS'),('MO'),('MT'),('NE'),('NV'),('NH'),('NJ'),
    ('NM'),('NY'),('NC'),('ND'),('OH'),('OK'),('OR'),('PA'),('RI'),('SC'),
    ('SD'),('TN'),('TX'),('UT'),('VT'),('VA'),('WA'),('WV'),('WI'),('WY'),
    ('DC'),('PR'),('VI'),('GU'),('AS'),('MP')
)
SELECT 'loc_state_in_valid_set' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM location l
LEFT JOIN valid v ON upper(l.state) = v.state
WHERE l.state IS NOT NULL
  AND v.state IS NULL;

-- 12) Cross-table presence: appointment.location_id must exist in location (when referenced)
SELECT 'loc_appt_location_fk_exists' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM appointment a
LEFT JOIN location l ON l.location_id = a.location_id
WHERE a.location_id IS NOT NULL
  AND l.location_id IS NULL;

-- 13) Cross-table presence: encounter.facility_id should exist in location (soft)
--     (Treat facility_id as a location_id if your pipelines map facilities to locations.)
SELECT 'loc_enc_facility_in_location' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS missing_fk
FROM encounter e
LEFT JOIN location l ON l.location_id = e.facility_id
WHERE e.facility_id IS NOT NULL
  AND l.location_id IS NULL;

-- 14) tuva_last_run not in the future (when present)
SELECT 'loc_tuva_last_run_not_future' AS test, COUNT(*) = 0 AS pass, COUNT(*) AS fail_count
FROM location
WHERE tuva_last_run IS NOT NULL
  AND tuva_last_run > (NOW()::timestamp without time zone);
