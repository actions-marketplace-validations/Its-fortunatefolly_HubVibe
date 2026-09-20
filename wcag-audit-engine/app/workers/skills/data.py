"""Data workers backed by BigQuery.

Two shapes, deliberately:

  data.query    the caller brings SQL. Deterministic, and the cheapest way to
                get an exact answer out of a dataset the caller already knows.
  data.question the caller brings a QUESTION and a table. Gemini writes the
                SQL, BigQuery runs it under the byte ceiling, Gemini reads the
                rows back as an answer. This is the composite the brief asks
                for: a data question in, an analysis out.

Both run through the adapter's dry-run gate, so neither can scan more than
the configured ceiling no matter what SQL is produced.
"""

import re

from .. import runtime
from ..providers import bigquery, gemini

_TABLE = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_SQL_AUTHOR = (
    "You write BigQuery Standard SQL. Return ONLY the SQL, with no markdown "
    "fence and no commentary. Rules: a single read-only SELECT; always include "
    "a LIMIT of at most 200; select only the columns needed to answer; prefer "
    "aggregates over raw rows; never use SELECT *."
)


async def run_sql(ctx, payload: dict) -> dict:
    """Execute caller-supplied read-only SQL under the byte ceiling."""
    sql = bigquery.validate_sql(payload.get("sql"))
    max_gib = payload.get("max_scan_gib")
    if max_gib is not None:
        try:
            max_gib = float(max_gib)
        except (TypeError, ValueError):
            raise runtime.InvalidRequest("`max_scan_gib` must be a number.")

    async def call(provider):
        return await provider.query(sql, max_gib=max_gib)

    value = await ctx.run("query", bigquery.PROVIDERS, call, per_attempt_seconds=120)
    return {
        "sql": sql,
        "columns": value["columns"],
        "rows": value["rows"],
        "row_count": value["row_count"],
        "total_rows": value["total_rows"],
        "truncated": value["truncated"],
        "gib_processed": value["gib_processed"],
        "cache_hit": value["cache_hit"],
    }


async def answer_question(ctx, payload: dict) -> dict:
    """A data question against a named table, answered end to end.

    Three steps, one price: write the SQL, run it, read the result back. The
    generated SQL and the rows both come back alongside the answer, because a
    buying agent that cannot see the query has no way to check the analysis.
    """
    question = (payload.get("question") or "").strip()
    if not question:
        raise runtime.InvalidRequest("`question` is required.")
    table = (payload.get("table") or "").strip()
    if not _TABLE.match(table):
        raise runtime.InvalidRequest(
            "`table` must be a fully qualified BigQuery table: project.dataset.table "
            "(for example bigquery-public-data.usa_names.usa_1910_2013).")

    schema_hint = payload.get("columns")
    columns_note = ""
    if isinstance(schema_hint, list) and schema_hint:
        columns_note = f"\nColumns available: {', '.join(str(c) for c in schema_hint)}"

    prompt = (
        f"Table: `{table}`{columns_note}\n\n"
        f"Question: {question}\n\n"
        "Write one BigQuery Standard SQL query that answers it."
    )

    async def write_sql(provider):
        return await provider.generate(prompt, system=_SQL_AUTHOR, temperature=0.0)

    drafted = await ctx.run("write_sql", gemini.PROVIDERS, write_sql, per_attempt_seconds=90)
    raw = drafted["text"].strip()
    # Models fence SQL even when told not to; strip it rather than failing the
    # paid job over punctuation.
    fenced = re.match(r"^```(?:sql)?\s*(.+?)\s*```$", raw, re.DOTALL | re.IGNORECASE)
    if fenced:
        raw = fenced.group(1).strip()
    sql = bigquery.validate_sql(raw)

    async def run_query(provider):
        return await provider.query(sql)

    data = await ctx.run("query", bigquery.PROVIDERS, run_query, per_attempt_seconds=120)

    rows_preview = data["rows"][:50]
    answer_prompt = (
        f"Question: {question}\n\nSQL that was run:\n{sql}\n\n"
        f"Result columns: {data['columns']}\nRows: {rows_preview}\n\n"
        "Answer the question from these rows. State the figures explicitly. "
        "If the rows do not answer it, say so."
    )

    async def summarize(provider):
        return await provider.generate(
            answer_prompt,
            system=("You are a data analyst. Use only the rows provided. "
                    "Never invent numbers."),
            temperature=0.1)

    summary = await ctx.run("summarize", gemini.PROVIDERS, summarize, per_attempt_seconds=90)

    return {
        "question": question,
        "table": table,
        "answer": summary["text"],
        "sql": sql,
        "columns": data["columns"],
        "rows": rows_preview,
        "row_count": data["row_count"],
        "gib_processed": data["gib_processed"],
        "model": summary["model"],
    }


