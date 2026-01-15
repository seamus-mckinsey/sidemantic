"""Metabase adapter for importing metrics from Metabase Enterprise Edition."""

import json
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from sidemantic.adapters.base import BaseAdapter
from sidemantic.core.metric import Metric
from sidemantic.core.model import Model
from sidemantic.core.semantic_graph import SemanticGraph


class MetabaseAPIError(Exception):
    """Error communicating with Metabase API."""

    pass


class MetabaseAdapter(BaseAdapter):
    """Adapter for importing metrics from Metabase Enterprise Edition.

    Metabase v51+ stores metrics as cards with type="metric". This adapter
    fetches metrics via the REST API and converts them to Sidemantic format.

    Note: This adapter is import-only (no export support).

    Example:
        >>> adapter = MetabaseAdapter(api_key="mb_xxx")
        >>> graph = adapter.parse("https://metabase.example.com")
        >>> for model in graph.models.values():
        ...     print(f"{model.name}: {[m.name for m in model.metrics]}")
    """

    def __init__(self, api_key: str, timeout: int = 30, page_size: int = 100):
        """Initialize the Metabase adapter.

        Args:
            api_key: Metabase API key (X-Api-Key header value)
            timeout: Request timeout in seconds
            page_size: Number of items to fetch per API request
        """
        self.api_key = api_key
        self.timeout = timeout
        self.page_size = page_size

    def parse(self, source: str | Path) -> SemanticGraph:
        """Fetch metrics from Metabase and convert to SemanticGraph.

        Args:
            source: Metabase instance URL (e.g., "https://metabase.example.com")

        Returns:
            SemanticGraph containing models with imported metrics

        Raises:
            MetabaseAPIError: If API communication fails
            ValueError: If source URL is invalid
        """
        base_url = str(source).rstrip("/")

        # Validate URL format
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid Metabase URL: {source}. Must start with http:// or https://")

        graph = SemanticGraph()

        # Fetch all metric cards
        metric_cards = self._fetch_metric_cards(base_url)

        if not metric_cards:
            return graph

        # Group metrics by source table
        metrics_by_source: dict[str, list[dict]] = {}
        for card in metric_cards:
            source_table = self._extract_source_table(card)
            if source_table not in metrics_by_source:
                metrics_by_source[source_table] = []
            metrics_by_source[source_table].append(card)

        # Create models with their metrics
        for source_table, cards in metrics_by_source.items():
            model = self._create_model(source_table, cards)
            if model:
                graph.add_model(model)

        return graph

    def _fetch_metric_cards(self, base_url: str) -> list[dict]:
        """Fetch all metric-type cards from Metabase.

        Args:
            base_url: Metabase instance URL

        Returns:
            List of metric card dictionaries
        """
        all_cards = []
        offset = 0

        while True:
            # Build URL with type filter
            # Note: Metabase card API may not support pagination consistently,
            # but we handle it just in case
            url = f"{base_url}/api/card?type=metric"
            if offset > 0:
                url += f"&offset={offset}&limit={self.page_size}"

            try:
                response = self._api_request(url)

                # Response can be a list or object with data key
                if isinstance(response, list):
                    cards = response
                else:
                    cards = response.get("data", [])

                if not cards:
                    break

                all_cards.extend(cards)

                # Check if we've fetched all cards
                if len(cards) < self.page_size:
                    break

                offset += self.page_size

            except MetabaseAPIError:
                # If pagination fails midway, return what we have
                break

        return all_cards

    def _api_request(self, url: str, method: str = "GET", data: dict | None = None) -> Any:
        """Make an authenticated API request to Metabase.

        Args:
            url: Full URL to request
            method: HTTP method
            data: Optional JSON body for POST requests

        Returns:
            Parsed JSON response

        Raises:
            MetabaseAPIError: If request fails
        """
        headers = {
            "X-Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

        body = json.dumps(data).encode("utf-8") if data else None

        request = urllib.request.Request(url, headers=headers, data=body, method=method)

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise MetabaseAPIError("Authentication failed. Check your API key.") from e
            elif e.code == 403:
                raise MetabaseAPIError("Access denied. Ensure API key has sufficient permissions.") from e
            elif e.code == 404:
                raise MetabaseAPIError(f"Endpoint not found: {url}") from e
            else:
                raise MetabaseAPIError(f"HTTP {e.code}: {e.reason}") from e
        except urllib.error.URLError as e:
            raise MetabaseAPIError(f"Failed to connect to Metabase: {e.reason}") from e
        except json.JSONDecodeError:
            raise MetabaseAPIError("Invalid JSON response from Metabase") from None

    def _extract_source_table(self, card: dict) -> str:
        """Extract the source table name from a metric card.

        Args:
            card: Metric card dictionary

        Returns:
            Source table/model name
        """
        query = card.get("dataset_query", {})

        # Native query - use card name as model
        if query.get("type") == "native":
            return self._sanitize_name(card.get("name", "native_query"))

        # MBQL query - extract source-table
        mbql_query = query.get("query", {})
        source_table = mbql_query.get("source-table")

        if source_table:
            # source-table can be an ID (int) or a string
            if isinstance(source_table, int):
                return f"table_{source_table}"
            elif isinstance(source_table, str):
                # Could be "card__123" for saved question references
                if source_table.startswith("card__"):
                    return f"card_{source_table[6:]}"
                return self._sanitize_name(source_table)
            elif isinstance(source_table, list):
                # Serialization format: ["database", "schema", "table"]
                return self._sanitize_name(source_table[-1]) if source_table else "unknown"

        return "unknown"

    def _create_model(self, source_table: str, cards: list[dict]) -> Model | None:
        """Create a Model from a group of metric cards.

        Args:
            source_table: Source table name
            cards: List of metric cards for this table

        Returns:
            Model with metrics, or None if no valid metrics
        """
        metrics = []

        for card in cards:
            metric = self._parse_metric_card(card)
            if metric:
                metrics.append(metric)

        if not metrics:
            return None

        # Clean up model name
        model_name = source_table.replace("table_", "").replace("card_", "")

        return Model(
            name=model_name,
            table=source_table if not source_table.startswith(("table_", "card_")) else None,
            description=f"Imported from Metabase ({len(metrics)} metrics)",
            primary_key="id",
            metrics=metrics,
        )

    def _parse_metric_card(self, card: dict) -> Metric | None:
        """Parse a Metabase metric card into a Sidemantic Metric.

        Args:
            card: Metric card dictionary

        Returns:
            Metric instance or None if parsing fails
        """
        name = card.get("name")
        if not name:
            return None

        description = card.get("description")

        # Parse the metric definition from dataset_query
        query = card.get("dataset_query", {})
        expression = self._mbql_to_sql(query)

        if not expression:
            return None

        # Parse any filters
        filters = self._extract_filters(query)

        # Create metric - the Metric class will parse aggregations from SQL
        return Metric(
            name=self._sanitize_name(name),
            sql=expression,
            description=description,
            filters=filters if filters else None,
            label=name,  # Keep original name as display label
        )

    def _mbql_to_sql(self, query: dict) -> str | None:
        """Convert Metabase query to SQL expression.

        Args:
            query: dataset_query from card

        Returns:
            SQL expression string or None
        """
        if query.get("type") == "native":
            # Native SQL query - extract the query text
            native = query.get("native", {})
            native_query = native.get("query")
            if native_query:
                # Try to extract aggregation from native query
                # This is a simplified approach - full parsing would need sqlglot
                return native_query

        # MBQL query - parse aggregation
        mbql = query.get("query", {})
        aggregation = mbql.get("aggregation", [])

        if not aggregation:
            return None

        # Parse first aggregation clause
        if aggregation and isinstance(aggregation[0], list):
            return self._parse_mbql_aggregation(aggregation[0])

        return None

    def _parse_mbql_aggregation(self, agg: Any) -> str | None:
        """Parse a single MBQL aggregation clause to SQL.

        Args:
            agg: MBQL aggregation clause (e.g., ["sum", ["field", 10, null]])

        Returns:
            SQL expression or None
        """
        if not agg or not isinstance(agg, list) or len(agg) == 0:
            return None

        op = agg[0]

        # Simple aggregations
        agg_map = {
            "count": "COUNT",
            "sum": "SUM",
            "avg": "AVG",
            "min": "MIN",
            "max": "MAX",
            "distinct": "COUNT_DISTINCT",
            "cum-count": "COUNT",  # Cumulative handled separately
            "cum-sum": "SUM",  # Cumulative handled separately
        }

        if op in agg_map:
            if op == "count" and len(agg) == 1:
                return "COUNT(*)"

            if len(agg) > 1:
                field_expr = self._parse_mbql_field(agg[1])
                if field_expr:
                    if op == "distinct":
                        return f"COUNT(DISTINCT {field_expr})"
                    return f"{agg_map[op]}({field_expr})"

            # Count without field
            if op == "count":
                return "COUNT(*)"

        # Arithmetic operators for derived metrics
        if op in ["+", "-", "*", "/"]:
            if len(agg) >= 3:
                left = self._parse_mbql_aggregation(agg[1])
                right = self._parse_mbql_aggregation(agg[2])
                if left and right:
                    return f"({left} {op} {right})"

        # Handle share/percentage
        if op == "share":
            # share computes percentage of total
            if len(agg) > 1:
                inner = self._parse_mbql_aggregation(agg[1])
                if inner:
                    return f"({inner} / SUM({inner}) OVER ())"

        return None

    def _parse_mbql_field(self, field: Any) -> str | None:
        """Parse an MBQL field reference to SQL column name.

        Args:
            field: MBQL field reference (e.g., ["field", 10, null] or ["field", "name", null])

        Returns:
            Column name or None
        """
        if not field:
            return None

        # String field name
        if isinstance(field, str):
            return field

        # Numeric literal
        if isinstance(field, (int, float)):
            return str(field)

        # MBQL field reference
        if not isinstance(field, list) or len(field) < 2:
            return None

        field_type = field[0]

        if field_type == "field":
            field_ref = field[1]
            # Field can be an ID (int) or name (string)
            if isinstance(field_ref, str):
                return field_ref
            elif isinstance(field_ref, int):
                return f"field_{field_ref}"

        # Expression reference
        if field_type == "expression":
            expr_name = field[1] if len(field) > 1 else None
            if isinstance(expr_name, str):
                return expr_name

        # Aggregation reference (for derived metrics)
        if field_type == "aggregation":
            agg_index = field[1] if len(field) > 1 else 0
            return f"agg_{agg_index}"

        return None

    def _extract_filters(self, query: dict) -> list[str] | None:
        """Extract filter clauses from MBQL query.

        Args:
            query: dataset_query from card

        Returns:
            List of SQL filter expressions or None
        """
        mbql = query.get("query", {})
        filter_clause = mbql.get("filter")

        if not filter_clause:
            return None

        # Convert MBQL filter to SQL
        sql_filter = self._mbql_filter_to_sql(filter_clause)
        return [sql_filter] if sql_filter else None

    def _mbql_filter_to_sql(self, filter_clause: Any) -> str | None:
        """Convert MBQL filter clause to SQL WHERE expression.

        Args:
            filter_clause: MBQL filter clause

        Returns:
            SQL filter expression or None
        """
        if not filter_clause or not isinstance(filter_clause, list) or len(filter_clause) < 2:
            return None

        op = filter_clause[0]

        # Basic comparison operators
        op_map = {
            "=": "=",
            "!=": "!=",
            "<": "<",
            ">": ">",
            "<=": "<=",
            ">=": ">=",
            "is-null": "IS NULL",
            "not-null": "IS NOT NULL",
            "contains": "LIKE",
            "starts-with": "LIKE",
            "ends-with": "LIKE",
        }

        if op in op_map:
            field = self._parse_mbql_field(filter_clause[1])
            if not field:
                return None

            if op in ["is-null", "not-null"]:
                return f"{field} {op_map[op]}"

            if len(filter_clause) >= 3:
                value = filter_clause[2]

                # Handle LIKE patterns
                if op == "contains":
                    return f"{field} LIKE '%{value}%'"
                elif op == "starts-with":
                    return f"{field} LIKE '{value}%'"
                elif op == "ends-with":
                    return f"{field} LIKE '%{value}'"

                # Regular comparison
                if isinstance(value, str):
                    return f"{field} {op_map[op]} '{value}'"
                return f"{field} {op_map[op]} {value}"

        # Logical operators
        if op == "and":
            parts = [self._mbql_filter_to_sql(f) for f in filter_clause[1:]]
            parts = [p for p in parts if p]
            return " AND ".join(parts) if parts else None

        if op == "or":
            parts = [self._mbql_filter_to_sql(f) for f in filter_clause[1:]]
            parts = [p for p in parts if p]
            return f"({' OR '.join(parts)})" if parts else None

        if op == "not":
            if len(filter_clause) > 1:
                inner = self._mbql_filter_to_sql(filter_clause[1])
                if inner:
                    return f"NOT ({inner})"

        # Between
        if op == "between":
            if len(filter_clause) >= 4:
                field = self._parse_mbql_field(filter_clause[1])
                low = filter_clause[2]
                high = filter_clause[3]
                if field:
                    return f"{field} BETWEEN {low} AND {high}"

        return None

    def _sanitize_name(self, name: str) -> str:
        """Sanitize metric/model name to be a valid identifier.

        Args:
            name: Original name

        Returns:
            Sanitized name suitable for use as identifier
        """
        # Replace spaces and special chars with underscores
        sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
        # Remove consecutive underscores
        sanitized = re.sub(r"_+", "_", sanitized)
        # Remove leading/trailing underscores
        sanitized = sanitized.strip("_")
        # Ensure it starts with a letter
        if sanitized and not sanitized[0].isalpha():
            sanitized = f"m_{sanitized}"
        return sanitized.lower() or "unnamed"
