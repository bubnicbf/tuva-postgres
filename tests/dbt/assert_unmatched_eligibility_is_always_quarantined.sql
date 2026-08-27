-- Proves the invariant documented on models/intermediate/
-- int_eligibility_resolved.sql's `resolved_person_id` column: a row is
-- missing a resolved person_id if and only if its quarantine_reason is
-- 'unmatched_member_key'. A dbt test FAILS when this returns any rows.

select *
from {{ ref('int_eligibility_resolved') }}
where (resolved_person_id is null) is distinct from (quarantine_reason = 'unmatched_member_key')
