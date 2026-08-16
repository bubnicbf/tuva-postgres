{#
    PostgreSQL has no TRY_CAST/SAFE_CAST -- an ordinary `::date`/
    `::numeric` cast on a malformed value raises and aborts the whole
    `dbt build`. These two macros give staging models (models/staging/
    *.sql) a documented, conservative alternative: validate the text
    shape with a regex first, and only cast when it matches; anything
    else (blank, malformed, source-specific placeholder text) becomes a
    typed NULL rather than a build failure -- exactly the "typed null ...
    for unavailable/unmappable fields" behavior the Input Layer contract
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
