"""REST import tests for the Metabase adapter."""

import json
import logging
import urllib.error
from unittest.mock import patch
from urllib.request import Request

import pytest

from sidemantic.adapters.metabase import MetabaseAdapter, MetabaseAPIError
from sidemantic.fidelity import capture_import_report
from sidemantic.formats import get_semantic_format

ORDERS_METADATA = {
    "id": 5,
    "name": "orders",
    "schema": "public",
    "db_id": 1,
    "fields": [
        {
            "id": 9,
            "name": "order_id",
            "semantic_type": "type/PK",
            "base_type": "type/Integer",
            "position": 0,
        },
        {
            "id": 10,
            "name": "amount",
            "semantic_type": "type/Currency",
            "base_type": "type/Float",
            "position": 1,
        },
        {
            "id": 11,
            "name": "status",
            "semantic_type": "type/Category",
            "base_type": "type/Text",
            "position": 2,
        },
        {
            "id": 12,
            "name": "created_at",
            "semantic_type": "type/CreationTimestamp",
            "base_type": "type/DateTime",
            "position": 3,
        },
    ],
}

ORDERS_WITHOUT_PK = {
    "id": 5,
    "name": "orders",
    "schema": "public",
    "db_id": 1,
    "fields": [
        {"id": 1, "name": "id", "semantic_type": "type/Category", "base_type": "type/Integer", "position": 0},
        {"id": 10, "name": "amount", "semantic_type": "type/Currency", "base_type": "type/Float", "position": 1},
    ],
}


class _Body:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _revenue_card(**overrides):
    card = {
        "id": 1,
        "name": "Total Revenue",
        "description": "Sum of all revenue",
        "type": "metric",
        "dataset_query": {
            "type": "query",
            "database": 1,
            "query": {"source-table": 5, "aggregation": [["sum", ["field", 10, None]]]},
        },
    }
    card.update(overrides)
    return card


@pytest.fixture
def adapter():
    return MetabaseAdapter(api_key="test_key", page_size=1)


def test_format_registry_exposes_import_only_adapter():
    spec = get_semantic_format("metabase")
    assert spec.adapter_class == "MetabaseAdapter"
    assert spec.supports_export is False
    assert isinstance(spec.create_adapter(api_key="test_key"), MetabaseAdapter)


def test_parse_requires_http_url(adapter):
    with pytest.raises(ValueError, match="Invalid Metabase URL"):
        adapter.parse("/path/to/file")


def test_parse_accepts_http_and_strips_slash(adapter):
    with patch.object(adapter, "_fetch_metric_cards", return_value=[]) as fetch:
        assert adapter.parse("http://localhost:3000").models == {}
        adapter.parse("https://metabase.example.com/")
    assert fetch.call_count == 2
    assert fetch.call_args_list[1].args[0] == "https://metabase.example.com"


def test_auth_and_first_page_errors_surface(adapter):
    for status, match in ((401, "Authentication failed"), (403, "Access denied"), (500, "HTTP 500")):
        error = urllib.error.HTTPError("https://example.com/api/card", status, "nope", {}, None)
        with patch("urllib.request.urlopen", side_effect=error), pytest.raises(MetabaseAPIError, match=match):
            adapter.parse("https://example.com")


def test_connection_error_surfaces(adapter):
    with (
        patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")),
        pytest.raises(MetabaseAPIError, match="Failed to connect"),
    ):
        adapter.parse("https://example.com")


def test_card_list_is_not_paged_with_offset(adapter):
    cards = [_revenue_card(id=index, name=f"Revenue {index}") for index in range(3)]
    seen: list[str] = []

    def urlopen(request: Request, timeout=0):
        seen.append(request.full_url)
        assert request.get_header("X-api-key") == "test_key"
        if request.full_url.endswith("/api/card"):
            return _Body(cards)
        if "/api/table/5/query_metadata" in request.full_url:
            return _Body(ORDERS_METADATA)
        raise AssertionError(request.full_url)

    with patch("urllib.request.urlopen", side_effect=urlopen):
        graph = adapter.parse("https://example.com")

    assert seen == [
        "https://example.com/api/card",
        "https://example.com/api/table/5/query_metadata?include_sensitive_fields=true&include_hidden_fields=true",
    ]
    assert "offset" not in seen[0]
    assert len(graph.models) == 1
    assert len([metric for metric in graph.models["orders"].metrics if metric.public]) == 3


