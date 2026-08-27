{{
  config(
    materialized='view'
  )
}}

-- Deterministic member_key -> person_id crosswalk (docs/
-- CLAIMS_MAPPING_DECISIONS.md decision 5; executable reference: src/
-- tuva_ingest/claims_mapping.load_crosswalk). Multiple member_key
-- values may resolve to the same person_id (an ordinary many-to-one
-- mapping, e.g. a documented identifier merge) -- never the reverse.
--
-- KNOWN SOURCE-CONTRACT LIMITATION (see docs/RUNBOOK.md "Known
-- limitations" and docs/CLAIMS_MAPPING_DECISIONS.md "Readiness" item 2):
-- this repository's raw ingestion contract (models/sources.yml) has no
-- fourth raw table/loader for a real vendor-delivered member crosswalk
-- feed today -- only eligibility/medical_claim/pharmacy_claim exist.
-- Fabricating a production crosswalk raw table with no real loader
-- behind it would be worse than being explicit about the gap, so this
-- model instead refs the versioned, deterministic
-- seeds/member_crosswalk_seed.csv (dbt seed -- see seeds/schema.yml)
-- as an INTERIM crosswalk source: it makes crosswalk resolution real,
-- tested, and deterministic against a representative sample today,
-- without inventing a raw ingestion path that does not exist. The
-- moment a real vendor crosswalk feed is ingested into its own raw
-- table, this model's single `from` below is the only line that needs
-- to change (to source('raw', 'member_crosswalk'), written using the
-- Jinja source() call this comment deliberately does not spell out
-- literally, so this docstring itself is never mistaken for a live
-- dependency by dbt's Jinja renderer) -- no downstream model depends on the crosswalk's
-- physical origin.
--
-- Also does NOT yet support: identifier splits (one member_key
-- resolving to more than one person_id over time) or identifier reuse
-- (the same member_key reissued to a different person later) -- both
-- would require a time-versioned crosswalk (member_key, person_id,
-- effective date range), which is a real vendor-data question this
-- repository cannot answer without a live source (see docs/
-- CLAIMS_MAPPING_DECISIONS.md decision 5).

with crosswalk as (

    select
        nullif(trim(member_key), '') as member_key,
        nullif(trim(person_id), '')  as person_id
    from {{ ref('member_crosswalk_seed') }}

)

select
    member_key,
    person_id
from crosswalk
where member_key is not null
  and person_id is not null
