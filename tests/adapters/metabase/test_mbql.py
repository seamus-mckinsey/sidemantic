"""MBQL translation tests. These helpers are HTTP-free so a YAML loader can reuse them."""

import ast
from pathlib import Path

import pytest

from sidemantic.adapters import metabase_mbql
from sidemantic.adapters.metabase_mbql import (
    MBQLTranslationError,
    aggregation_sql,
    filter_sql,
    is_complete_query,
    sanitize_identifier,
    translate_dataset_query,
)

MODULE_PATH = Path("sidemantic/adapters/metabase_mbql.py")


def test_module_has_no_http_or_adapter_imports():
    tree = ast.parse(MODULE_PATH.read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    assert imported
    assert all("urllib" not in name and "metabase" not in name for name in imported)
    assert "distinct" not in metabase_mbql._SIMPLE_AGGS
    assert "COUNT_DISTINCT" not in metabase_mbql._SQL_FUNCS.values()


def test_sanitize_identifier():
    assert sanitize_identifier("Total Revenue ($)") == "total_revenue"
    assert sanitize_identifier("123_metric") == "m_123_metric"
    assert sanitize_identifier("my metric name") == "my_metric_name"
    assert sanitize_identifier("") == "unnamed"
    assert sanitize_identifier("___test___") == "test"


def test_simple_aggregations():
    assert aggregation_sql(["sum", ["field", "amount", None]]).sql == "SUM(amount)"
    assert aggregation_sql(["count"]).sql == "COUNT(*)"
    assert aggregation_sql(["count", ["field", "id", None]]).sql == "COUNT(id)"
    assert aggregation_sql(["avg", ["field", "price", None]]).sql == "AVG(price)"
    distinct = aggregation_sql(["distinct", ["field", "user_id", None]])
    assert distinct.sql == "COUNT(DISTINCT user_id)"
    assert distinct.agg == "count_distinct"


def test_ratio_preserves_both_aggregations():
    result = aggregation_sql(["/", ["sum", ["field", "revenue", None]], ["sum", ["field", "orders", None]]])
    assert result.sql == "(SUM(revenue) / SUM(orders))"
    assert result.agg is None
    assert result.metric_type is None


def test_field_id_requires_a_resolver_and_uses_real_names():
    with pytest.raises(MBQLTranslationError, match="unresolved Metabase field id 123"):
        aggregation_sql(["sum", ["field", 123, None]])

    resolved = aggregation_sql(["sum", ["field", 10, None]], resolve_field={10: "amount"})
    assert resolved.sql == "SUM(amount)"
    assert resolved.agg == "sum"
    assert resolved.inner_sql == "amount"


def test_field_names_expressions_and_mbql5_option_maps():
    assert aggregation_sql(["sum", ["field", "column_name", None]]).inner_sql == "column_name"
    assert aggregation_sql(["sum", "direct_field"]).inner_sql == "direct_field"
    assert aggregation_sql(["sum", ["expression", "custom_calc", None]]).inner_sql == "custom_calc"

    mbql5 = ["sum", {"lib/uuid": "agg"}, ["field", {"lib/uuid": "field", "base-type": "type/Float"}, 10]]
    assert aggregation_sql(mbql5, resolve_field={10: "amount"}).sql == "SUM(amount)"


def test_cumulative_and_share_are_not_flattened():
    cumulative = aggregation_sql(["cum-sum", ["field", "amount", None]])
    assert cumulative.metric_type == "cumulative"
    assert cumulative.agg == "sum"
    assert cumulative.inner_sql == "amount"
    assert cumulative.sql is None

    running_count = aggregation_sql(["cum-count"])
    assert running_count.metric_type == "cumulative"
    assert running_count.agg == "count"
    assert running_count.sql is None

    share = aggregation_sql(["share", ["sum", ["field", "revenue", None]]])
    assert share.skipped
    assert "share" in share.skipped
    assert share.sql is None


def test_filters_and_escaping():
    assert filter_sql(["=", ["field", "status", None], "active"]) == "status = 'active'"
    assert filter_sql([">", ["field", "amount", None], 100]) == "amount > 100"
    assert filter_sql(["is-null", ["field", "deleted_at", None]]) == "deleted_at IS NULL"
    assert filter_sql(["not-null", ["field", "email", None]]) == "email IS NOT NULL"
    assert (
        filter_sql(
            [
                "and",
                ["=", ["field", "status", None], "active"],
                [">", ["field", "amount", None], 0],
            ]
        )
        == "status = 'active' AND amount > 0"
    )
    assert (
        filter_sql(
            [
                "or",
                ["=", ["field", "status", None], "active"],
                ["=", ["field", "status", None], "pending"],
            ]
        )
        == "(status = 'active' OR status = 'pending')"
    )
    assert filter_sql(["contains", ["field", "name", None], "test"]) == "name LIKE '%test%' ESCAPE '\\'"
    assert filter_sql(["between", ["field", "amount", None], 10, 100]) == "amount BETWEEN 10 AND 100"

    assert filter_sql(["=", ["field", "name", None], "O'Brien"]) == "name = 'O''Brien'"
    assert filter_sql(["contains", ["field", "name", None], "100%_a"]) == "name LIKE '%100\\%\\_a%' ESCAPE '\\'"
    assert filter_sql(["between", ["field", "name", None], "a'b", "c"]) == "name BETWEEN 'a''b' AND 'c'"
    assert filter_sql(["=", ["field", "flag", None], True]) == "flag = TRUE"


def test_mbql5_dataset_query_resolves_ids():
    dataset_query = {
        "lib/type": "mbql/query",
        "stages": [
            {
                "lib/type": "mbql.stage/mbql",
                "source-table": 5,
                "aggregation": [["sum", {"lib/uuid": "a"}, ["field", {"lib/uuid": "b"}, 10]]],
                "filters": [["=", {"lib/uuid": "c"}, ["field", {"base-type": "type/Text"}, 11], "paid"]],
            }
        ],
    }
    translated = translate_dataset_query(dataset_query, resolve_field={10: "amount", 11: "status"})
    assert translated.source_table == 5
    assert translated.aggregation is not None
    assert translated.aggregation.sql == "SUM(amount)"
    assert translated.filters == ("status = 'paid'",)
    assert translated.skip_reason is None


def test_native_select_is_skipped_and_expression_is_complete():
    select_query = translate_dataset_query({"type": "native", "native": {"query": "SELECT sum(amount) FROM orders"}})
    assert select_query.skip_reason
    assert "SELECT" in select_query.skip_reason
    assert is_complete_query("WITH t AS (SELECT 1) SELECT * FROM t")

    expression = translate_dataset_query({"type": "native", "native": {"query": " SUM(amount) / COUNT(*) "}})
    assert expression.skip_reason is None
    assert expression.aggregation is not None
    assert expression.aggregation.sql == "SUM(amount) / COUNT(*)"
    assert expression.aggregation.sql_is_complete is True
