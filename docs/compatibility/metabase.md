# Metabase

Import-only adapter for Metabase metrics, which are cards with `type: metric`. The adapter calls the REST API with an `X-Api-Key` header. It does not read Metabase serialization YAML; MBQL translation is split out so a later YAML loader can reuse it.

```python
from sidemantic.adapters.metabase import MetabaseAdapter

graph = MetabaseAdapter(api_key="mb_...").parse("https://metabase.example.com")
```

The format name is `metabase`. Export is not supported. Directory auto-discovery does not select this adapter, because the source is an instance URL rather than files. Construct the adapter from `sidemantic.formats.get_semantic_format("metabase")` when you want the registry entry.

## What is imported

- MBQL aggregations become measures. `sum`, `count`, `avg`, `min`, `max`, `distinct`, and arithmetic combinations are translated.
- `cum-sum` and `cum-count` become `type: cumulative` metrics that reference a private base measure.
- Integer `source-table` values are resolved with `GET /api/table/:id/query_metadata` (`include_hidden_fields` and `include_sensitive_fields` set). `GET /api/field/:id` fills in any field id missing from that payload. `card__` sources follow the saved question back to its table, or use its native SQL as the model query.
- A primary key is recorded only when a field's `semantic_type` is `type/PK`. A column named `id` is not assumed to be the key.
- Legacy MBQL and MBQL 5 clause shapes (option maps between the operator and its arguments) are both accepted.

## Limits

- `GET /api/card` is requested once, with no `limit`, `offset`, or `type` parameter. Those parameters are not part of the documented card-list contract, and Metabase returns the full list in one response. Metric cards are selected by `type == "metric"` after the response is read. If the body is a page envelope that includes `data` and `total`, later pages are requested and a repeated page raises `MetabaseAPIError` instead of returning a partial graph.
- Authentication and authorization failures, including a 401 or 403 on the first card page or on table metadata, raise `MetabaseAPIError`.
- A native query that is a complete `SELECT` or `WITH` statement is skipped. A native expression that is not a full statement is stored with `sql_is_complete=True`.
- `share` is skipped. Sidemantic has no share metric type that preserves a single aggregation level.
- Filter literals are quoted, and `LIKE` patterns escape quotes, `%`, and `_`.

MBQL helpers live in `sidemantic.adapters.metabase_mbql` and perform no HTTP.
