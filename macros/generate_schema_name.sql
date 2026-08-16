{#
    Standard dbt "use the custom schema exactly, never prefixed with the
    target schema" override (see dbt's own docs on custom schemas). This
    project relies on it so `models/final/*.sql`'s `+schema:
    "{{ var('input_layer_schema') }}"` config (see dbt_project.yml)
    lands those models in a schema literally named after
    INPUT_LAYER_SCHEMA -- not `{target_schema}_{input_layer_schema}` --
    since that is the schema name the Tuva package's ref()s must resolve
    against.
#}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- set default_schema = target.schema -%}
    {%- if custom_schema_name is none -%}
        {{ default_schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
