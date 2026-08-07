{# Helpers that turn dbt's stored test failures into a quarantine a resolver can reason about. #}

{% macro row_key(columns) %}
    {#- A stable identity for a row, so a stored failure can be matched back to it.

        Every column is cast to VARCHAR first. `concat_ws` takes strings and nothing else, and
        the columns handed to it here are dates and decimals — `FUNCTION_NOT_FOUND: Unexpected
        parameters (varchar(1), varchar, date, decimal(18,4)) for function concat_ws`. Casting
        at the call site rather than asking each model to pre-format keeps the key's definition
        in one place, which matters because two models disagreeing about how a date becomes a
        string would produce two different identities for the same row. -#}
    LOWER(TO_HEX(MD5(TO_UTF8(CONCAT_WS(
        '|'
        {%- for column in columns -%}
        , CAST({{ column }} AS VARCHAR)
        {%- endfor -%}
    )))))
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
        tomorrow is included without anybody editing a list.

        **This is a skeleton, and saying so is the point.** Every branch below emits
        `WHERE 1 = 0`, so `all_failures` is created with the right shape and no rows — it
        names the tests that ran and reports none of their failures. `quarantined_keys`
        therefore contributes nothing, and a row is marked quarantined in gold only because
        the ingestion already said so.

        Finishing it means selecting from each audit table rather than declaring it, and that
        needs a `row_key` on the staging models so a stored failure can be matched back to the
        row it came from. Until then `E_UPSTREAM_QUARANTINE` rests on the upstream marker
        alone, which is a narrower claim than the one this repository makes elsewhere — and a
        narrower claim stated is better than a wider one implied. -#}
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
                {#- `CAST(... AS TIMESTAMP)` for `detected_at`, not bare `CURRENT_TIMESTAMP`.
                    Athena returns `timestamp(3) with time zone` and a Hive view refuses the
                    zone. Every model in the run had already built when this failed, which is
                    the worst place for it: `PASS=57 ERROR=0` on the line above the error. -#}
                {% do selects.append(
                    "SELECT '" ~ model_name ~ "' AS model_name, '" ~ node.name ~ "' AS rule, "
                    ~ "CAST(NULL AS VARCHAR) AS tenant_id, "
                    ~ "CAST(NULL AS VARCHAR) AS row_key, CAST(NULL AS VARCHAR) AS payload, "
                    ~ "CAST(CURRENT_TIMESTAMP AS TIMESTAMP) AS detected_at WHERE 1 = 0"
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