def test_envelope_pagination_follows_total_without_duplicates(adapter):
    first = _revenue_card(id=1, name="Revenue A")
    second = _revenue_card(id=2, name="Revenue B")
    pages = {
        "https://example.com/api/card": {"data": [first], "total": 2, "limit": 1, "offset": 0},
        "https://example.com/api/card?offset=1&limit=1": {"data": [first, second], "total": 2, "limit": 1, "offset": 1},
    }

    def urlopen(request: Request, timeout=0):
        if request.full_url in pages:
            return _Body(pages[request.full_url])
        if "/api/table/5/query_metadata" in request.full_url:
            return _Body(ORDERS_METADATA)
        raise AssertionError(request.full_url)

    with patch("urllib.request.urlopen", side_effect=urlopen):
        graph = adapter.parse("https://example.com")

    names = sorted(metric.name for metric in graph.models["orders"].metrics)
    assert names == ["revenue_a", "revenue_b"]


def test_repeated_page_is_an_error(adapter):
    card = _revenue_card()
    payload = {"data": [card], "total": 3, "limit": 1, "offset": 0}

    def urlopen(request: Request, timeout=0):
        return _Body(payload)

    with patch("urllib.request.urlopen", side_effect=urlopen), pytest.raises(MetabaseAPIError, match="repeated a page"):
        adapter.parse("https://example.com")


def test_integer_table_and_field_ids_resolve_real_names(adapter):
    card = _revenue_card()
    card["dataset_query"]["query"]["filter"] = ["=", ["field", 11, None], "O'Brien"]

    def urlopen(request: Request, timeout=0):
        if request.full_url.endswith("/api/card"):
            return _Body([card])
        if "/query_metadata" in request.full_url:
            return _Body(ORDERS_METADATA)
        raise AssertionError(request.full_url)

    with patch("urllib.request.urlopen", side_effect=urlopen):
        graph = adapter.parse("https://example.com")

    model = graph.models["orders"]
    assert model.table == "public.orders"
    assert model.primary_key == "order_id"
    assert model.default_time_dimension == "created_at"
    metric = model.get_metric("total_revenue")
    assert metric is not None
    assert metric.agg == "sum"
    assert metric.sql == "amount"
    assert metric.filters == ["status = 'O''Brien'"]
    assert "field_10" not in (metric.sql or "")
    assert adapter.validate(graph) == []


def test_id_column_is_not_invented_as_primary_key(adapter):
    def urlopen(request: Request, timeout=0):
        if request.full_url.endswith("/api/card"):
            return _Body([_revenue_card()])
        if "/query_metadata" in request.full_url:
            return _Body(ORDERS_WITHOUT_PK)
        raise AssertionError(request.full_url)

    with patch("urllib.request.urlopen", side_effect=urlopen):
        graph = adapter.parse("https://example.com")

    model = graph.models["orders"]
    assert model.table == "public.orders"
    assert model.primary_key is None
    errors = adapter.validate(graph)
    assert errors == ["Model orders has no primary key"]


def test_table_auth_error_surfaces(adapter):
    def urlopen(request: Request, timeout=0):
        if request.full_url.endswith("/api/card"):
            return _Body([_revenue_card()])
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    with patch("urllib.request.urlopen", side_effect=urlopen), pytest.raises(MetabaseAPIError, match="Authentication"):
        adapter.parse("https://example.com")


def test_missing_field_falls_back_to_field_endpoint(adapter):
    def urlopen(request: Request, timeout=0):
        if request.full_url.endswith("/api/card"):
            return _Body([_revenue_card()])
        if "/query_metadata" in request.full_url:
            return _Body({**ORDERS_METADATA, "fields": [ORDERS_METADATA["fields"][0]]})
        if request.full_url.endswith("/api/field/10"):
            return _Body({"id": 10, "name": "amount"})
        raise AssertionError(request.full_url)

    with patch("urllib.request.urlopen", side_effect=urlopen):
        graph = adapter.parse("https://example.com")

    assert graph.models["orders"].get_metric("total_revenue").sql == "amount"


def test_questions_are_ignored_and_metrics_group_by_table(adapter):
    users = {
        "id": 6,
        "name": "users",
        "schema": None,
        "fields": [
            {"id": 20, "name": "user_id", "semantic_type": "type/PK", "base_type": "type/Integer", "position": 0}
        ],
    }
    cards = [
        _revenue_card(),
        {
            "id": 2,
            "name": "Order Count",
            "type": "metric",
            "dataset_query": {"type": "query", "query": {"source-table": 5, "aggregation": [["count"]]}},
        },
        {
            "id": 3,
            "name": "User Count",
            "type": "metric",
            "dataset_query": {"type": "query", "query": {"source-table": 6, "aggregation": [["count"]]}},
        },
        {
            "id": 4,
            "name": "Not a metric",
            "type": "question",
            "dataset_query": {"type": "query", "query": {"source-table": 6, "aggregation": [["count"]]}},
        },
    ]

    def urlopen(request: Request, timeout=0):
        if request.full_url.endswith("/api/card"):
            return _Body(cards)
        if "/api/table/5/" in request.full_url:
            return _Body(ORDERS_METADATA)
        if "/api/table/6/" in request.full_url:
            return _Body(users)
        raise AssertionError(request.full_url)

    with patch("urllib.request.urlopen", side_effect=urlopen):
        graph = adapter.parse("https://example.com")

    assert set(graph.models) == {"orders", "users"}
    assert len(graph.models["orders"].metrics) == 2
    assert graph.models["users"].table == "users"
    assert graph.models["users"].primary_key == "user_id"