def _id_cols_sql(payload: dict) -> str:
    """`, id_cols => [...]` for a table holding several series at once (one
    row per series per timestamp), or "" when the caller names none."""
    id_cols = payload.get("id_cols")
    if id_cols is None:
        return ""
    if not isinstance(id_cols, list) or not id_cols or \
            not all(isinstance(c, str) and _IDENT.match(c) for c in id_cols):
        raise runtime.InvalidRequest(
            "`id_cols`, when given, must be a non-empty list of column names.")
    return ", id_cols => [" + ", ".join(f"'{c}'" for c in id_cols) + "]"


async def forecast(ctx, payload: dict) -> dict:
    """Forecast a time series in a BigQuery table using AI.FORECAST.

    Google's own pretrained TimesFM model, called directly -- no model to
    train, no model to own. Point it at a table and the columns to read.
    """
    table = (payload.get("table") or "").strip()
    if not _TABLE.match(table):
        raise runtime.InvalidRequest(
            "`table` must be a fully qualified BigQuery table: project.dataset.table.")
    timestamp_col = (payload.get("timestamp_col") or "").strip()
    data_col = (payload.get("data_col") or "").strip()
    if not timestamp_col or not data_col:
        raise runtime.InvalidRequest("`timestamp_col` and `data_col` are required.")
    if not _IDENT.match(timestamp_col) or not _IDENT.match(data_col):
        raise runtime.InvalidRequest("`timestamp_col`/`data_col` must be plain column names.")
    try:
        horizon = int(payload.get("horizon", 10))
    except (TypeError, ValueError):
        raise runtime.InvalidRequest("`horizon` must be a whole number.")
    if not 1 <= horizon <= 1000:
        raise runtime.InvalidRequest("`horizon` must be between 1 and 1000.")

    id_cols_sql = _id_cols_sql(payload)

    sql = (
        f"SELECT * FROM AI.FORECAST((SELECT * FROM `{table}`), "
        f"data_col => '{data_col}', timestamp_col => '{timestamp_col}', "
        f"horizon => {horizon}{id_cols_sql})")

    async def call(provider):
        return await provider.query(sql, max_gib=payload.get("max_scan_gib"))

    value = await ctx.run("forecast", bigquery.PROVIDERS, call, per_attempt_seconds=150)
    return {
        "table": table, "timestamp_col": timestamp_col, "data_col": data_col,
        "horizon": horizon, "columns": value["columns"], "rows": value["rows"],
        "row_count": value["row_count"], "gib_processed": value["gib_processed"],
    }


async def detect_anomalies(ctx, payload: dict) -> dict:
    """Detect anomalies in a target table's time series, forecast against a
    history table, using AI.DETECT_ANOMALIES.

    Two tables because that is the function's own contract: TimesFM forecasts
    from the history table and flags where the target table's actual values
    depart from that forecast. Both must share the same column names.
    """
    history_table = (payload.get("history_table") or "").strip()
    target_table = (payload.get("target_table") or "").strip()
    if not _TABLE.match(history_table):
        raise runtime.InvalidRequest(
            "`history_table` must be a fully qualified BigQuery table: project.dataset.table.")
    if not _TABLE.match(target_table):
        raise runtime.InvalidRequest(
            "`target_table` must be a fully qualified BigQuery table: project.dataset.table.")
    timestamp_col = (payload.get("timestamp_col") or "").strip()
    data_col = (payload.get("data_col") or "").strip()
    if not timestamp_col or not data_col:
        raise runtime.InvalidRequest("`timestamp_col` and `data_col` are required.")
    if not _IDENT.match(timestamp_col) or not _IDENT.match(data_col):
        raise runtime.InvalidRequest("`timestamp_col`/`data_col` must be plain column names.")
    try:
        threshold = float(payload.get("anomaly_prob_threshold", 0.95))
    except (TypeError, ValueError):
        raise runtime.InvalidRequest("`anomaly_prob_threshold` must be a number.")
    if not 0.5 <= threshold <= 0.999:
        raise runtime.InvalidRequest("`anomaly_prob_threshold` must be between 0.5 and 0.999.")

    sql = (
        f"SELECT * FROM AI.DETECT_ANOMALIES(TABLE `{history_table}`, TABLE `{target_table}`, "
        f"data_col => '{data_col}', timestamp_col => '{timestamp_col}', "
        f"anomaly_prob_threshold => {threshold}{_id_cols_sql(payload)})")

    async def call(provider):
        return await provider.query(sql, max_gib=payload.get("max_scan_gib"))

    value = await ctx.run("detect_anomalies", bigquery.PROVIDERS, call, per_attempt_seconds=150)
    return {
        "history_table": history_table, "target_table": target_table,
        "timestamp_col": timestamp_col, "data_col": data_col,
        "anomaly_prob_threshold": threshold, "columns": value["columns"],
        "rows": value["rows"], "row_count": value["row_count"],
        "gib_processed": value["gib_processed"],
    }


SKILLS = {"data.query": run_sql, "data.question": answer_question,
          "data.forecast": forecast, "data.anomalies": detect_anomalies}
