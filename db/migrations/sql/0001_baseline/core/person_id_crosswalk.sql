-- db/tables/person_id_crosswalk.sql
-- Links source-specific identifiers (patient_id/member_id + payer/plan) to canonical person_id.
-- Uses psql var :"schema" supplied by the wrapper.

CREATE TABLE IF NOT EXISTS :"schema".person_id_crosswalk (
  person_id     varchar NOT NULL,   -- canonical person
  patient_id    varchar,            -- clinical system id
  member_id     varchar,            -- payer product/plan id
  payer         varchar,            -- payer name
  plan          varchar,            -- plan/subcontract name
  data_source   varchar,            -- provenance tag (adapter/source)

  -- Guardrail: require at least one linking handle besides person_id
  CONSTRAINT pxw_handle_present CHECK (
    patient_id IS NOT NULL
    OR member_id IS NOT NULL
  )
);

-- FK to patient (deferrable so you can load in any order)
-- Guarded so re-running this file after the constraint already exists is a no-op.
DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class r ON r.oid = c.conrelid
    JOIN pg_namespace n ON n.oid = r.relnamespace
    WHERE c.contype = 'f'
      AND c.conname = 'pxw_person_fk'
      AND r.relname = 'person_id_crosswalk'
      AND n.nspname = :'schema'
  ) THEN
    EXECUTE format(
      'ALTER TABLE %I.person_id_crosswalk ADD CONSTRAINT pxw_person_fk FOREIGN KEY (person_id) REFERENCES %I.patient(person_id) DEFERRABLE INITIALLY DEFERRED',
      :'schema', :'schema'
    );
  END IF;
END$$;

-- De-duplication: treat NULLs as empty for uniqueness to prevent accidental dup rows
-- (Postgres UNIQUE treats NULLs as distinct; use COALESCE in an expression index)
CREATE UNIQUE INDEX IF NOT EXISTS pxw_unique_key
  ON :"schema".person_id_crosswalk (
    person_id,
    COALESCE(patient_id, ''),
    COALESCE(member_id, ''),
    COALESCE(payer, ''),
    COALESCE(plan, ''),
    COALESCE(data_source, '')
  );

-- Access pattern indexes
CREATE INDEX IF NOT EXISTS pxw_by_patient
  ON :"schema".person_id_crosswalk (COALESCE(patient_id, ''), COALESCE(data_source, ''));

CREATE INDEX IF NOT EXISTS pxw_by_member
  ON :"schema".person_id_crosswalk (COALESCE(member_id, ''), COALESCE(payer, ''), COALESCE(plan, ''));

CREATE INDEX IF NOT EXISTS pxw_by_person
  ON :"schema".person_id_crosswalk (person_id);
