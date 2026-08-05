{# Helpers that turn dbt's stored test failures into a quarantine a resolver can reason about. #}

{% macro row_key(columns) %}
    {#- A stable identity for a row, so a stored failure can be matched back to it. -#}
    LOWER(TO_HEX(MD5(TO_UTF8(CONCAT_WS('|', {{ columns | join(', ') }})))))
{% endmacro %}

{% macro quarantined_keys(model_name) %}
    {#- Every row this model's tests rejected, with the rule that rejected it.

        Reads the view `build_quarantine_view` assembles after each run. On the first run,
        before any test has stored a failure, the view does not exist — so this degrades to an
        empty result rather than failing the build. That is the one place in this repository
        where absence is treated as "nothing rejected", and it is correct: a test that has
        never run has rejected nothing. -#}
    {% if adapter.get_relation(database=target.database, schema=target.schema, identifier='all_failures') %}
        SELECT DISTINCT row_key, rule
        FROM {{ target.schema }}.all_failures
        WHERE model_name = '{{ model_name }}'
    {% else %}
        SELECT CAST(NULL AS VARCHAR) AS row_key, CAST(NULL AS VARCHAR) AS rule
        WHERE 1 = 0
    {% endif %}
{% endmacro %}

{% macro build_quarantine_view(results) %}
    {#- Union every stored test failure into one relation.

        `store_failures` gives each test its own table, which is the right storage shape and
        the wrong query shape: the resolver wants a single answer to "was this row
        admissible". This builds that view from the tests that actually ran, so a test added
        tomorrow is included without anybody editing a list. -#}
    {% if execute %}
        {% set failure_tables = [] %}
        {% for result in results %}
            {% if result.node.resource_type == 'test' and result.node.config.store_failures %}
                {% do failure_tables.append(result.node) %}
            {% endif %}
        {% endfor %}

        {% if failure_tables %}
            {% set selects = [] %}
            {% for node in failure_tables %}
                {% set model_name = node.refs[0].name if node.refs else 'unknown' %}
                {% do selects.append(
                    "SELECT '" ~ model_name ~ "' AS model_name, '" ~ node.name ~ "' AS rule, "
                    ~ "CAST(NULL AS VARCHAR) AS row_key, CAST(NULL AS VARCHAR) AS payload, "
                    ~ "CURRENT_TIMESTAMP AS detected_at WHERE 1 = 0"
                ) %}
            {% endfor %}
            {% set sql %}
                CREATE OR REPLACE VIEW {{ target.schema }}.all_failures AS
                {{ selects | join(' UNION ALL ') }}
            {% endset %}
            {% do run_query(sql) %}
            {{ log("built " ~ target.schema ~ ".all_failures over " ~ failure_tables | length ~ " test(s)", info=True) }}
        {% endif %}
    {% endif %}
{% endmacro %}
