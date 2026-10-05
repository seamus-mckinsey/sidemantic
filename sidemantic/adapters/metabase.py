"""Metabase adapter for importing metrics from a Metabase instance."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sidemantic.adapters.base import BaseAdapter
from sidemantic.adapters.metabase_mbql import (
    TranslatedQuery,
    is_complete_query,
    native_query_sql,
    quote_ident,
    sanitize_identifier,
    source_table_of,
    translate_dataset_query,
)
from sidemantic.core.dimension import Dimension
from sidemantic.core.metric import Metric
from sidemantic.core.model import Model
from sidemantic.core.semantic_graph import SemanticGraph
from sidemantic.fidelity import record_import_note

logger = logging.getLogger(__name__)


class MetabaseAPIError(Exception):
    """Raised when a Metabase API request fails."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class _Source:
    """Physical table or saved-question SQL that metrics are grouped onto."""

    key: tuple
    model_name: str
    table: str | None = None
    sql: str | None = None
    primary_key: str | list[str] | None = None
    dimensions: list[Dimension] | None = None
    default_time_dimension: str | None = None
    metadata: dict[str, Any] | None = None


class MetabaseAdapter(BaseAdapter):
    """Import Metabase metrics (cards with ``type='metric'``) over the REST API.

    The adapter is import-only. Pass the instance URL to :meth:`parse` and the
    API key at construction; the key is sent as ``X-Api-Key``. Directory
    auto-discovery does not call this adapter, because a Metabase source is a
    URL rather than a file. MBQL translation itself lives in
    :mod:`sidemantic.adapters.metabase_mbql`.

    Example:
        >>> adapter = MetabaseAdapter(api_key="mb_xxx")
        >>> graph = adapter.parse("https://metabase.example.com")
    """

    def __init__(self, api_key: str, timeout: int = 30, page_size: int = 100):
        """Initialize the Metabase adapter.

        Args:
            api_key: Metabase API key sent in the ``X-Api-Key`` header.
            timeout: Request timeout in seconds.
            page_size: Page size used only when ``/api/card`` itself returns a
                ``data``/``total`` envelope. The first request never sends
                ``limit`` or ``offset``.
        """
        self.api_key = api_key
        self.timeout = timeout
        self.page_size = page_size
        self._reset_caches()

    def parse(self, source: str | Path) -> SemanticGraph:
        """Fetch metric cards and convert them to a semantic graph.

        Args:
            source: Metabase instance URL, for example ``https://metabase.example.com``.

        Returns:
            Semantic graph of one model per resolved source table.

        Raises:
            MetabaseAPIError: If authentication, authorization, or the card list fails.
            ValueError: If ``source`` is not an HTTP URL.
        """
        base_url = str(source).rstrip("/")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError(f"Invalid Metabase URL: {source}. Must start with http:// or https://")

        self._reset_caches()
        graph = SemanticGraph()
        grouped: dict[tuple, list[tuple[dict, TranslatedQuery, _Source]]] = {}

        for card in self._fetch_metric_cards(base_url):
            if not isinstance(card, dict):
                continue
            prepared = self._prepare_card(base_url, card)
            if prepared is None:
                continue
            translated, resolved = prepared
            grouped.setdefault(resolved.key, []).append((card, translated, resolved))

        used_names: set[str] = set()
        for items in grouped.values():
            resolved = items[0][2]
            model_name = self._claim_name(resolved.model_name, used_names, resolved.key)
            metrics: list[Metric] = []
            metric_names: set[str] = set()
            for card, translated, _resolved in items:
                metrics.extend(self._metrics_for_card(card, translated, metric_names))
            if not metrics:
                continue
            public_count = sum(1 for metric in metrics if metric.public)
            graph.add_model(
                Model(
                    name=model_name,
                    table=resolved.table,
                    sql=resolved.sql,
                    description=f"Imported from Metabase ({public_count} metrics)",
                    primary_key=resolved.primary_key,
                    dimensions=list(resolved.dimensions or []),
                    metrics=metrics,
                    default_time_dimension=resolved.default_time_dimension,
                    metadata=resolved.metadata,
                )
            )

        return graph

    def _reset_caches(self) -> None:
        self._sources: dict[tuple, _Source] = {}
        self._field_names: dict[int, str] = {}
        self._cards: dict[int, dict] = {}
        self._missing_tables: set[int] = set()
        self._missing_cards: set[int] = set()

    def _fetch_metric_cards(self, base_url: str) -> list[dict]:
        """Return metric cards visible to the API key.

        ``GET /api/card`` documents ``f`` and ``model_id`` only and returns the
        full list in one response. ``limit``, ``offset``, and ``type`` are not
        part of that contract; sending them repeats the same payload. Metric
        cards are selected afterwards by ``type == 'metric'``.

        When a response is an explicit page envelope (``data`` plus integer
        ``total``) and the first page is short of ``total``, later pages are
        requested with the advertised offset. A repeated page raises instead of
        returning a partial list.
        """
        url = self._url(base_url, "/api/card")
        response = self._api_request(url)
        cards, total = self._unwrap_cards(response)
        collected: list[dict] = []
        seen: set[int] = set()

        def add(batch: list) -> int:
            added = 0
            for card in batch:
                if not isinstance(card, dict):
                    continue
                card_id = card.get("id")
                if isinstance(card_id, int):
                    if card_id in seen:
                        continue
                    seen.add(card_id)
                collected.append(card)
                added += 1
            return added

        if total is not None and total > 0 and not cards:
            raise MetabaseAPIError("Metabase reported cards but the first page was empty")

        add(cards)
        if total is None or len(cards) >= total:
            return [card for card in collected if self._is_metric_card(card)]

        limit = self.page_size
        if isinstance(response, dict) and isinstance(response.get("limit"), int) and response["limit"] > 0:
            limit = response["limit"]
        offset = len(cards)
        while offset < total:
            page_url = self._url(base_url, "/api/card", {"offset": offset, "limit": limit})
            page = self._api_request(page_url)
            page_cards, _page_total = self._unwrap_cards(page)
            if not page_cards:
                raise MetabaseAPIError(f"Metabase reported {total} cards but the page at offset {offset} was empty")
            if add(page_cards) == 0:
                raise MetabaseAPIError(
                    "GET /api/card repeated a page when asked for offset/limit; refusing to return a partial metric list"
                )
            offset += len(page_cards)

        return [card for card in collected if self._is_metric_card(card)]

    def _prepare_card(self, base_url: str, card: dict) -> tuple[TranslatedQuery, _Source] | None:
        name = card.get("name")
        label = name if isinstance(name, str) and name else f"card {card.get('id')}"
        dataset_query = card.get("dataset_query")
        if not isinstance(dataset_query, dict):
            self._drop("metabase_query", f"{label} has no dataset_query")
            return None

        self._preload_source(base_url, dataset_query)
        table_id = card.get("table_id")
        if isinstance(table_id, int):
            self._ensure_table(base_url, table_id)
        translated = translate_dataset_query(
            dataset_query,
            resolve_field=lambda field_id: self._resolve_field_name(base_url, field_id),
        )
        if translated.skip_reason:
            detail = f"{label}: {translated.skip_reason}"
            native_statement = bool(translated.native_sql and is_complete_query(translated.native_sql))
            self._drop("metabase_native_query" if native_statement else "metabase_aggregation", detail)
            return None
        if translated.extra_aggregations:
            self._note(
                "metabase_aggregation",
                f"{label} defines {translated.extra_aggregations + 1} aggregations; imported only the first",
                severity="approximated",
            )

        source = translated.source_table
        if source is None and isinstance(table_id, int):
            source = table_id
        if isinstance(source, int) and source in self._missing_tables:
            self._drop("metabase_table", f"{label} source table {source} was not found")
            return None
        resolved = self._resolve_source(base_url, source)
        if resolved is None:
            self._drop("metabase_table", f"{label} has no resolvable source table")
            return None
        return translated, resolved

    def _preload_source(self, base_url: str, dataset_query: dict) -> None:
        source = source_table_of(dataset_query)
        if isinstance(source, int):
            self._ensure_table(base_url, source)
        elif isinstance(source, str) and source.startswith("card__"):
            card_id = _card_id(source)
            if card_id is not None:
                self._ensure_card(base_url, card_id, set())

    def _resolve_source(self, base_url: str, source: Any) -> _Source | None:
        if isinstance(source, int):
            return self._ensure_table(base_url, source)
        if isinstance(source, str) and source.startswith("card__"):
            card_id = _card_id(source)
            if card_id is None:
                return None
            return self._source_from_card(base_url, card_id, set())
        if isinstance(source, str) and source:
            return self._named_table(source)
        if isinstance(source, list):
            names = [part for part in source if isinstance(part, str) and part]
            if len(names) >= 2:
                return self._named_table(names[-1], schema=names[-2])
            if names:
                return self._named_table(names[-1])
        return None

    def _named_table(self, name: str, schema: str | None = None) -> _Source:
        key = ("table-name", schema or "", name)
        existing = self._sources.get(key)
        if existing:
            return existing
        source = _Source(
            key=key,
            model_name=sanitize_identifier(name),
            table=_qualified_table(schema, name),
        )
        self._sources[key] = source
        return source

    def _ensure_table(self, base_url: str, table_id: int) -> _Source | None:
        key = ("table", table_id)
        existing = self._sources.get(key)
        if existing:
            return existing
        if table_id in self._missing_tables:
            return None

        payload = self._table_payload(base_url, table_id)
        if payload is None:
            self._missing_tables.add(table_id)
            return None

        fields = payload.get("fields") if isinstance(payload.get("fields"), list) else []
        for field in fields:
            if isinstance(field, dict) and isinstance(field.get("id"), int) and isinstance(field.get("name"), str):
                self._field_names.setdefault(field["id"], field["name"])

        physical = payload.get("name")
        if not isinstance(physical, str) or not physical:
            self._missing_tables.add(table_id)
            return None
        schema = payload.get("schema") if isinstance(payload.get("schema"), str) and payload.get("schema") else None
        dimensions, primary_key = _dimensions_from_fields(fields)
        time_dims = [dim.name for dim in dimensions if dim.type == "time"]
        database_id = payload.get("db_id") if isinstance(payload.get("db_id"), int) else None
        metadata: dict[str, Any] = {"metabase": {"table_id": table_id}}
        if database_id is not None:
            metadata["metabase"]["database_id"] = database_id
        source = _Source(
            key=key,
            model_name=sanitize_identifier(physical),
            table=_qualified_table(schema, physical),
            primary_key=primary_key,
            dimensions=dimensions,
            default_time_dimension=time_dims[0] if time_dims else None,
            metadata=metadata,
        )
        self._sources[key] = source
        return source

    def _table_payload(self, base_url: str, table_id: int) -> dict | None:
        metadata_url = self._url(
            base_url,
            f"/api/table/{table_id}/query_metadata",
            {"include_sensitive_fields": "true", "include_hidden_fields": "true"},
        )
        try:
            payload = self._api_request(metadata_url)
        except MetabaseAPIError as exc:
            if exc.status_code != 404:
                raise
            try:
                payload = self._api_request(self._url(base_url, f"/api/table/{table_id}"))
            except MetabaseAPIError as fallback_exc:
                if fallback_exc.status_code == 404:
                    return None
                raise
        return payload if isinstance(payload, dict) else None

    def _ensure_card(self, base_url: str, card_id: int, stack: set[int]) -> dict | None:
        cached = self._cards.get(card_id)
        if cached is not None:
            return cached
        if card_id in self._missing_cards or card_id in stack:
            return None
        stack.add(card_id)
        try:
            try:
                payload = self._api_request(self._url(base_url, f"/api/card/{card_id}"))
            except MetabaseAPIError as exc:
                if exc.status_code == 404:
                    self._missing_cards.add(card_id)
                    return None
                raise
            if not isinstance(payload, dict):
                self._missing_cards.add(card_id)
                return None
            self._cards[card_id] = payload
            self._harvest_result_metadata(payload)
            source = source_table_of(payload.get("dataset_query") or {})
            if isinstance(source, int):
                self._ensure_table(base_url, source)
            elif isinstance(source, str) and source.startswith("card__"):
                nested = _card_id(source)
                if nested is not None:
                    self._ensure_card(base_url, nested, stack)
            return payload
        finally:
            stack.discard(card_id)

    def _source_from_card(self, base_url: str, card_id: int, stack: set[int]) -> _Source | None:
        key = ("card", card_id)
        existing = self._sources.get(key)
        if existing:
            return existing
        if card_id in stack:
            return None
        card = self._ensure_card(base_url, card_id, stack)
        if card is None:
            return None
        dataset_query = card.get("dataset_query") if isinstance(card.get("dataset_query"), dict) else {}
        source = source_table_of(dataset_query)
        stack.add(card_id)
        try:
            if isinstance(source, int):
                return self._ensure_table(base_url, source)
            if isinstance(source, str) and source.startswith("card__"):
                nested = _card_id(source)
                if nested is not None and nested != card_id:
                    return self._source_from_card(base_url, nested, stack)
        finally:
            stack.discard(card_id)

        native = native_query_sql(dataset_query)
        if native and is_complete_query(native):
            raw_name = card.get("name")
            model_name = sanitize_identifier(raw_name if isinstance(raw_name, str) and raw_name else f"card_{card_id}")
            resolved = _Source(
                key=key,
                model_name=model_name,
                sql=native.strip(),
                metadata={"metabase": {"card_id": card_id}},
            )
            self._sources[key] = resolved
            return resolved
        return None

    def _resolve_field_name(self, base_url: str, field_id: int) -> str | None:
        cached = self._field_names.get(field_id)
        if cached:
            return cached
        try:
            payload = self._api_request(self._url(base_url, f"/api/field/{field_id}"))
        except MetabaseAPIError as exc:
            if exc.status_code == 404:
                return None
            raise
        if isinstance(payload, dict) and isinstance(payload.get("name"), str) and payload["name"]:
            self._field_names[field_id] = payload["name"]
            return payload["name"]
        return None

    def _harvest_result_metadata(self, card: dict) -> None:
        for column in card.get("result_metadata") or []:
            if not isinstance(column, dict) or not isinstance(column.get("name"), str):
                continue
            field_id = _top_level_int(column.get("field_ref"))
            if field_id is not None:
                self._field_names.setdefault(field_id, column["name"])

    def _metrics_for_card(self, card: dict, translated: TranslatedQuery, used: set[str]) -> list[Metric]:
        aggregation = translated.aggregation
        if aggregation is None:
            return []
        raw_name = card.get("name")
        label = raw_name if isinstance(raw_name, str) and raw_name else None
        base_name = sanitize_identifier(label or "metric")
        metric_name = self._claim_name(base_name, used, ("metric", card.get("id")))
        description = card.get("description") if isinstance(card.get("description"), str) else None
        filters = list(translated.filters) or None
        card_id = card.get("id") if isinstance(card.get("id"), int) else None
        metadata = {"metabase": {"card_id": card_id}} if card_id is not None else None

        if aggregation.metric_type == "cumulative":
            base_metric_name = self._claim_name(f"{metric_name}__base", used, ("metric-base", card.get("id")))
            base = Metric(
                name=base_metric_name,
                agg=aggregation.agg,
                sql=aggregation.inner_sql,
                filters=filters,
                public=False,
                visibility="private",
                metadata=metadata,
            )
            cumulative = Metric(
                name=metric_name,
                type="cumulative",
                sql=base_metric_name,
                description=description,
                label=label,
                filters=filters,
                metadata=metadata,
            )
            return [base, cumulative]

        if aggregation.agg:
            return [
                Metric(
                    name=metric_name,
                    agg=aggregation.agg,
                    sql=aggregation.inner_sql,
                    description=description,
                    label=label,
                    filters=filters,
                    metadata=metadata,
                )
            ]

        if aggregation.sql:
            return [
                Metric(
                    name=metric_name,
                    sql=aggregation.sql,
                    sql_is_complete=aggregation.sql_is_complete,
                    description=description,
                    label=label,
                    filters=filters,
                    metadata=metadata,
                )
            ]
        return []

    def _api_request(self, url: str) -> Any:
        """GET a Metabase URL and return decoded JSON.

        Raises:
            MetabaseAPIError: On HTTP failures, connection failures, and invalid JSON.
        """
        request = urllib.request.Request(
            url,
            headers={"X-Api-Key": self.api_key, "Content-Type": "application/json"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise MetabaseAPIError("Authentication failed. Check your API key.", status_code=401) from exc
            if exc.code == 403:
                raise MetabaseAPIError(
                    "Access denied. Ensure API key has sufficient permissions.", status_code=403
                ) from exc
            if exc.code == 404:
                raise MetabaseAPIError(f"Endpoint not found: {url}", status_code=404) from exc
            raise MetabaseAPIError(f"HTTP {exc.code}: {exc.reason}", status_code=exc.code) from exc
        except urllib.error.URLError as exc:
            raise MetabaseAPIError(f"Failed to connect to Metabase: {exc.reason}") from exc

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise MetabaseAPIError("Invalid JSON response from Metabase") from exc

    def _drop(self, construct: str, detail: str) -> None:
        self._note(construct, detail, severity="dropped")

    def _note(self, construct: str, detail: str, *, severity: str) -> None:
        logger.warning("Metabase import %s %s: %s", severity, construct, detail)
        record_import_note(construct, detail, severity=severity, source="metabase")

    @staticmethod
    def _claim_name(preferred: str, used: set[str], key: tuple) -> str:
        if preferred not in used:
            used.add(preferred)
            return preferred
        suffix = key[-1]
        candidate = sanitize_identifier(f"{preferred}_{suffix}")
        n = 2
        while candidate in used:
            candidate = sanitize_identifier(f"{preferred}_{suffix}_{n}")
            n += 1
        used.add(candidate)
        return candidate

    @staticmethod
    def _is_metric_card(card: dict) -> bool:
        return card.get("type") == "metric" and not card.get("archived")

    @staticmethod
    def _unwrap_cards(response: Any) -> tuple[list, int | None]:
        if isinstance(response, list):
            return response, None
        if isinstance(response, dict):
            data = response.get("data", [])
            if not isinstance(data, list):
                raise MetabaseAPIError("Unexpected /api/card payload")
            total = response.get("total")
            return data, total if isinstance(total, int) else None
        raise MetabaseAPIError("Unexpected /api/card payload")

    @staticmethod
    def _url(base_url: str, path: str, params: dict | None = None) -> str:
        url = f"{base_url}{path}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return url


def _card_id(source: str) -> int | None:
    raw = source[6:]
    if raw.isdigit():
        return int(raw)
    return None


def _top_level_int(ref: Any) -> int | None:
    if not isinstance(ref, list):
        return None
    for part in ref[1:]:
        if isinstance(part, int) and not isinstance(part, bool):
            return part
    return None


def _qualified_table(schema: str | None, name: str) -> str:
    table = quote_ident(name)
    if schema:
        return f"{quote_ident(schema)}.{table}"
    return table


def _dimension_type(base_type: str | None, semantic_type: str | None) -> str:
    base = base_type or ""
    semantic = semantic_type or ""
    if "Boolean" in base or semantic == "type/Boolean":
        return "boolean"
    if any(token in base for token in ("Date", "Time")) or semantic.startswith("type/Date") or "Temporal" in semantic:
        return "time"
    if any(token in base for token in ("Integer", "Float", "Decimal", "Number", "BigInteger")):
        return "numeric"
    return "categorical"


def _dimensions_from_fields(fields: list) -> tuple[list[Dimension], str | list[str] | None]:
    ordered = sorted(
        (field for field in fields if isinstance(field, dict)),
        key=lambda field: (field.get("position") is None, field.get("position") or 0, field.get("id") or 0),
    )
    used: set[str] = set()
    dimensions: list[Dimension] = []
    primary_keys: list[str] = []
    for field in ordered:
        physical = field.get("name")
        if not isinstance(physical, str) or not physical:
            continue
        column_sql = quote_ident(physical)
        dim_name = sanitize_identifier(physical)
        n = 2
        while dim_name in used:
            suffix = field.get("id")
            dim_name = sanitize_identifier(f"{physical}_{suffix if n == 2 and suffix is not None else n}")
            n += 1
        used.add(dim_name)
        base_type = field.get("base_type") if isinstance(field.get("base_type"), str) else None
        semantic = field.get("semantic_type") or field.get("special_type")
        semantic = semantic if isinstance(semantic, str) else None
        dim_type = _dimension_type(base_type, semantic)
        kwargs: dict[str, Any] = {
            "name": dim_name,
            "type": dim_type,
            "sql": column_sql,
            "description": field.get("description") if isinstance(field.get("description"), str) else None,
            "label": field.get("display_name") if isinstance(field.get("display_name"), str) else None,
        }
        if dim_type == "time":
            kwargs["granularity"] = "day" if base_type and "Date" in base_type else "hour"
        dimensions.append(Dimension(**kwargs))
        if semantic in {"type/PK", "type/PrimaryKey"}:
            primary_keys.append(column_sql)

    if len(primary_keys) == 1:
        return dimensions, primary_keys[0]
    if primary_keys:
        return dimensions, primary_keys
    return dimensions, None
