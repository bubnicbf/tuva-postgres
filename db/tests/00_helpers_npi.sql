-- db/tests/00_helpers_npi.sql
-- Provides a Postgres function to validate NPIs using the Luhn algorithm
-- with the '80840' prefix per NPPES spec.
-- Expects psql var: :"schema"
SET search_path TO :"schema", public;

CREATE OR REPLACE FUNCTION :"schema".is_valid_npi(npi_input text)
RETURNS boolean
LANGUAGE plpgsql
AS $$
DECLARE
  digits       text;
  base9        text;
  check_digit  int;
  payload      text;  -- '80840' || first 9 digits of NPI
  len          int;
  i            int;
  d            int;
  s            int := 0;
BEGIN
  IF npi_input IS NULL THEN
    RETURN false;
  END IF;

  -- Keep digits only
  digits := regexp_replace(npi_input, '\D', '', 'g');

  -- Must be 10 digits
  IF length(digits) <> 10 THEN
    RETURN false;
  END IF;

  base9       := substr(digits, 1, 9);
  check_digit := substr(digits, 10, 1)::int;
  payload     := '80840' || base9;
  len         := length(payload);

  -- Luhn checksum over payload, doubling every odd position from the right
  FOR i IN 1..len LOOP
    d := substr(payload, len - i + 1, 1)::int;  -- right to left
    IF (i % 2) = 1 THEN
      d := d * 2;
      IF d > 9 THEN d := d - 9; END IF;
    END IF;
    s := s + d;
  END LOOP;

  RETURN ((10 - (s % 10)) % 10) = check_digit;
END;
$$;
