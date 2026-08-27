{#
    Deterministic institutional/professional/undetermined claim-type
    precedence rule (docs/CLAIMS_MAPPING_DECISIONS.md, decision 3;
    executable reference: src/tuva_ingest/claims_mapping.
    derive_claim_type). This is genuinely multi-signal, cross-field
    business logic -- not a row-local cast -- so it is applied in
    models/intermediate/int_medical_claim_lines.sql, never in staging.

    Evaluated in this exact order (an explicit, already-classified
    source signal is trusted ahead of the vendor form-code signal --
    documented as a "fourth, higher-precedence rule" extension in
    docs/CLAIMS_MAPPING_DECISIONS.md decision 3 -- so the existing
    "tuva" test source's direct `claim_type` field keeps working
    unchanged, and a real vendor's own explicit indicator, if one is
    ever supplied, can be threaded in the same way):

    1. `source_claim_type_hint` already one of institutional/
       professional/undetermined -> use it as-is (explicit source
       signal, highest precedence).
    2. `claim_form_code = 'UB04'` -> institutional; `= 'CMS1500'` ->
       professional (the primary vendor-shaped signal).
    3. Otherwise `bill_type_code` populated -> institutional (a UB-04
       type-of-bill code only exists on institutional claims).
    4. Otherwise `place_of_service_code` populated -> professional (a
       CMS-1500 POS code only exists on professional claims).
    5. Otherwise -> 'undetermined' (never NULL; matches models/final/
       schema.yml's existing accepted_values test).

    All four expr arguments must already be raw_field()/trim()-extracted
    text (or NULL) -- this macro does no further extraction itself.
#}
{% macro derive_claim_type(source_claim_type_hint, claim_form_code, bill_type_code, place_of_service_code) -%}
    (case
        when {{ source_claim_type_hint }} in ('institutional', 'professional', 'undetermined')
            then {{ source_claim_type_hint }}
        when {{ claim_form_code }} = 'UB04' then 'institutional'
        when {{ claim_form_code }} = 'CMS1500' then 'professional'
        when {{ bill_type_code }} is not null then 'institutional'
        when {{ place_of_service_code }} is not null then 'professional'
        else 'undetermined'
    end)
{%- endmacro %}