def test_cumulative_share_and_native_queries(adapter, caplog):
    cards = [
        {
            "id": 1,
            "name": "Cumulative Revenue",
            "type": "metric",
            "dataset_query": {
                "type": "query",
                "query": {"source-table": 5, "aggregation": [["cum-sum", ["field", 10, None]]]},
            },
        },
        {
            "id": 2,
            "name": "Revenue Share",
            "type": "metric",
            "dataset_query": {
                "type": "query",
                "query": {"source-table": 5, "aggregation": [["share", ["sum", ["field", 10, None]]]]},
            },
        },
        {
            "id": 3,
            "name": "Native Select",
            "type": "metric",
            "dataset_query": {"type": "native", "native": {"query": "SELECT sum(amount) FROM orders"}},
        },
        {
            "id": 4,
            "name": "Native Ratio",
            "type": "metric",
            "table_id": 5,
            "dataset_query": {"type": "native", "native": {"query": "SUM(amount) / COUNT(*)"}},
        },
    ]

    def urlopen(request: Request, timeout=0):
        if request.full_url.endswith("/api/card"):
            return _Body(cards)
        if "/query_metadata" in request.full_url:
            return _Body(ORDERS_METADATA)
        raise AssertionError(request.full_url)

    with capture_import_report() as report, caplog.at_level(logging.WARNING):
        with patch("urllib.request.urlopen", side_effect=urlopen):
            graph = adapter.parse("https://example.com")

    model = graph.models["orders"]
    cumulative = model.get_metric("cumulative_revenue")
    base = model.get_metric("cumulative_revenue__base")
    assert cumulative.type == "cumulative"
    assert cumulative.sql == "cumulative_revenue__base"
    assert base.agg == "sum"
    assert base.sql == "amount"
    assert base.visibility == "private"
    assert base.public is False
    assert model.get_metric("revenue_share") is None
    assert model.get_metric("native_select") is None
    native = model.get_metric("native_ratio")
    assert native.sql == "SUM(amount) / COUNT(*)"
    assert native.sql_is_complete is True
    assert "SUM(SUM(" not in (native.sql or "")
    notes = " ".join(note.detail for note in report.notes)
    assert "share aggregation" in notes
    assert "complete SELECT" in notes
    assert "share aggregation" in caplog.text


def test_saved_question_source_unwraps_to_its_table(adapter):
    cards = [
        {
            "id": 8,
            "name": "Orders model",
            "type": "model",
            "dataset_query": {"type": "query", "query": {"source-table": 5}},
        }
    ]
    metric = {
        "id": 9,
        "name": "Model Revenue",
        "type": "metric",
        "dataset_query": {
            "type": "query",
            "query": {"source-table": "card__8", "aggregation": [["sum", ["field", 10, None]]]},
        },
    }

    def urlopen(request: Request, timeout=0):
        if request.full_url.endswith("/api/card"):
            return _Body([metric])
        if request.full_url.endswith("/api/card/8"):
            return _Body(cards[0])
        if "/api/table/5/" in request.full_url:
            return _Body(ORDERS_METADATA)
        raise AssertionError(request.full_url)

    with patch("urllib.request.urlopen", side_effect=urlopen):
        graph = adapter.parse("https://example.com")

    assert graph.models["orders"].table == "public.orders"
    assert graph.models["orders"].get_metric("model_revenue").sql == "amount"


def test_string_source_table_does_not_invent_a_primary_key(adapter):
    card = {
        "id": 1,
        "name": "Named Revenue",
        "type": "metric",
        "dataset_query": {
            "type": "query",
            "query": {
                "source-table": "orders",
                "aggregation": [["sum", ["field", "amount", None]]],
                "filter": ["contains", ["field", "status", None], "100%"],
            },
        },
    }
    with patch.object(adapter, "_fetch_metric_cards", return_value=[card]):
        graph = adapter.parse("https://example.com")

    model = graph.models["orders"]
    assert model.table == "orders"
    assert model.primary_key is None
    assert model.metrics[0].filters == ["status LIKE '%100\\%%' ESCAPE '\\'"]
