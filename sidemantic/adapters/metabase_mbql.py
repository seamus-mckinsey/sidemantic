"""MBQL to SQL translation with no HTTP.

The Metabase REST adapter imports these helpers. A future loader for Metabase
serialization YAML can call the same functions with the same clause shapes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

_SIMPLE_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Sidemantic aggregation names and the SQL function that renders each one.
# ``distinct`` is intentionally absent: MBQL distinct is COUNT(DISTINCT ...),
# which is ``count_distinct``, and a map entry of ``COUNT_DISTINCT`` is never rendered.
_SIMPLE_AGGS = {
    "sum": "sum",
    "avg": "avg",
    "min": "min",
    "max": "max",
    "median": "median",
    "stddev": "stddev",
    "var": "variance",
}
_SQL_FUNCS = {
    "sum": "SUM",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
    "median": "MEDIAN",
    "stddev": "STDDEV",
    "var": "VARIANCE",
}
_COMPARE = {"=": "=", "!=": "!=", "<": "<", ">": ">", "<=": "<=", ">=": ">="}

FieldResolver = Callable[[int], str | None]


class MBQLTranslationError(ValueError):
    """Raised when an MBQL clause cannot be translated without inventing SQL."""


@dataclass(frozen=True)
class Aggregation:
    """One MBQL aggregation translated for a Sidemantic metric.

    Simple aggregations set ``agg`` and ``inner_sql`` (the column, or None for
    COUNT(*)) and also ``sql`` as the full function call. Cumulative aggregations
    set ``metric_type='cumulative'`` and leave ``sql`` empty so callers do not
    flatten them to a plain SUM or COUNT. ``skipped`` explains clauses that must
    not be imported.
    """

    sql: str | None = None
    agg: str | None = None
    inner_sql: str | None = None
    metric_type: str | None = None
    sql_is_complete: bool = False
    skipped: str | None = None


@dataclass(frozen=True)
class TranslatedQuery:
    """Dataset query reduced to a source, one aggregation, and filter predicates."""

    source_table: Any = None
    aggregation: Aggregation | None = None
    filters: tuple[str, ...] = ()
    native_sql: str | None = None
    skip_reason: str | None = None
    extra_aggregations: int = 0


def sanitize_identifier(name: str) -> str:
    """Turn a display name into a lowercase Sidemantic identifier."""
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    sanitized = re.sub(r"_+", "_", sanitized).strip("_")
    if sanitized and not sanitized[0].isalpha():
        sanitized = f"m_{sanitized}"
    return sanitized.lower() or "unnamed"


def quote_ident(name: str) -> str:
    """Quote a column or table name when it is not a plain identifier."""
    if not isinstance(name, str) or name == "":
        raise MBQLTranslationError("column name is empty")
    if _SIMPLE_IDENT.match(name):
        return name
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: Any) -> str:
    """Render a filter literal as SQL, quoting strings and rejecting other objects."""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise MBQLTranslationError("non-finite numeric filter value")
        return repr(value)
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    raise MBQLTranslationError(f"unsupported filter value type {type(value).__name__}")


def is_complete_query(sql: str) -> bool:
    """True when ``sql`` is a statement rather than a scalar expression."""
    text = sql.strip()
    while text.startswith("("):
        text = text[1:].lstrip()
    if not text:
        return False
    head = text.split(None, 1)[0].lower().rstrip("(")
    return head in {"select", "with"}


def aggregation_sql(clause: Any, resolve_field: FieldResolver | Mapping[int, str] | None = None) -> Aggregation:
    """Translate one MBQL aggregation clause."""
    resolve = _as_resolver(resolve_field)
    if not isinstance(clause, list) or not clause or not isinstance(clause[0], str):
        return Aggregation(skipped="aggregation clause is not an MBQL list")

    op = clause[0]
    args = _positional(clause)

    if op == "count":
        if not args:
            return Aggregation(sql="COUNT(*)", agg="count")
        inner = _field_sql(args[0], resolve)
        return Aggregation(sql=f"COUNT({inner})", agg="count", inner_sql=inner)

    if op == "distinct":
        if not args:
            return Aggregation(skipped="distinct aggregation is missing a field")
        inner = _field_sql(args[0], resolve)
        return Aggregation(sql=f"COUNT(DISTINCT {inner})", agg="count_distinct", inner_sql=inner)

    if op == "cum-sum":
        if not args:
            return Aggregation(skipped="cum-sum aggregation is missing a field")
        inner = _field_sql(args[0], resolve)
        return Aggregation(metric_type="cumulative", agg="sum", inner_sql=inner)

    if op == "cum-count":
        if not args:
            return Aggregation(metric_type="cumulative", agg="count")
        inner = _field_sql(args[0], resolve)
        return Aggregation(metric_type="cumulative", agg="count", inner_sql=inner)

    if op in {"share", "share-where"}:
        return Aggregation(
            skipped=(
                "share aggregation is not imported because Sidemantic has no share metric type "
                "that keeps a single aggregation level"
            )
        )

    if op in _SIMPLE_AGGS:
        if not args:
            return Aggregation(skipped=f"{op} aggregation is missing a field")
        inner = _field_sql(args[0], resolve)
        return Aggregation(sql=f"{_SQL_FUNCS[op]}({inner})", agg=_SIMPLE_AGGS[op], inner_sql=inner)

    if op in {"+", "-", "*", "/"}:
        if len(args) < 2:
            return Aggregation(skipped=f"arithmetic aggregation {op} is missing an operand")
        left = aggregation_sql(args[0], resolve)
        right = aggregation_sql(args[1], resolve)
        if left.skipped or right.skipped:
            return Aggregation(skipped=left.skipped or right.skipped)
        if left.metric_type or right.metric_type:
            return Aggregation(skipped="cumulative aggregations cannot be combined with arithmetic in one metric")
        if not left.sql or not right.sql:
            return Aggregation(skipped="arithmetic aggregation is missing a side")
        return Aggregation(sql=f"({left.sql} {op} {right.sql})")

    return Aggregation(skipped=f"unsupported MBQL aggregation {op}")


def filter_sql(clause: Any, resolve_field: FieldResolver | Mapping[int, str] | None = None) -> str | None:
    """Translate one MBQL filter clause to a SQL predicate."""
    resolve = _as_resolver(resolve_field)
    if clause is None:
        return None
    if not isinstance(clause, list) or not clause or not isinstance(clause[0], str):
        raise MBQLTranslationError("filter clause is not an MBQL list")

    op = clause[0]
    operands = _positional(clause)

    if op in {"and", "or"}:
        parts = []
        for part in operands:
            rendered = filter_sql(part, resolve)
            if rendered:
                parts.append(rendered)
        if not parts:
            return None
        joined = (" AND " if op == "and" else " OR ").join(parts)
        return joined if op == "and" else f"({joined})"

    if op == "not":
        if not operands:
            raise MBQLTranslationError("not filter is missing an operand")
        inner = filter_sql(operands[0], resolve)
        return f"NOT ({inner})" if inner else None

    if not operands:
        raise MBQLTranslationError(f"{op} filter is missing a field")

    field = _field_sql(operands[0], resolve)
    values = operands[1:]

    if op in {"is-null", "not-null"}:
        return f"{field} {'IS NULL' if op == 'is-null' else 'IS NOT NULL'}"
    if op == "is-empty":
        return f"({field} IS NULL OR {field} = '')"
    if op == "not-empty":
        return f"({field} IS NOT NULL AND {field} <> '')"
    if op in {"contains", "starts-with", "ends-with", "does-not-contain"}:
        if not values:
            raise MBQLTranslationError(f"{op} filter is missing a value")
        mode = op if op in {"starts-with", "ends-with"} else "contains"
        like = _like(field, values[0], mode=mode)
        return f"NOT ({like})" if op == "does-not-contain" else like
    if op == "between":
        if len(values) < 2:
            raise MBQLTranslationError("between filter is missing bounds")
        return f"{field} BETWEEN {_value_sql(values[0], resolve)} AND {_value_sql(values[1], resolve)}"
    if op in {"in", "not-in"}:
        literals = _in_list(values, resolve)
        keyword = "IN" if op == "in" else "NOT IN"
        return f"{field} {keyword} ({literals})"
    if op in _COMPARE:
        if not values:
            raise MBQLTranslationError(f"{op} filter is missing a value")
        if len(values) == 1 and _unwrap(values[0]) is None and op in {"=", "!="}:
            return f"{field} {'IS NULL' if op == '=' else 'IS NOT NULL'}"
        if len(values) > 1 and op in {"=", "!="}:
            keyword = "IN" if op == "=" else "NOT IN"
            return f"{field} {keyword} ({_in_list(values, resolve)})"
        return f"{field} {_COMPARE[op]} {_value_sql(values[0], resolve)}"

    raise MBQLTranslationError(f"unsupported MBQL filter {op}")


def source_table_of(dataset_query: dict) -> Any:
    """Return the raw MBQL ``source-table`` value, if the query has one."""
    if not isinstance(dataset_query, dict):
        return None
    stages = _stages(dataset_query)
    if not stages:
        return None
    stage = _aggregation_stage(stages) or stages[0]
    return _source_from_stages(stages, stage)


def native_query_sql(dataset_query: dict) -> str | None:
    """Return native SQL text from a legacy or MBQL 5 dataset query."""
    if not isinstance(dataset_query, dict):
        return None
    return _native_sql(dataset_query, _stages(dataset_query) or [])


def translate_dataset_query(
    dataset_query: dict, resolve_field: FieldResolver | Mapping[int, str] | None = None
) -> TranslatedQuery:
    """Translate a metric card ``dataset_query`` into source, aggregation, and filters."""
    if not isinstance(dataset_query, dict):
        return TranslatedQuery(skip_reason="dataset_query is not an object")

    stages = _stages(dataset_query)
    mbql = _aggregation_stage(stages) if stages else None
    if mbql is None:
        native = _native_sql(dataset_query, stages or [])
        if native is None or not native.strip():
            return TranslatedQuery(skip_reason="query has no MBQL aggregation or native SQL")
        if is_complete_query(native):
            return TranslatedQuery(
                native_sql=native,
                skip_reason="native query is a complete SELECT/WITH statement, so it is not stored on the metric",
            )
        return TranslatedQuery(
            native_sql=native,
            aggregation=Aggregation(sql=native.strip(), sql_is_complete=True),
        )

    source = _source_from_stages(stages or [], mbql)
    aggregations = mbql.get("aggregation") or []
    if not isinstance(aggregations, list) or not aggregations:
        return TranslatedQuery(source_table=source, skip_reason="metric has no aggregation")

    try:
        aggregation = aggregation_sql(aggregations[0], resolve_field)
    except MBQLTranslationError as exc:
        return TranslatedQuery(source_table=source, skip_reason=str(exc))
    extra = max(0, len(aggregations) - 1)
    if aggregation.skipped:
        return TranslatedQuery(
            source_table=source,
            aggregation=aggregation,
            skip_reason=aggregation.skipped,
            extra_aggregations=extra,
        )

    try:
        filters = _filters_for_stage(mbql, resolve_field)
    except MBQLTranslationError as exc:
        return TranslatedQuery(source_table=source, skip_reason=str(exc), extra_aggregations=extra)

    return TranslatedQuery(
        source_table=source,
        aggregation=aggregation,
        filters=tuple(filters),
        extra_aggregations=extra,
    )


def _as_resolver(resolve_field: FieldResolver | Mapping[int, str] | None) -> FieldResolver:
    if resolve_field is None:
        return lambda _field_id: None
    if isinstance(resolve_field, Mapping):
        names = resolve_field

        def _from_mapping(field_id: int) -> str | None:
            name = names.get(field_id)
            return name if isinstance(name, str) and name else None

        return _from_mapping
    return resolve_field


def _positional(clause: list) -> list[Any]:
    """Drop MBQL option maps so legacy and MBQL 5 clauses share a shape."""
    return [part for part in clause[1:] if not isinstance(part, dict)]


def _field_sql(field: Any, resolve: FieldResolver) -> str:
    if isinstance(field, str):
        return quote_ident(field)
    if not isinstance(field, list) or not field or not isinstance(field[0], str):
        raise MBQLTranslationError("field reference is not a name or MBQL clause")

    kind = field[0]
    args = [part for part in field[1:] if part is not None and not isinstance(part, dict)]
    if kind == "field":
        ref = next(
            (part for part in reversed(args) if isinstance(part, (str, int)) and not isinstance(part, bool)), None
        )
        if isinstance(ref, str):
            return quote_ident(ref)
        if isinstance(ref, int):
            name = resolve(ref)
            if not name:
                raise MBQLTranslationError(f"unresolved Metabase field id {ref}")
            return quote_ident(name)
        raise MBQLTranslationError("field clause is missing an id or name")
    if kind == "expression":
        if args and isinstance(args[0], str):
            return quote_ident(args[0])
        raise MBQLTranslationError("expression reference is missing a name")
    if kind == "aggregation":
        raise MBQLTranslationError("aggregation references are not expanded inside metric SQL")
    raise MBQLTranslationError(f"unsupported field clause {kind}")


def _unwrap(value: Any) -> Any:
    if isinstance(value, list) and value and value[0] == "value":
        if len(value) < 2:
            raise MBQLTranslationError("value clause is empty")
        return value[1]
    return value


def _value_sql(value: Any, resolve: FieldResolver) -> str:
    value = _unwrap(value)
    if isinstance(value, list) and value and isinstance(value[0], str) and value[0] in {"field", "expression"}:
        return _field_sql(value, resolve)
    return quote_literal(value)


def _like(field: str, value: Any, mode: str = "contains") -> str:
    value = _unwrap(value)
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise MBQLTranslationError("LIKE filter value must be a string or number")
    text = value if isinstance(value, str) else str(value)
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_").replace("'", "''")
    if mode == "starts-with":
        pattern = f"{escaped}%"
    elif mode == "ends-with":
        pattern = f"%{escaped}"
    else:
        pattern = f"%{escaped}%"
    return f"{field} LIKE '{pattern}' ESCAPE '\\'"


def _in_list(values: list[Any], resolve: FieldResolver) -> str:
    if len(values) == 1 and isinstance(_unwrap(values[0]), list):
        values = _unwrap(values[0])
    if not values:
        raise MBQLTranslationError("IN filter has no values")
    return ", ".join(_value_sql(value, resolve) for value in values)


def _stages(dataset_query: dict) -> list[dict] | None:
    if dataset_query.get("type") == "native":
        return None
    raw_stages = dataset_query.get("stages")
    if isinstance(raw_stages, list):
        return [stage for stage in raw_stages if isinstance(stage, dict)]
    query = dataset_query.get("query")
    if isinstance(query, dict):
        nested = query.get("stages")
        if isinstance(nested, list):
            return [stage for stage in nested if isinstance(stage, dict)]
        return [query]
    if dataset_query.get("lib/type") == "mbql/query":
        return []
    return None


def _aggregation_stage(stages: list[dict]) -> dict | None:
    for stage in stages:
        if stage.get("aggregation") and stage.get("lib/type") != "mbql.stage/native":
            return stage
    return None


def _source_from_stages(stages: list[dict], stage: dict) -> Any:
    if "source-table" in stage:
        return stage.get("source-table")
    for candidate in stages:
        if "source-table" in candidate:
            return candidate.get("source-table")
    return None


def _native_text(native: Any) -> str | None:
    if isinstance(native, str):
        return native
    if isinstance(native, dict):
        query = native.get("query")
        if isinstance(query, str):
            return query
    return None


def _native_sql(dataset_query: dict, stages: list[dict]) -> str | None:
    if dataset_query.get("type") == "native":
        return _native_text(dataset_query.get("native"))
    for stage in stages:
        if stage.get("lib/type") == "mbql.stage/native" or (
            "native" in stage and "aggregation" not in stage and "source-table" not in stage
        ):
            text = _native_text(stage.get("native"))
            if text is not None:
                return text
    return None


def _filters_for_stage(stage: dict, resolve_field: FieldResolver | Mapping[int, str] | None) -> list[str]:
    rendered: list[str] = []
    legacy = stage.get("filter")
    many = stage.get("filters")
    if legacy:
        sql = filter_sql(legacy, resolve_field)
        if sql:
            rendered.append(sql)
    if isinstance(many, list) and many:
        clause: Any = many[0] if len(many) == 1 else ["and", *many]
        sql = filter_sql(clause, resolve_field)
        if sql:
            rendered.append(sql)
    return rendered
