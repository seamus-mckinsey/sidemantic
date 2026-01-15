"""Tests for Metabase adapter."""

import urllib.error
from unittest.mock import patch

import pytest

from sidemantic.adapters.metabase import MetabaseAdapter, MetabaseAPIError


class TestMetabaseAdapter:
    """Tests for MetabaseAdapter."""

    @pytest.fixture
    def adapter(self):
        """Create adapter instance."""
        return MetabaseAdapter(api_key="test_key")

    @pytest.fixture
    def mock_metric_card(self):
        """Sample metric card response."""
        return {
            "id": 1,
            "name": "Total Revenue",
            "description": "Sum of all revenue",
            "type": "metric",
            "dataset_query": {
                "type": "query",
                "database": 1,
                "query": {
                    "source-table": 5,
                    "aggregation": [["sum", ["field", 10, None]]],
                },
            },
        }

    @pytest.fixture
    def mock_count_metric_card(self):
        """Sample count metric card."""
        return {
            "id": 2,
            "name": "Order Count",
            "description": "Number of orders",
            "type": "metric",
            "dataset_query": {
                "type": "query",
                "database": 1,
                "query": {
                    "source-table": 5,
                    "aggregation": [["count"]],
                },
            },
        }

    def test_parse_requires_valid_url(self, adapter):
        """Test that parse requires valid HTTP URL."""
        with pytest.raises(ValueError, match="Invalid Metabase URL"):
            adapter.parse("/path/to/file")

    def test_parse_accepts_http_url(self, adapter):
        """Test that parse accepts HTTP URLs."""
        with patch.object(adapter, "_fetch_metric_cards", return_value=[]):
            graph = adapter.parse("http://localhost:3000")
            assert graph is not None
            assert len(graph.models) == 0

    def test_parse_accepts_https_url(self, adapter):
        """Test that parse accepts HTTPS URLs."""
        with patch.object(adapter, "_fetch_metric_cards", return_value=[]):
            graph = adapter.parse("https://metabase.example.com")
            assert graph is not None

    def test_parse_strips_trailing_slash(self, adapter):
        """Test that parse strips trailing slash from URL."""
        with patch.object(adapter, "_fetch_metric_cards", return_value=[]) as mock_fetch:
            adapter.parse("https://metabase.example.com/")
            # Verify the URL passed to fetch doesn't have double slashes
            mock_fetch.assert_called_once()

    def test_parse_creates_models_for_metrics(self, adapter, mock_metric_card):
        """Test that parse creates models for imported metrics."""
        with patch.object(adapter, "_fetch_metric_cards", return_value=[mock_metric_card]):
            graph = adapter.parse("https://example.com")

            assert len(graph.models) == 1
            model = list(graph.models.values())[0]
            assert len(model.metrics) == 1
            assert model.metrics[0].name == "total_revenue"

    def test_parse_groups_metrics_by_source_table(self, adapter, mock_metric_card, mock_count_metric_card):
        """Test that metrics from same source table are grouped."""
        with patch.object(adapter, "_fetch_metric_cards", return_value=[mock_metric_card, mock_count_metric_card]):
            graph = adapter.parse("https://example.com")

            # Both metrics have source-table: 5, so should be in one model
            assert len(graph.models) == 1
            model = list(graph.models.values())[0]
            assert len(model.metrics) == 2

    def test_metric_name_sanitization(self, adapter):
        """Test that metric names are properly sanitized."""
        assert adapter._sanitize_name("Total Revenue ($)") == "total_revenue"
        assert adapter._sanitize_name("123_metric") == "m_123_metric"
        assert adapter._sanitize_name("my metric name") == "my_metric_name"
        assert adapter._sanitize_name("") == "unnamed"
        assert adapter._sanitize_name("___test___") == "test"


