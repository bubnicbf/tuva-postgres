{#
    Standard dbt "use the custom schema exactly, never prefixed with the
    target schema" override (see dbt's own docs on custom schemas), PLUS
    a routing rule for the pinned Tuva package's own core/mart models
    (see packages.yml, tuva-health/the_tuva_project 0.18.0) into this
    project's analytics_core_schema/analytics_marts_schema vars (see
    dbt_project.yml, config.py's ANALYTICS_CORE_SCHEMA/
    ANALYTICS_MARTS_SCHEMA).

    This project's OWN models keep working exactly as before: `models/
    staging/*.sql`'s `+schema: "{{ var('staging_schema') }}"` and
    `models/final/*.sql`'s `+schema: "{{ var('input_layer_schema') }}"`
    (see dbt_project.yml) land in a schema literally named after that
    var -- never `{target_schema}_{custom_schema_name}` -- exactly like
    before this change.

    For every OTHER package's models (in practice, only the pinned Tuva
    package -- `node.package_name` is never this project's own package
    name for those nodes), this macro additionally remaps the package's
    OWN configured custom_schema_name: a schema named literally "core"
    (Tuva's core data model) or "terminology" routes to
    analytics_core_schema; every other non-none custom_schema_name Tuva
    configures (its various mart schemas) routes to
    analytics_marts_schema.

    IMPORTANT / KNOWN LIMITATION: this repository's own sandboxed
    development/CI environment could not run `dbt deps` (no network
    access to fetch the pinned package), so the exact schema-name
    literals the real tuva-health/the_tuva_project 0.18.0 package
    configures for its own models could not be independently confirmed
    by running `dbt parse`/`dbt list` against the actual installed
    package here. "core"/"terminology" (mapped to analytics_core_schema)
    matches this package's publicly documented model-group naming as of
    this change; every other Tuva-configured schema name is treated as a
    mart and routed to analytics_marts_schema. Before relying on this in
    production, run `dbt deps && dbt list --output json --output-keys
    unique_id,schema` (or `dbt parse`) once you have network access, and
    adjust `_TUVA_CORE_SCHEMA_NAMES` below if any Tuva-configured schema
    name differs from what is assumed here. See docs/RUNBOOK.md "Known
    limitations".
#}
{% set _tuva_core_schema_names = ['core', 'terminology'] %}

{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set default_schema = target.schema -%}
    {%- if custom_schema_name is none -%}
        {{ default_schema }}
    {%- elif node is not none and node.package_name != project_name -%}
        {%- if custom_schema_name | trim in _tuva_core_schema_names -%}
            {{ var('analytics_core_schema', 'analytics_core') }}
        {%- else -%}
            {{ var('analytics_marts_schema', 'analytics_marts') }}
        {%- endif -%}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
