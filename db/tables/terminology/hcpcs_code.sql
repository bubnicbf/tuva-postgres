-- db/terminology/hcpcs_code.sql
-- Terminology: HCPCS Level II (code → seqnum/recid + short/long descriptions).
-- Uses psql var :"terminology_schema"

-- 1) Target table (final shape)
CREATE TABLE IF NOT EXISTS :"terminology_schema".hcpcs_code (
  hcpcs              varchar,
  seqnum             varchar,
  recid              varchar,
  long_description   varchar,
  short_description  varchar
);

-- 2) Align columns for legacy installs (safe to re-run)
ALTER TABLE :"terminology_schema".hcpcs_code
  ADD COLUMN IF NOT EXISTS hcpcs             varchar,
  ADD COLUMN IF NOT EXISTS seqnum            varchar,
  ADD COLUMN IF NOT EXISTS recid             varchar,
  ADD COLUMN IF NOT EXISTS long_description  varchar,
  ADD COLUMN IF NOT EXISTS short_description varchar;

-- 3) Ensure composite primary key on (hcpcs, seqnum, recid)
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class r ON r.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = r.relnamespace
    WHERE c.contype = 'p'
      AND c.conname = 'hcpcs_code_pkey'
      AND r.relname = 'hcpcs_code'
      AND n.nspname = :'terminology_schema'
  ) THEN
    -- Will fail if any of the key columns contain NULLs; ensure your seed respects non-null keys.
    EXECUTE format('ALTER TABLE %I.hcpcs_code ADD CONSTRAINT hcpcs_code_pkey PRIMARY KEY (hcpcs, seqnum, recid)',
                   :'terminology_schema');
  END IF;
END$$;

-- 4) Helpful indexes for lookups
CREATE INDEX IF NOT EXISTS hcpcs_code_hcpcs_idx
  ON :"terminology_schema".hcpcs_code (hcpcs);
CREATE INDEX IF NOT EXISTS hcpcs_code_short_desc_idx
  ON :"terminology_schema".hcpcs_code (short_description);

-- 5) Documentation
COMMENT ON TABLE  :"terminology_schema".hcpcs_code
  IS 'HCPCS Level II terminology: (hcpcs, seqnum, recid) → short/long descriptions.';
COMMENT ON COLUMN :"terminology_schema".hcpcs_code.hcpcs
  IS 'The HCPCS Level II code.';
COMMENT ON COLUMN :"terminology_schema".hcpcs_code.seqnum
  IS 'Sequence number (line sequence within the HCPCS source feed).';
COMMENT ON COLUMN :"terminology_schema".hcpcs_code.recid
  IS 'Record identifier (record type within the HCPCS source feed).';
COMMENT ON COLUMN :"terminology_schema".hcpcs_code.long_description
  IS
