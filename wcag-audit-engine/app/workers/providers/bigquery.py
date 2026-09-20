"""BigQuery over its REST API -- the data provider behind the analysis worker.

Connected, not rebuilt: BigQuery does the querying. This adapter's only jobs
are (a) never letting a customer's question run up an unbounded bill, and
(b) reporting how many bytes it actually cost.

THE COST GATE, WHICH IS THE WHOLE POINT

BigQuery on-demand bills by bytes scanned, and a careless query against a
public dataset can scan terabytes. So every query is dry-run FIRST -- free,
and it returns the exact byte count -- and the real run is refused if that
exceeds the ceiling. The run also carries maximumBytesBilled, so even if the
estimate were wrong, BigQuery itself kills the job rather than billing us.
Two independent brakes, because this is the one worker that can lose real
money on a single malformed request.

Bytes are measured exactly. The price defaults to Google's published
on-demand rate, $6.25 per TiB (cloud.google.com/bigquery/pricing, read
2026-09-19; AI.FORECAST's TimesFM is billed on the same rate), charged at
list even inside the 1 TiB monthly free tier so the ledger never flatters a
margin. BQ_PRICE_PER_TIB overrides it.
"""

import logging
import os
import re
from typing import Optional

import httpx

from .. import runtime
from . import google_auth

log = logging.getLogger("hubvibe.workers.bigquery")

_TIMEOUT = float(os.environ.get("WORKER_BQ_TIMEOUT_SECONDS", "120"))
_MAX_GIB = float(os.environ.get("WORKER_BQ_MAX_SCAN_GIB", "20"))
_MAX_ROWS = int(os.environ.get("WORKER_BQ_MAX_ROWS", "200"))
_BYTES_PER_GIB = 1024 ** 3
_BYTES_PER_TIB = 1024 ** 4

# Read-only by construction. BigQuery permissions should enforce this too, but
# a worker that accepts arbitrary customer SQL must not rely solely on IAM
# being configured correctly on every deployment.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|CREATE|ALTER|TRUNCATE|GRANT|REVOKE|"
    r"EXPORT|LOAD|CALL|BEGIN|COMMIT|ROLLBACK)\b", re.IGNORECASE)


def _price_per_tib() -> Optional[float]:
    raw = os.environ.get("BQ_PRICE_PER_TIB", "6.25")
    try:
        return float(raw)
    except ValueError:
        return None


def _cost_micros(bytes_processed: int):
    rate = _price_per_tib()
    if rate is None:
        return None, False
    return int(round((bytes_processed / _BYTES_PER_TIB) * rate * 1_000_000)), True


def validate_sql(sql: str) -> str:
    """Reject anything that is not a single read. Raises InvalidRequest, which
    the router answers BEFORE payment is read."""
    if not sql or not sql.strip():
        raise runtime.InvalidRequest("`sql` is required.")
    cleaned = sql.strip().rstrip(";")
    if ";" in cleaned:
        raise runtime.InvalidRequest("Only a single statement is allowed.")
    if _FORBIDDEN.search(cleaned):
        raise runtime.InvalidRequest(
            "This worker runs read-only SELECT queries; the statement contains a "
            "write or DDL keyword.")
    if not re.match(r"^\s*(SELECT|WITH)\b", cleaned, re.IGNORECASE):
        raise runtime.InvalidRequest("Query must begin with SELECT or WITH.")
    return cleaned


class _BigQuery:
    id = "bigquery"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    def _url(self) -> str:
        return (f"https://bigquery.googleapis.com/bigquery/v2/projects/"
                f"{google_auth.project()}/queries")

    async def _post(self, body: dict) -> dict:
        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(self._url(), headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"BigQuery timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"BigQuery unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"BigQuery returned {response.status_code}", reason="provider_overloaded")
        if response.status_code >= 400:
            detail = ""
            try:
                detail = (response.json().get("error") or {}).get("message", "")
            except Exception:
                detail = response.text[:200]
            raise runtime.PermanentProviderError(f"BigQuery rejected the query: {detail}")
        return response.json()

    async def estimate(self, sql: str) -> int:
        """Bytes this query would scan. Free -- BigQuery does not bill dry runs."""
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())
        data = await self._post({"query": sql, "useLegacySql": False, "dryRun": True})
        return int(data.get("totalBytesProcessed") or 0)

    async def query(self, sql: str, max_gib: Optional[float] = None) -> runtime.ProviderResult:
        """Estimate, refuse if too large, then run under a hard byte ceiling."""
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())

        ceiling_gib = max_gib if max_gib is not None else _MAX_GIB
        ceiling_bytes = int(ceiling_gib * _BYTES_PER_GIB)

        estimated = await self.estimate(sql)
        if estimated > ceiling_bytes:
            # InvalidRequest, not a provider failure: the query is the problem
            # and no retry or fallback can help. The caller is told the real
            # number so it can narrow the query itself.
            raise runtime.InvalidRequest(
                f"Query would scan {estimated / _BYTES_PER_GIB:.2f} GiB, over this "
                f"worker's {ceiling_gib:.0f} GiB limit. Narrow it (fewer columns, "
                f"a partition filter, or a LIMIT on a subquery).")

        data = await self._post({
            "query": sql,
            "useLegacySql": False,
            "maximumBytesBilled": str(ceiling_bytes),
            "maxResults": _MAX_ROWS,
            "timeoutMs": int(_TIMEOUT * 1000),
        })

        # jobs.query answers jobComplete:false -- with no rows and no error --
        # when the job outlives timeoutMs. Unchecked, that reads as a successful
        # empty result and the caller is billed for "no data" when the truth is
        # "we stopped waiting". Raising means the gate never settles.
        if data.get("jobComplete") is False:
            raise runtime.TransientProviderError(
                f"BigQuery did not finish within {_TIMEOUT:.0f}s; no rows came back.",
                reason="provider_timeout")

        schema = [f.get("name") for f in (data.get("schema") or {}).get("fields", [])]
        rows = []
        for row in (data.get("rows") or [])[:_MAX_ROWS]:
            values = [cell.get("v") for cell in row.get("f", [])]
            rows.append(dict(zip(schema, values)) if schema else values)

        billed = int(data.get("totalBytesProcessed") or estimated or 0)
        cost, measured = _cost_micros(billed)

        return runtime.ProviderResult(
            value={
                "columns": schema,
                "rows": rows,
                "row_count": len(rows),
                "total_rows": int(data.get("totalRows") or len(rows)),
                "truncated": int(data.get("totalRows") or 0) > len(rows),
                "bytes_processed": billed,
                "gib_processed": round(billed / _BYTES_PER_GIB, 4),
                "cache_hit": bool(data.get("cacheHit")),
            },
            cost_micros=cost, cost_measured=measured,
            usage=f"bytes={billed}")


PROVIDERS = [_BigQuery()]
PROVIDER = PROVIDERS[0]
