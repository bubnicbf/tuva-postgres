{#
    Extract a single field from a raw_row JSONB column as trimmed text,
    normalizing an empty/whitespace-only string to SQL NULL. Every
    staging model (models/staging/*.sql) uses this instead of a bare
    `->>` operator so "empty string" and "missing key" are always
    treated identically -- exactly the "normalize ... empty strings"
    behavior staging models are responsible for (see README.md
    "Architecture").
#}
{% macro raw_field(column, key) -%}
    nullif(trim(both from ({{ column }} ->> '{{ key }}')), '')
{%- endmacro %}
