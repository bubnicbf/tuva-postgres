{#
    PostgreSQL has no TRY_CAST/SAFE_CAST -- an ordinary `::date`/
    `::numeric` cast on a malformed value raises and aborts the whole
    `dbt build`. These macros give staging models (models/staging/*.sql)
    a documented, conservative alternative: validate the text shape with
    a regex first, and only cast when it matches; anything else (blank,
    malformed, source-specific placeholder text) becomes a typed NULL
    rather than a build failure -- exactly the "typed null ... for
    unavailable/unmappable fields" behavior the Input Layer contract
    expects (see README.md "Architecture" and models/final/*.sql's
    column docs for which fields this applies to).
#}
{% macro safe_date(expr) -%}
    (case when ({{ expr }}) ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' then ({{ expr }})::date else null end)
{%- endmacro %}

{% macro safe_numeric(expr) -%}
    (case when ({{ expr }}) ~ '^-?[0-9]+(\.[0-9]+)?$' then ({{ expr }})::numeric else null end)
{%- endmacro %}

{% macro safe_integer(expr) -%}
    (case when ({{ expr }}) ~ '^-?[0-9]+$' then ({{ expr }})::integer else null end)
{%- endmacro %}

{#
    Convert a source integer-cents string into a decimal dollar amount,
    matching src/tuva_ingest/claims_mapping.py's `cents_to_amount`
    exactly: strict digits-only (optionally signed) shape validation,
    then a NUMERIC (never float, never integer `//`) division by 100,
    rounded to 2 decimal places. Anything that doesn't match the strict
    shape -- blank, malformed, source-specific placeholder text --
    becomes a typed NULL (a pending/not-yet-adjudicated amount), never a
    silently-defaulted `0`. This is row-local unit conversion, not
    business logic, so it belongs in staging (see docs/
    CLAIMS_MAPPING_DECISIONS.md decision 6 and models/staging/
    stg_medical_claim.sql's header for how paid_cents/allowed_cents/
    charge_cents feed this).

    A negative value is intentional and expected on a void/reversal
    claim line (docs/CLAIMS_MAPPING_DECISIONS.md decision 2) -- this
    macro never clamps or takes an absolute value.
#}
{% macro cents_to_amount(expr) -%}
    (case when ({{ expr }}) ~ '^-?[0-9]+$' then (({{ expr }})::numeric / 100)::numeric(38, 2) else null end)
{%- endmacro %}