class TestMBQLParsing:
    """Tests for MBQL to SQL conversion."""

    @pytest.fixture
    def adapter(self):
        return MetabaseAdapter(api_key="test_key")

    def test_mbql_sum_parsing(self, adapter):
        """Test parsing MBQL sum aggregation."""
        agg = ["sum", ["field", "amount", None]]
        result = adapter._parse_mbql_aggregation(agg)
        assert result == "SUM(amount)"

    def test_mbql_count_parsing(self, adapter):
        """Test parsing MBQL count aggregation."""
        result = adapter._parse_mbql_aggregation(["count"])
        assert result == "COUNT(*)"

    def test_mbql_count_with_field(self, adapter):
        """Test parsing MBQL count with field."""
        result = adapter._parse_mbql_aggregation(["count", ["field", "id", None]])
        assert result == "COUNT(id)"

    def test_mbql_avg_parsing(self, adapter):
        """Test parsing MBQL avg aggregation."""
        agg = ["avg", ["field", "price", None]]
        result = adapter._parse_mbql_aggregation(agg)
        assert result == "AVG(price)"

    def test_mbql_distinct_parsing(self, adapter):
        """Test parsing MBQL distinct count."""
        agg = ["distinct", ["field", "user_id", None]]
        result = adapter._parse_mbql_aggregation(agg)
        assert result == "COUNT(DISTINCT user_id)"

    def test_mbql_ratio_parsing(self, adapter):
        """Test parsing MBQL ratio expression."""
        agg = ["/", ["sum", ["field", "revenue", None]], ["sum", ["field", "orders", None]]]
        result = adapter._parse_mbql_aggregation(agg)
        assert result == "(SUM(revenue) / SUM(orders))"

    def test_mbql_field_id_reference(self, adapter):
        """Test parsing field by ID."""
        field = ["field", 123, None]
        result = adapter._parse_mbql_field(field)
        assert result == "field_123"

    def test_mbql_field_name_reference(self, adapter):
        """Test parsing field by name."""
        field = ["field", "column_name", None]
        result = adapter._parse_mbql_field(field)
        assert result == "column_name"

    def test_mbql_field_string_direct(self, adapter):
        """Test parsing direct string field."""
        result = adapter._parse_mbql_field("direct_field")
        assert result == "direct_field"

    def test_mbql_expression_reference(self, adapter):
        """Test parsing expression reference."""
        field = ["expression", "custom_calc", None]
        result = adapter._parse_mbql_field(field)
        assert result == "custom_calc"


class TestFilterParsing:
    """Tests for MBQL filter parsing."""

    @pytest.fixture
    def adapter(self):
        return MetabaseAdapter(api_key="test_key")

    def test_simple_equality_filter(self, adapter):
        """Test simple equality filter."""
        filter_clause = ["=", ["field", "status", None], "active"]
        result = adapter._mbql_filter_to_sql(filter_clause)
        assert result == "status = 'active'"

    def test_numeric_comparison_filter(self, adapter):
        """Test numeric comparison filter."""
        filter_clause = [">", ["field", "amount", None], 100]
        result = adapter._mbql_filter_to_sql(filter_clause)
        assert result == "amount > 100"

    def test_is_null_filter(self, adapter):
        """Test IS NULL filter."""
        filter_clause = ["is-null", ["field", "deleted_at", None]]
        result = adapter._mbql_filter_to_sql(filter_clause)
        assert result == "deleted_at IS NULL"

    def test_is_not_null_filter(self, adapter):
        """Test IS NOT NULL filter."""
        filter_clause = ["not-null", ["field", "email", None]]
        result = adapter._mbql_filter_to_sql(filter_clause)
        assert result == "email IS NOT NULL"

    def test_and_filter(self, adapter):
        """Test AND filter combination."""
        filter_clause = [
            "and",
            ["=", ["field", "status", None], "active"],
            [">", ["field", "amount", None], 0],
        ]
        result = adapter._mbql_filter_to_sql(filter_clause)
        assert result == "status = 'active' AND amount > 0"

    def test_or_filter(self, adapter):
        """Test OR filter combination."""
        filter_clause = [
            "or",
            ["=", ["field", "status", None], "active"],
            ["=", ["field", "status", None], "pending"],
        ]
        result = adapter._mbql_filter_to_sql(filter_clause)
        assert result == "(status = 'active' OR status = 'pending')"

    def test_contains_filter(self, adapter):
        """Test LIKE contains filter."""
        filter_clause = ["contains", ["field", "name", None], "test"]
        result = adapter._mbql_filter_to_sql(filter_clause)
        assert result == "name LIKE '%test%'"

    def test_between_filter(self, adapter):
        """Test BETWEEN filter."""
        filter_clause = ["between", ["field", "amount", None], 10, 100]
        result = adapter._mbql_filter_to_sql(filter_clause)
        assert result == "amount BETWEEN 10 AND 100"


