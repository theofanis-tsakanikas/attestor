{# Helpers that turn dbt's stored test failures into a quarantine a resolver can reason about. #}

{% macro row_key(columns) %}
    {#- A stable identity for a row, so a stored failure can be matched back to it. -#}
    LOWER(TO_HEX(MD5(TO_UTF8(CONCAT_WS('|', {{ columns | join(', ') }})))))
{% endmacro %}

{% macro quarantined_keys(model_name) %}
    {#- Every row this model's tests rejected, with the rule that rejected it.

        `store_failures` puts each failing test's rows in its own table under the quarantine
        schema. Unioning them here is what lets a single `dq_status` column answer "was this
        row admissible" without the resolver knowing which tests exist. -#}
    SELECT DISTINCT
        row_key,
        rule
    FROM {{ target.schema }}_quarantine.all_failures
    WHERE model_name = '{{ model_name }}'
{% endmacro %}