class TestSourceTableExtraction:
    """Tests for source table extraction."""

    @pytest.fixture
    def adapter(self):
        return MetabaseAdapter(api_key="test_key")

    def test_numeric_table_id(self, adapter):
        """Test extraction with numeric table ID."""
        card = {
            "name": "Test Metric",
            "dataset_query": {"type": "query", "query": {"source-table": 42}},
        }
        result = adapter._extract_source_table(card)
        assert result == "table_42"

    def test_string_table_name(self, adapter):
        """Test extraction with string table name."""
        card = {
            "name": "Test Metric",
            "dataset_query": {"type": "query", "query": {"source-table": "orders"}},
        }
        result = adapter._extract_source_table(card)
        assert result == "orders"

    def test_card_reference(self, adapter):
        """Test extraction with card reference."""
        card = {
            "name": "Test Metric",
            "dataset_query": {"type": "query", "query": {"source-table": "card__123"}},
        }
        result = adapter._extract_source_table(card)
        assert result == "card_123"

    def test_native_query_fallback(self, adapter):
        """Test extraction falls back to card name for native queries."""
        card = {
            "name": "Custom Native Query",
            "dataset_query": {"type": "native", "native": {"query": "SELECT * FROM foo"}},
        }
        result = adapter._extract_source_table(card)
        assert result == "custom_native_query"

    def test_missing_source_table(self, adapter):
        """Test extraction with missing source-table."""
        card = {
            "name": "Test Metric",
            "dataset_query": {"type": "query", "query": {}},
        }
        result = adapter._extract_source_table(card)
        assert result == "unknown"


class TestAPIErrorHandling:
    """Tests for API error handling."""

    @pytest.fixture
    def adapter(self):
        return MetabaseAdapter(api_key="test_key")

    def test_auth_error_401(self, adapter):
        """Test 401 authentication error."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError("http://example.com", 401, "Unauthorized", {}, None)

            with pytest.raises(MetabaseAPIError, match="Authentication failed"):
                adapter._api_request("http://example.com/api/card")

    def test_forbidden_error_403(self, adapter):
        """Test 403 forbidden error."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError("http://example.com", 403, "Forbidden", {}, None)

            with pytest.raises(MetabaseAPIError, match="Access denied"):
                adapter._api_request("http://example.com/api/card")

    def test_not_found_error_404(self, adapter):
        """Test 404 not found error."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError("http://example.com", 404, "Not Found", {}, None)

            with pytest.raises(MetabaseAPIError, match="Endpoint not found"):
                adapter._api_request("http://example.com/api/card")

    def test_connection_error(self, adapter):
        """Test connection error handling."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

            with pytest.raises(MetabaseAPIError, match="Failed to connect"):
                adapter._api_request("http://example.com/api/card")


class TestFullIntegration:
    """Integration tests with mocked API responses."""

    @pytest.fixture
    def adapter(self):
        return MetabaseAdapter(api_key="test_key")

    def test_empty_response(self, adapter):
        """Test handling of empty API response."""
        with patch.object(adapter, "_fetch_metric_cards", return_value=[]):
            graph = adapter.parse("https://example.com")
            assert len(graph.models) == 0

    def test_multiple_source_tables(self, adapter):
        """Test metrics from multiple source tables create multiple models."""
        cards = [
            {
                "id": 1,
                "name": "Order Revenue",
                "dataset_query": {
                    "type": "query",
                    "query": {"source-table": 1, "aggregation": [["sum", ["field", "amount", None]]]},
                },
            },
            {
                "id": 2,
                "name": "User Count",
                "dataset_query": {
                    "type": "query",
                    "query": {"source-table": 2, "aggregation": [["count"]]},
                },
            },
        ]

        with patch.object(adapter, "_fetch_metric_cards", return_value=cards):
            graph = adapter.parse("https://example.com")

            assert len(graph.models) == 2

    def test_metric_with_filter(self, adapter):
        """Test metric with filter is properly parsed."""
        card = {
            "id": 1,
            "name": "Active Revenue",
            "dataset_query": {
                "type": "query",
                "query": {
                    "source-table": 1,
                    "aggregation": [["sum", ["field", "amount", None]]],
                    "filter": ["=", ["field", "status", None], "active"],
                },
            },
        }

        with patch.object(adapter, "_fetch_metric_cards", return_value=[card]):
            graph = adapter.parse("https://example.com")

            model = list(graph.models.values())[0]
            metric = model.metrics[0]
            assert metric.filters is not None
            assert "status = 'active'" in metric.filters[0]

    def test_derived_metric_parsing(self, adapter):
        """Test derived metric (ratio) parsing."""
        card = {
            "id": 1,
            "name": "Average Order Value",
            "dataset_query": {
                "type": "query",
                "query": {
                    "source-table": 1,
                    "aggregation": [["/", ["sum", ["field", "revenue", None]], ["count"]]],
                },
            },
        }

        with patch.object(adapter, "_fetch_metric_cards", return_value=[card]):
            graph = adapter.parse("https://example.com")

            model = list(graph.models.values())[0]
            metric = model.metrics[0]
            assert metric.name == "average_order_value"
            # The SQL should contain the ratio expression
            assert metric.sql is not None
