"""The machine-readable contract of every worker: what a paid call returns.

One JSON Schema per worker, written from the literal `return {...}` of the
skill that produces it (app/workers/skills/*.py), plus the envelope the
router wraps every result in. Three surfaces read these and must agree:

  * openapi.json     -- the 200 response schema and example of each /work route
  * agent.json       -- `output_schema` on each capability, `response_envelope`
  * the 402's Bazaar record -- `info.output` example and schema

Before this file every one of those said `{"status": "ok"}` or "object; see
`returns`", which told a shopping agent nothing about what it was buying.
The Bazaar ranks on the completeness of the output schema; an agent choosing
between two sellers reads it; a pipeline routing a job checks the keys. So
the keys here are the keys the code emits -- a guard test generates an
example from each schema and validates it, and the live simulation
(scripts/simulate-work-calls.py) can validate delivered results against them.

`examples` on a property is the value the generated example uses. JSON Schema
2020-12 defines the keyword; validators ignore it; OpenAPI 3.1 renders it.

REPRESENTATIVE_QUERIES feeds /.well-known/ard.json: the natural-language
asks a registry builds its semantic index from (ARD spec, "SHOULD contain
2-5 examples").
"""

from typing import Any

# --- schema helpers -----------------------------------------------------------


def _s(description: str, example: Any, nullable: bool = False) -> dict:
    """A string property with the example the docs show."""
    return {"type": ["string", "null"] if nullable else "string",
            "description": description, "examples": [example]}


def _i(description: str, example: int, nullable: bool = False) -> dict:
    return {"type": ["integer", "null"] if nullable else "integer",
            "description": description, "examples": [example]}


def _n(description: str, example: float, nullable: bool = False) -> dict:
    return {"type": ["number", "null"] if nullable else "number",
            "description": description, "examples": [example]}


def _b(description: str, example: bool) -> dict:
    return {"type": "boolean", "description": description, "examples": [example]}


def _const(value: str, description: str) -> dict:
    return {"type": "string", "const": value, "description": description}


def _enum(values: list, description: str, example: str) -> dict:
    return {"type": "string", "enum": values, "description": description, "examples": [example]}


def _obj(properties: dict, required: list, description: str = "", **extra) -> dict:
    schema = {"type": "object", "properties": properties, "required": required}
    if description:
        schema["description"] = description
    schema.update(extra)
    return schema


def _arr(items: dict, description: str, example: list = None) -> dict:
    schema = {"type": "array", "items": items, "description": description}
    if example is not None:
        schema["examples"] = [example]
    return schema


def _free(description: str, example: Any = None, types=None) -> dict:
    """A value whose shape belongs to an upstream provider (a Maps result, a
    raw JSON-RPC result). Described, exemplified, not constrained."""
    schema = {"description": description}
    if types:
        schema["type"] = types
    if example is not None:
        schema["examples"] = [example]
    return schema


# --- shared fragments -----------------------------------------------------------

_MODEL = _s("The model that produced the answer.", "gemini-2.5-flash")
_TOKENS = _obj({"prompt": _i("Prompt tokens billed by the provider.", 312),
                "output": _i("Output tokens billed by the provider.", 88)},
               ["prompt", "output"], "Token usage for the call.")

_CITED_SOURCE = _obj({"n": _i("Citation number used as [n] in the text.", 1),
                      "url": _s("Source URL.", "https://example.com/about"),
                      "title": _s("Page title, when the page had one.", "About Example", nullable=True)},
                     ["n", "url"], "One source the answer cites.")
_UNREAD_SOURCE = _obj({"url": _s("Source that could not be read.", "https://example.com/paywalled"),
                       "reason": _s("Why it could not be read.", "HTTP 403 from the target")},
                      ["url", "reason"], "A source that was found but not read; disclosed, never silently dropped.")

_BQ_COLUMNS = _arr(_s("Column name.", "name"), "Result column names, in order.", ["name", "n"])
_BQ_ROWS = _arr(_obj({}, [], "One row keyed by column name.", additionalProperties=True,
                     examples=[{"name": "James", "n": 4942431}]),
                "Result rows, keyed by column name.", [{"name": "James", "n": 4942431}])
_GIB = _n("Gibibytes BigQuery scanned; the metered cost basis.", 0.012)

_PROBABILITY = _obj({"outcome": _s("Outcome label.", "Yes"),
                     "probability_pct": _n("Implied probability in percent.", 62.5, nullable=True)},
                    ["outcome"], "One outcome and what the market prices it at.")
_MARKET = _obj({
    "id": _s("Polymarket market id.", "512345"),
    "question": _s("The market question.", "Will BTC close above $100k on Dec 31?"),
    "slug": _s("Polymarket slug.", "will-btc-close-above-100k"),
    "active": _b("Whether the market is active.", True),
    "closed": _b("Whether the market is closed.", False),
    "end_date": _s("ISO 8601 end date.", "2026-12-31T00:00:00Z", nullable=True),
    "volume": _n("Traded volume in USD.", 1834520.5, nullable=True),
    "liquidity": _n("Liquidity in USD.", 240310.2, nullable=True),
    "implied_probabilities": _arr(_PROBABILITY, "Each outcome with its implied probability.",
                                  [{"outcome": "Yes", "probability_pct": 62.5},
                                   {"outcome": "No", "probability_pct": 37.5}]),
}, ["id", "question", "implied_probabilities"], "One prediction market.")

_SPOT_QUOTE_PROPS = {
    "product_id": _s("Coinbase product, base-quote.", "BTC-USD"),
    "price": _s("Last trade price, as the exchange reports it.", "97231.45"),
    "price_change_24h_pct": _s("24-hour percentage change.", "1.82", nullable=True),
    "volume_24h": _s("24-hour volume in the base currency.", "14231.9", nullable=True),
    "base_currency": _s("Base currency code.", "BTC", nullable=True),
    "quote_currency": _s("Quote currency code.", "USD", nullable=True),
    "status": _s("Product status as reported by the exchange.", "online", nullable=True),
    "source": _const("coinbase-advanced-trade-public", "Data source."),
}
_SPOT_QUOTE = _obj(_SPOT_QUOTE_PROPS, ["product_id", "price", "source"], "Live spot quote.")

_PAGE_SOURCE = _obj({
    "text_chars": _i("Characters of readable text extracted.", 8421),
    "truncated": _b("Whether the text was cut at the extraction limit.", False),
    "javascript_rendered": _b("Whether a real browser rendered the page first.", True),
}, ["text_chars", "truncated", "javascript_rendered"], "How the page was read.")

_LINK = _obj({"text": _s("Anchor text.", "Pricing"),
              "href": _s("Absolute href.", "https://example.com/pricing")},
             ["href"], "One link on the page.")

_MCP_TOOL = _obj({"name": _s("Tool name.", "get_weather", nullable=True),
                  "description": _s("Tool description.", "Current weather for a city.", nullable=True),
                  "annotations": _free("The tool's annotations object, as served.",
                                       {"readOnlyHint": True}, ["object", "null"])},
                 ["name"], "One tool the inspected server lists.")

_VERDICT = _obj({
    "claim": _s("The claim, as given.", "HubVibe sells machine-payable site audits."),
    "verdict": _enum(["SUPPORTED", "CONTRADICTED", "UNSUPPORTED"],
                     "Whether the sources support, contradict, or do not address the claim.",
                     "SUPPORTED"),
    "quote": _s("The sentence the verdict rests on; null when UNSUPPORTED.",
                "Every check is priced per call over HTTP 402.", nullable=True),
    "source_n": _i("Which source (by n) the quote is from; null when UNSUPPORTED.", 1, nullable=True),
}, ["claim", "verdict"], "One claim and its verdict.")


_BOUNDS = _arr(_n("Bound.", 0.0), "Lower and upper bound at the confidence level.", [1.43, 2.53])
_T_TEST = _obj({
    "null_hypothesis": _s("What the test rejects.", "slope = 0 (no linear relationship)"),
    "t": _n("Student t statistic; null when undefined (a perfect fit).", 11.4, nullable=True),
    "p_value": _n("Two-sided p-value; null when undefined.", 0.0015, nullable=True),
    "significant_at_alpha": {"type": ["boolean", "null"], "description": "p_value < alpha.",
                             "examples": [True]},
}, ["null_hypothesis", "t", "p_value", "significant_at_alpha"], "One two-sided Student t test.")
_NORMALITY = {
    "type": ["object", "null"],
    "description": "Jarque-Bera normality test; null when the values are constant.",
    "properties": {
        "test": _const("jarque_bera", "The test."),
        "statistic": _n("Jarque-Bera statistic.", 0.42),
        "p_value": _n("Chi-square(2) p-value, exp(-JB/2).", 0.81),
        "null_hypothesis": _s("What the test rejects.", "the values are normally distributed"),
        "reject_at_alpha": _b("p_value < alpha: not normal at this alpha.", False),
    },
    "required": ["test", "statistic", "p_value", "null_hypothesis", "reject_at_alpha"],
}


def _nobj(properties: dict, required: list, description: str) -> dict:
    """An object that is null when the metric was not requested or is undefined."""
    return {"type": ["object", "null"], "properties": properties, "required": required,
            "description": description}


# --- one schema per worker, keyed by catalog name -------------------------------

OUTPUT_SCHEMAS = {
    "chain.network": _obj({
        "network": _const("base-mainnet", "Chain read."),
        "block_number": _i("Current head block number.", 41230577),
        "gas_price_wei": _s("Current gas price in wei, as a decimal string.", "12500000"),
        "gas_price_gwei": _n("Current gas price in gwei.", 0.0125),
    }, ["network", "block_number", "gas_price_wei", "gas_price_gwei"]),

    "chain.address": _obj({
        "address": _s("The address queried.", "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd"),
        "network": _const("base-mainnet", "Chain read."),
        "balance_wei": _s("ETH balance in wei, as a decimal string.", "1250000000000000000"),
        "balance_eth": _n("ETH balance.", 1.25),
        "transaction_count": _i("Nonce: transactions sent from this address.", 42),
        "is_contract": _b("Whether code is deployed at the address.", False),
        "code_size_bytes": _i("Deployed bytecode size; 0 for an externally owned account.", 0),
    }, ["address", "network", "balance_wei", "balance_eth", "transaction_count",
        "is_contract", "code_size_bytes"]),

    "chain.transaction": _obj({
        "hash": _s("Transaction hash.", "0x" + "ab" * 32),
        "network": _const("base-mainnet", "Chain read."),
        "from": _s("Sender.", "0x837C40E2B4e976f43Ffb4451eE281A00fA9477dd", nullable=True),
        "to": _s("Recipient; null for a contract creation.",
                 "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", nullable=True),
        "value_wei": _s("Value transferred in wei, as a decimal string.", "0"),
        "value_eth": _n("Value transferred in ETH.", 0.0),
        "block_number": _i("Block the transaction was mined in; null while pending.", 41230501, nullable=True),
        "mined": _b("Whether the transaction is in a block.", True),
        "status": _enum(["success", "failed", "pending"], "Receipt status.", "success"),
        "gas_used": _i("Gas used, from the receipt.", 65231),
        "log_count": _i("Number of logs emitted, from the receipt.", 2),
    }, ["hash", "network", "from", "to", "value_wei", "value_eth", "block_number", "mined", "status"]),

    "chain.rpc": _obj({
        "method": _s("The JSON-RPC method called.", "eth_blockNumber"),
        "endpoint": _s("The RPC endpoint that answered.", "https://mainnet.base.org"),
        "result": _free("The chain's `result`, verbatim, when the call succeeded.", "0x2751f11"),
        "error": _free("The chain's JSON-RPC error object, verbatim, when it answered with one "
                       "(a revert reason, an unknown block). Either `result` or `error` is present.",
                       {"code": -32000, "message": "execution reverted"}, ["object", "null"]),
    }, ["method", "endpoint"]),

    "market.quote": _SPOT_QUOTE,

    "market.prediction": _obj({
        "query": _s("The search query, or null for the top markets by volume.", "bitcoin", nullable=True),
        "markets": _arr(_MARKET, "Matching markets, highest volume first."),
        "count": _i("Number of markets returned.", 1),
        "source": _const("polymarket-gamma", "Data source."),
        "note": _s("What the numbers are and are not.",
                   "Implied probabilities are what the market is currently pricing, not a forecast by HubVibe."),
    }, ["query", "markets", "count", "source", "note"]),

    "market.rates": _obj({
        "currency": _s("Base currency of the table.", "USD"),
        "rates": _obj({}, [], "Exchange rate per currency code, as decimal strings.",
                      additionalProperties={"type": "string"},
                      examples=[{"EUR": "0.92", "GBP": "0.78", "BTC": "0.0000103"}]),
        "source": _const("coinbase-exchange-rates", "Data source."),
    }, ["currency", "rates", "source"]),

    "market.ticker": _obj({
        "product_id": _s("Coinbase product, base-quote.", "BTC-USD"),
        "price": _s("Last trade price.", "97231.45", nullable=True),
        "bid": _s("Best bid.", "97230.10", nullable=True),
        "ask": _s("Best ask.", "97232.80", nullable=True),
        "volume": _s("24-hour volume in the base currency.", "14231.9", nullable=True),
        "time": _s("Exchange timestamp, ISO 8601.", "2026-09-22T04:10:12.345Z", nullable=True),
        "source": _const("coinbase-exchange", "Data source."),
    }, ["product_id", "price", "bid", "ask", "source"]),

    "prediction.market": _obj({
        "slug": _s("The slug queried.", "will-btc-close-above-100k"),
        "market": _MARKET,
        "source": _const("polymarket-gamma", "Data source."),
    }, ["slug", "market", "source"]),

    "prediction.events": _obj({
        "events": _arr(_obj({
            "id": _s("Event id.", "24011"),
            "title": _s("Event title.", "Bitcoin price on December 31"),
            "slug": _s("Event slug.", "bitcoin-price-on-december-31"),
            "volume": _n("Traded volume in USD.", 5230100.0, nullable=True),
            "end_date": _s("ISO 8601 end date.", "2026-12-31T00:00:00Z", nullable=True),
            "market_count": _i("Markets grouped under this event.", 6),
        }, ["id", "title", "slug", "market_count"], "One event."),
            "Events, highest volume first."),
        "count": _i("Number of events returned.", 1),
        "source": _const("polymarket-gamma", "Data source."),
    }, ["events", "count", "source"]),

    "extract.page": _obj({
        "url": _s("The URL requested.", "https://example.com"),
        "final_url": _s("The URL after redirects.", "https://example.com/", nullable=True),
        "title": _s("Page title.", "Example Domain", nullable=True),
        "description": _s("Meta description.", "Example Domain for documentation.", nullable=True),
        "text": _s("Readable text of the page.", "Example Domain. This domain is for use in illustrative examples..."),
        "text_chars": _i("Characters in `text`.", 172),
        "truncated": _b("Whether the text was cut at the extraction limit.", False),
        "links": _arr(_LINK, "Up to 100 links on the page.",
                      [{"text": "More information...", "href": "https://www.iana.org/domains/example"}]),
        "javascript_rendered": _b("Whether a real browser rendered the page first.", True),
    }, ["url", "text", "text_chars", "truncated", "links", "javascript_rendered"]),

    "fetch.raw": _obj({
        "url": _s("The URL requested.", "https://example.com"),
        "final_url": _s("The URL after redirects.", "https://example.com/", nullable=True),
        "status": _i("HTTP status the target returned; any status is a completed fetch.", 200),
        "content_type": _s("Content-Type header.", "text/html; charset=UTF-8", nullable=True),
        "bytes": _i("Response body size in bytes.", 1256),
        "text": _s("Body text for textual content types; null for binary.",
                   "<!doctype html><html>...", nullable=True),
        "truncated": _b("Whether `text` was cut at the fetch limit.", False),
        "headers": _obj({}, [], "Response headers, lower-cased names.",
                        additionalProperties={"type": "string"},
                        examples=[{"content-type": "text/html; charset=UTF-8", "cache-control": "max-age=604800"}]),
    }, ["url", "status", "bytes", "truncated", "headers"]),

    "search.web": _obj({
        "query": _s("The query searched.", "x402 payment protocol"),
        "answer": _s("Grounded answer with the sources it drew on.",
                     "x402 is an HTTP-native payment protocol that uses the 402 status code..."),
        "sources": _arr(_obj({"url": _s("Source URL.", "https://www.x402.org/"),
                              "title": _s("Source title.", "x402", nullable=True)}, ["url"]),
                        "Web sources the answer is grounded in."),
        "search_queries_used": _arr(_s("A query the grounding step issued.", "x402 protocol"),
                                    "Search queries the grounding step actually ran.", ["x402 protocol"]),
        "model": _MODEL,
    }, ["query", "answer", "sources", "search_queries_used", "model"]),

    "llm.analyze": _obj({
        "question": _s("The question asked of the material.", "What does it sell, and at what price?"),
        "answer": _s("The answer, from the material only; says so when the material does not contain it.",
                     "It sells machine-payable site audits at $0.05 per call."),
        "model": _MODEL,
        "input_chars": _i("Characters of material analysed.", 61),
        "tokens": _TOKENS,
    }, ["question", "answer", "model", "input_chars", "tokens"]),

    "llm.extract": _obj({
        "fields": _obj({}, [], "Exactly the requested field names; null where the material does not state a value.",
                       additionalProperties=True,
                       examples=[{"title": "HubVibe", "pricing": "$0.05 per call"}]),
        "model": _MODEL,
        "tokens": _TOKENS,
    }, ["fields", "model", "tokens"]),

    "llm.generate": _obj({
        "text": _s("The completion.", "Here is a short poem about bees..."),
        "model": _s("The model used.", "gemini-2.5-flash"),
        "provider": _s("The provider that served it.", "gemini"),
        "finish_reason": _s("Why generation stopped.", "STOP", nullable=True),
        "usage": _obj({"input_tokens": _i("Input tokens.", 14), "output_tokens": _i("Output tokens.", 96)},
                      ["input_tokens", "output_tokens"], "Token usage."),
    }, ["text", "model", "provider", "usage"]),

    "code.execute": _obj({
        "code": _s("The code that ran.", "print(sum(range(10)))"),
        "output": _s("Captured stdout/stderr.", "45\n"),
        "outcome": _s("Sandbox outcome code.", "OUTCOME_OK"),
        "summary": _s("The model's one-line summary of the run.", "Printed the sum 45.", nullable=True),
        "model": _MODEL,
    }, ["code", "output", "outcome", "model"]),

    "image.generate": _obj({
        "prompt": _s("The prompt.", "A beehive built from circuit boards, isometric illustration"),
        "aspect_ratio": _s("Aspect ratio generated.", "1:1"),
        "image_base64": _s("The image, base64.", "iVBORw0KGgo..."),
        "mime_type": _s("Image MIME type.", "image/png"),
        "model": _s("Image model.", "imagen-4.0-generate-001"),
    }, ["prompt", "aspect_ratio", "image_base64", "mime_type", "model"]),

    "speech.synthesize": _obj({
        "text_chars": _i("Characters synthesised.", 58),
        "voice": _s("Voice used.", "en-US-Neural2-C"),
        "audio_base64": _s("The audio, base64.", "UklGRi4AAABXQVZF..."),
        "mime_type": _s("Audio MIME type.", "audio/mpeg"),
    }, ["text_chars", "voice", "audio_base64", "mime_type"]),

    "speech.transcribe": _obj({
        "transcript": _s("The transcript.", "hello world"),
        "language_code": _s("Language recognised.", "en-US"),
        "confidence": _n("Mean recognition confidence, 0-1; null when the engine gives none.", 0.94, nullable=True),
        "model": _s("Recognition model.", "latest_short"),
    }, ["transcript", "language_code", "model"]),

    "video.generate": _obj({
        "prompt": _s("The prompt.", "A single bee landing on a circuit-board flower, slow motion"),
        "aspect_ratio": _s("Aspect ratio generated.", "16:9"),
        "duration_seconds": _i("Clip length in seconds.", 4),
        "video_base64": _s("The video, base64, when returned inline.", "AAAAIGZ0eXBpc29t...", nullable=True),
        "gcs_uri": _s("Cloud Storage URI, when the provider stored it instead.", None, nullable=True),
        "mime_type": _s("Video MIME type.", "video/mp4"),
        "model": _s("Video model.", "veo-3.0-generate-001"),
    }, ["prompt", "aspect_ratio", "duration_seconds", "mime_type", "model"]),

    "stats.probability": _obj({
        "source": _obj({
            "type": _enum(["points", "bigquery"], "Where the points came from.", "points"),
            "table": _s("BigQuery table read, when source is bigquery.", None, nullable=True),
            "x_column": _s("Column used for x, when source is bigquery.", None, nullable=True),
            "y_column": _s("Column used for y, when source is bigquery.", None, nullable=True),
            "sql": _s("The read-only SQL that ran, when source is bigquery.", None, nullable=True),
            "rows_available": _i("Rows with finite x and y in the table; null for inline points.",
                                 None, nullable=True),
            "rows_used": _i("Points the statistics were computed from.", 5),
            "sampled": _b("True when the table had more usable rows than were read.", False),
            "gib_processed": _n("Gibibytes BigQuery scanned; null for inline points.", None,
                                nullable=True),
        }, ["type", "table", "x_column", "y_column", "sql", "rows_available", "rows_used",
            "sampled", "gib_processed"], "Where the data came from and how much of it was used."),
        "n": _i("Number of points.", 5),
        "alpha": _n("Significance level used.", 0.05),
        "confidence_level": _n("1 - alpha: the level of every interval.", 0.95),
        "metrics": _arr(_enum(["linear_regression", "normal_distribution", "p_values", "prediction"],
                              "Metric name.", "linear_regression"),
                        "Metrics computed, in canonical order.",
                        ["linear_regression", "normal_distribution", "p_values", "prediction"]),
        "linear_regression": _nobj({
            "slope": _n("OLS slope.", 1.98),
            "intercept": _n("OLS intercept.", 0.08),
            "r": _n("Pearson correlation; null when y is constant.", 0.998, nullable=True),
            "r_squared": _n("Coefficient of determination; null when y is constant.", 0.996, nullable=True),
            "adjusted_r_squared": _n("R^2 adjusted for degrees of freedom.", 0.995, nullable=True),
            "slope_std_error": _n("Standard error of the slope.", 0.071),
            "intercept_std_error": _n("Standard error of the intercept.", 0.236),
            "residual_std_error": _n("Standard error of the residuals, sqrt(SSE / df).", 0.225),
            "degrees_of_freedom": _i("n - 2.", 3),
            "slope_t": _n("t statistic of the slope; null when undefined.", 27.8, nullable=True),
            "intercept_t": _n("t statistic of the intercept; null when undefined.", 0.34, nullable=True),
            "t_critical": _n("Two-sided t critical value at alpha with df degrees of freedom.", 3.18),
            "slope_ci": _BOUNDS,
            "intercept_ci": _BOUNDS,
            "f_statistic": _n("F statistic of the regression (t^2); null when undefined.", 773.0,
                              nullable=True),
            "sse": _n("Sum of squared residuals.", 0.152),
            "sst": _n("Total sum of squares of y.", 39.4),
            "x_mean": _n("Mean of x.", 3.0),
            "y_mean": _n("Mean of y.", 6.02),
        }, ["slope", "intercept", "r", "r_squared", "adjusted_r_squared", "slope_std_error",
            "intercept_std_error", "residual_std_error", "degrees_of_freedom", "slope_t",
            "intercept_t", "t_critical", "slope_ci", "intercept_ci", "f_statistic", "sse", "sst",
            "x_mean", "y_mean"], "Ordinary least squares fit of y on x; null when not requested."),
        "normal_distribution": _nobj({
            "of": _enum(["y", "x", "residuals"], "Which values the model fits.", "y"),
            "mean": _n("Sample mean.", 6.02),
            "std_dev": _n("Sample standard deviation (n - 1).", 3.14),
            "variance": _n("Sample variance (n - 1).", 9.86),
            "min": _n("Smallest value.", 2.1),
            "max": _n("Largest value.", 10.1),
            "median": _n("Median of the values.", 6.2),
            "skewness": _n("Sample skewness; null when the values are constant.", 0.05, nullable=True),
            "excess_kurtosis": _n("Excess kurtosis; null when the values are constant.", -1.3,
                                  nullable=True),
            "quantiles": {"type": ["array", "null"],
                          "description": "Quantiles of the fitted normal; null when degenerate.",
                          "items": _obj({"p": _n("Probability.", 0.95),
                                         "value": _n("Value at that quantile.", 11.19)},
                                        ["p", "value"])},
            "probabilities": _arr(_obj({"query": _free("The query as sent.", {"below": 8.0}, ["object"]),
                                        "probability": _n("Probability under the fitted normal.", 0.736)},
                                       ["query", "probability"]),
                                  "Answers to probability_queries, in order.",
                                  [{"query": {"below": 8.0}, "probability": 0.736}]),
            "normality": _NORMALITY,
        }, ["of", "mean", "std_dev", "variance", "min", "max", "median", "skewness",
            "excess_kurtosis", "quantiles", "probabilities", "normality"],
            "Normal model of the chosen values; null when not requested."),
        "p_values": _nobj({
            "slope": _T_TEST,
            "intercept": _T_TEST,
            "normality_of_residuals": _NORMALITY,
        }, ["slope", "intercept", "normality_of_residuals"],
            "Hypothesis tests validated at alpha; null when not requested."),
        "prediction": {"type": ["array", "null"],
                       "description": "One entry per predict_x; null when not requested.",
                       "items": _obj({"x": _n("The x asked for.", 6.0),
                                      "y_hat": _n("Predicted y.", 11.96),
                                      "mean_ci": _BOUNDS,
                                      "prediction_interval": _BOUNDS},
                                     ["x", "y_hat", "mean_ci", "prediction_interval"])},
        "notes": _arr(_s("A caveat about a degenerate input.", "The points lie exactly on a line."),
                      "Caveats about the input; empty when there are none.", []),
        "method": _s("How the numbers were produced.",
                     "Ordinary least squares (closed form). Student t p-values from the "
                     "regularised incomplete beta function..."),
    }, ["source", "n", "alpha", "confidence_level", "metrics", "linear_regression",
        "normal_distribution", "p_values", "prediction", "notes", "method"]),

    "data.query": _obj({
        "sql": _s("The SQL that ran.", "SELECT name, SUM(number) AS n FROM `bigquery-public-data.usa_names.usa_1910_2013` GROUP BY name ORDER BY n DESC LIMIT 5"),
        "columns": _BQ_COLUMNS,
        "rows": _BQ_ROWS,
        "row_count": _i("Rows returned (capped).", 5),
        "total_rows": _i("Rows the query produced before the cap.", 5),
        "truncated": _b("Whether rows were cut at the cap.", False),
        "gib_processed": _GIB,
        "cache_hit": _b("Whether BigQuery served it from cache (no bytes billed).", False),
    }, ["sql", "columns", "rows", "row_count", "total_rows", "truncated", "gib_processed", "cache_hit"]),

    "data.question": _obj({
        "question": _s("The question asked.", "Which five names were given most often?"),
        "table": _s("The table queried.", "bigquery-public-data.usa_names.usa_1910_2013"),
        "answer": _s("The answer, read from the rows; states the figures.",
                     "James (4,942,431), John (4,834,422), ..."),
        "sql": _s("The SQL the model wrote and that ran.", "SELECT name, SUM(number) AS n FROM ... LIMIT 5"),
        "columns": _BQ_COLUMNS,
        "rows": _BQ_ROWS,
        "row_count": _i("Rows returned.", 5),
        "gib_processed": _GIB,
        "model": _MODEL,
    }, ["question", "table", "answer", "sql", "columns", "rows", "row_count", "gib_processed", "model"]),

    "data.forecast": _obj({
        "table": _s("Source table.", "bigquery-public-data.covid19_nyt.us_states"),
        "timestamp_col": _s("Timestamp column.", "date"),
        "data_col": _s("Value column forecast.", "confirmed_cases"),
        "horizon": _i("Forecast points requested.", 10),
        "columns": _arr(_s("AI.FORECAST output column.", "forecast_timestamp"),
                        "AI.FORECAST output columns.",
                        ["state_name", "forecast_timestamp", "forecast_value",
                         "confidence_level", "prediction_interval_lower_bound",
                         "prediction_interval_upper_bound", "ai_forecast_status"]),
        "rows": _arr(_obj({}, [], "One forecast point.", additionalProperties=True,
                          examples=[{"state_name": "Texas", "forecast_timestamp": "2023-03-24 00:00:00",
                                     "forecast_value": "8631745.2", "confidence_level": "0.95"}]),
                     "Forecast rows.",
                     [{"state_name": "Texas", "forecast_timestamp": "2023-03-24 00:00:00",
                       "forecast_value": "8631745.2", "confidence_level": "0.95"}]),
        "row_count": _i("Rows returned.", 10),
        "gib_processed": _GIB,
    }, ["table", "timestamp_col", "data_col", "horizon", "columns", "rows", "row_count", "gib_processed"]),

    "data.anomalies": _obj({
        "history_table": _s("Table the forecast is fitted on.", "bigquery-public-data.covid19_nyt.us_states"),
        "target_table": _s("Table checked for anomalies.", "bigquery-public-data.covid19_nyt.us_states"),
        "timestamp_col": _s("Timestamp column.", "date"),
        "data_col": _s("Value column.", "confirmed_cases"),
        "anomaly_prob_threshold": _n("Probability threshold used.", 0.95),
        "columns": _arr(_s("AI.DETECT_ANOMALIES output column.", "is_anomaly"),
                        "AI.DETECT_ANOMALIES output columns.",
                        ["state_name", "date", "confirmed_cases", "is_anomaly",
                         "lower_bound", "upper_bound", "anomaly_probability"]),
        "rows": _arr(_obj({}, [], "One checked point.", additionalProperties=True,
                          examples=[{"state_name": "Texas", "date": "2023-03-20", "confirmed_cases": "8631000",
                                     "is_anomaly": "false", "anomaly_probability": "0.12"}]),
                     "Checked rows with their anomaly flag.",
                     [{"state_name": "Texas", "date": "2023-03-20", "confirmed_cases": "8631000",
                       "is_anomaly": "false", "anomaly_probability": "0.12"}]),
        "row_count": _i("Rows returned.", 10),
        "gib_processed": _GIB,
    }, ["history_table", "target_table", "timestamp_col", "data_col", "anomaly_prob_threshold",
        "columns", "rows", "row_count", "gib_processed"]),

    "monitor.snapshot": _obj({
        "url": _s("The URL baselined.", "https://example.com"),
        "title": _s("Page title at snapshot time.", "Example Domain", nullable=True),
        "text_chars": _i("Characters of readable text captured.", 172),
        "content_hash": _s("sha256 of the readable text, hex.", "a" * 64),
        "note": _s("What to do next.", "Baseline saved. Call monitor.check on this URL later to see what changed."),
    }, ["url", "text_chars", "content_hash", "note"]),

    "monitor.check": _obj({
        "url": _s("The URL checked.", "https://example.com"),
        "title": _s("Page title now.", "Example Domain", nullable=True),
        "changed": _b("Whether the readable text differs from the baseline.", True),
        "baseline_age_seconds": _n("Seconds since the baseline was taken.", 86400),
        "change_summary": _s("What changed, in words; or that nothing did.",
                             "The pricing section now lists a $0.15 bundle; the headline is unchanged."),
        "model": _s("Model that summarised the diff; present only when something changed.", "gemini-2.5-flash"),
    }, ["url", "changed", "baseline_age_seconds", "change_summary"]),

    "security.mcp_inspect": _obj({
        "url": _s("The MCP endpoint inspected.", "https://mcp.example.com/mcp"),
        "reachable": _b("Whether the server answered the MCP initialize handshake.", True),
        "requires_auth": _b("Whether an unauthenticated initialize was refused.", False),
        "initialize_status": _i("HTTP status of the initialize call.", 200),
        "protocol_version": _s("MCP protocol version the server negotiated.", "2025-06-18", nullable=True),
        "server_name": _s("serverInfo.name.", "example-mcp", nullable=True),
        "server_version": _s("serverInfo.version.", "1.0.0", nullable=True),
        "tool_count": _i("Tools listed.", 3),
        "tools": _arr(_MCP_TOOL, "Every tool listed, with its annotations."),
        "tools_without_readonly_annotation": _arr(
            _s("A tool name.", "delete_records"),
            "Tools that do not declare readOnlyHint: the ones an agent should treat as able to change state.",
            ["delete_records"]),
        "tools_error": _s("Why tools/list failed, when it did.", None, nullable=True),
    }, ["url", "reachable", "requires_auth", "initialize_status", "tool_count", "tools",
        "tools_without_readonly_annotation"]),

    "maps.places": _obj({
        "query": _s("The place query.", "coffee near Union Square, San Francisco"),
        "result": _free("Places found, in Google Maps Grounding Lite's own response shape.",
                        {"places": [{"name": "Blue Bottle Coffee", "formattedAddress": "66 Mint St, San Francisco, CA"}]},
                        "object"),
    }, ["query", "result"]),

    "maps.route": _obj({
        "origin": _s("Origin as given.", "San Francisco, CA"),
        "destination": _s("Destination as given.", "Oakland, CA"),
        "travel_mode": _enum(["DRIVE", "WALK", "BICYCLE", "TRANSIT"], "Travel mode used.", "DRIVE"),
        "result": _free("The route, in Google Maps Grounding Lite's own response shape.",
                        {"routes": [{"distanceMeters": 19870, "duration": "1362s"}]}, "object"),
    }, ["origin", "destination", "travel_mode", "result"]),

    "maps.weather": _obj({
        "location": _s("Location as given.", "San Francisco, CA"),
        "result": _free("Current conditions, in Google Maps Grounding Lite's own response shape.",
                        {"temperature": {"degrees": 17.2, "unit": "CELSIUS"}, "weatherCondition": {"type": "PARTLY_CLOUDY"}},
                        "object"),
    }, ["location", "result"]),

    "research.brief": _obj({
        "url": _s("The page read.", "https://example.com"),
        "final_url": _s("The URL after redirects.", "https://example.com/", nullable=True),
        "title": _s("Page title.", "Example Domain", nullable=True),
        "question": _s("The question answered.", "What does this page offer, who is it for, and what is the pricing?"),
        "brief": _s("The answer, from the page only.", "The page is a placeholder domain reserved for documentation examples..."),
        "source": _PAGE_SOURCE,
        "model": _MODEL,
    }, ["url", "question", "brief", "source", "model"]),

    "research.page_facts": _obj({
        "url": _s("The page read.", "https://example.com"),
        "final_url": _s("The URL after redirects.", "https://example.com/", nullable=True),
        "title": _s("Page title.", "Example Domain", nullable=True),
        "fields": _obj({}, [], "Exactly the requested field names; null where the page does not state a value.",
                       additionalProperties=True,
                       examples=[{"title": "Example Domain", "pricing": None}]),
        "model": _MODEL,
    }, ["url", "fields", "model"]),

    "market.intel": _obj({
        "product_id": _s("Spot product read.", "BTC-USD"),
        "spot": _SPOT_QUOTE,
        "prediction_markets": _arr(_MARKET, "Matching prediction markets, highest volume first."),
        "analysis": _s("What the two sources together do and do not support.",
                       "Spot is up 1.8% on the day while the top market prices a year-end close above $100k at 62%..."),
        "model": _MODEL,
        "disclaimer": _s("Not investment advice.",
                         "Market data and implied probabilities only. Not investment advice and not a forecast by HubVibe."),
    }, ["product_id", "spot", "prediction_markets", "analysis", "model", "disclaimer"]),

    "research.web": _obj({
        "question": _s("The question researched.", "What is the x402 payment protocol?"),
        "answer": _s("Cited answer; every claim carries [n].",
                     "x402 is an HTTP-native payment standard built on the 402 status code [1]..."),
        "sources": _arr(_CITED_SOURCE, "Sources read, numbered as cited."),
        "partial": _arr(_UNREAD_SOURCE, "Sources found but not read, with the reason.", []),
        "model": _MODEL,
    }, ["question", "answer", "sources", "partial", "model"]),

    "research.company": _obj({
        "company": _s("The company researched.", "Anthropic"),
        "report": _s("Cited brief: what it does, products, notable facts; thin or conflicting evidence is called out.",
                     "Anthropic is an AI safety company that builds the Claude model family [1]..."),
        "sources": _arr(_CITED_SOURCE, "Sources read, numbered as cited."),
        "partial": _arr(_UNREAD_SOURCE, "Sources found but not read, with the reason.", []),
        "model": _MODEL,
    }, ["company", "report", "sources", "partial", "model"]),

    "verify.claims": _obj({
        "claims": _arr(_s("A claim, as given.", "HubVibe sells machine-payable site audits."),
                       "The claims checked, in order.", ["HubVibe sells machine-payable site audits."]),
        "verdicts": _arr(_VERDICT, "One verdict per claim, same order."),
        "sources_read": _arr(_CITED_SOURCE, "Sources read, numbered as cited in source_n."),
        "sources_unread": _arr(_UNREAD_SOURCE, "Sources that could not be read, with the reason.", []),
        "model": _MODEL,
    }, ["claims", "verdicts", "sources_read", "sources_unread", "model"]),
}


# --- the envelope every /work 200 is wrapped in (app/workers/router.py) ---------

_PROVENANCE = _obj({
    "steps": _arr(_obj({
        "step": _s("Step name inside the worker.", "quote"),
        "ok": _b("Whether the step succeeded.", True),
        "provider": _s("Provider that served the step.", "coinbase-advanced-trade-public"),
        "ms": _i("Milliseconds the step took.", 312),
        "reason": _s("Failure reason, on a failed step.", "provider_timeout"),
    }, ["step", "ok", "ms"], "One step of the job."), "Every step the job ran, in order."),
    "providers_used": _arr(_s("Provider name.", "coinbase-advanced-trade-public"),
                           "Providers that served the job.", ["coinbase-advanced-trade-public"]),
    "attempts": _i("Provider attempts made, including retries and failovers.", 1),
    "elapsed_ms": _i("Wall-clock milliseconds for the whole job.", 340),
}, ["steps", "providers_used", "attempts", "elapsed_ms"],
    "How the result was produced: which providers ran and how long each took.")

RESPONSE_ENVELOPE = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "description": (
        "The 200 body of every paid /work call. `result` is the worker's own "
        "output (its schema is per route); everything else is the same on all "
        "38 routes. A receipt for the job is at `receipt_url`."),
    "properties": {
        "status": _const("ok", "Present only on a delivered result."),
        "worker": _s("Catalog name of the worker that ran.", "market.quote"),
        "price_usd": _n("What this call cost, in USD.", 0.02),
        "result": {"type": "object", "description": "The worker's output; see the route's own schema."},
        "provenance": _PROVENANCE,
        "receipt_id": _s("Receipt id for this job.", "rcpt_9f1c2b3a4d5e6f70"),
        "receipt_url": _s("Where to fetch the machine-readable receipt (free, no payment).",
                          "/work/receipts/rcpt_9f1c2b3a4d5e6f70"),
        "billing_warning": _s("Present only when the charge was recorded with a caveat.",
                              "settlement pending"),
    },
    "required": ["status", "worker", "price_usd", "result", "provenance", "receipt_id", "receipt_url"],
}


def response_schema(worker) -> dict:
    """The full 200 schema of one route: the envelope with this worker's
    result schema in place of the generic `result`."""
    schema = {k: v for k, v in RESPONSE_ENVELOPE.items() if k != "properties"}
    schema["properties"] = {**RESPONSE_ENVELOPE["properties"], "result": worker.output_schema}
    schema["title"] = f"{worker.name} response"
    return schema


# --- examples generated FROM the schemas, so they cannot drift ----------------

_FORMAT_EXAMPLES = {
    "uri": "https://example.com",
    "url": "https://example.com",
    "uri-reference": "https://example.com",
    "iri": "https://example.com",
    "email": "agent@example.com",
    "date": "2026-01-01",
    "date-time": "2026-01-01T00:00:00Z",
    "time": "00:00:00Z",
    "hostname": "example.com",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
    "uuid": "00000000-0000-4000-8000-000000000000",
}


def example_from_schema(schema: dict) -> Any:
    """A value that satisfies `schema`, preferring the `examples` it carries."""
    if "examples" in schema and schema["examples"]:
        return schema["examples"][0]
    if "const" in schema:
        return schema["const"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((t for t in kind if t != "null"), kind[0] if kind else None)
    if kind == "object" or (kind is None and "properties" in schema):
        return {name: example_from_schema(prop)
                for name, prop in (schema.get("properties") or {}).items()}
    if kind == "array":
        items = schema.get("items") or {}
        return [example_from_schema(items)] if items else []
    if kind == "string":
        # A strict validator checks `format`: the word "example" in a field
        # declared a URI made the audit records invalid to Coinbase's index,
        # which kept the old rows for /audit/wcag and /audit/bundle.
        return _FORMAT_EXAMPLES.get(schema.get("format"), "example")
    if kind == "integer":
        return 0
    if kind == "number":
        return 0.0
    if kind == "boolean":
        return True
    return None


def output_example(worker) -> dict:
    """An example `result` for this worker, generated from its schema."""
    return example_from_schema(worker.output_schema)


def response_example(worker) -> dict:
    """An example 200 body for this worker: the envelope around its result."""
    envelope = example_from_schema(RESPONSE_ENVELOPE)
    envelope.pop("billing_warning", None)  # only present on a caveated charge
    envelope["worker"] = worker.name
    envelope["price_usd"] = worker.price_usd
    envelope["result"] = output_example(worker)
    return envelope


# --- representative queries for /.well-known/ard.json ----------------------------
# 2-5 per worker, per the ARD spec. Phrased as the ask an agent would search
# for, not as our own route names.

REPRESENTATIVE_QUERIES = {
    "chain.network": [
        "what is the current block number on Base",
        "current gas price on Base mainnet",
        "is the Base chain live right now",
    ],
    "chain.address": [
        "ETH balance and transaction count of a Base address",
        "is this Base address a contract or a wallet",
        "look up an address on Base mainnet",
    ],
    "chain.transaction": [
        "did this Base transaction succeed",
        "look up a transaction hash on Base with its receipt",
        "how much gas did a Base transaction use",
    ],
    "chain.rpc": [
        "call eth_getLogs on Base for a contract",
        "raw JSON-RPC read against Base mainnet",
        "eth_call a view function on Base",
    ],
    "market.quote": [
        "current BTC-USD price on Coinbase",
        "24 hour price change and volume for ETH-USD",
        "spot price of a crypto pair",
    ],
    "market.prediction": [
        "what odds do prediction markets give on an event",
        "top Polymarket markets by volume",
        "implied probability of an outcome on Polymarket",
    ],
    "market.rates": [
        "exchange rates for USD against every currency",
        "how much is one EUR in BTC",
        "current fiat and crypto exchange rate table",
    ],
    "market.ticker": [
        "best bid and ask for BTC-USD",
        "live order book top of book for a Coinbase product",
        "bid ask spread on ETH-USD",
    ],
    "prediction.market": [
        "look up a Polymarket market by slug",
        "current probabilities for a specific prediction market",
    ],
    "prediction.events": [
        "list the biggest prediction market events right now",
        "which Polymarket events have the most volume",
    ],
    "extract.page": [
        "extract the readable text and links from a web page",
        "read a JavaScript-rendered page and return its content",
        "get the title, description and text of a URL",
    ],
    "fetch.raw": [
        "fetch a URL and return the raw status, headers and body",
        "what HTTP status and headers does this URL return",
        "download the raw HTML of a page without rendering",
    ],
    "search.web": [
        "search the web and answer with sources",
        "live web search with citations for a question",
        "find recent web results about a topic",
    ],
    "llm.analyze": [
        "answer a question about this text using only the text",
        "summarize the key points of a document",
        "analyze provided material without guessing beyond it",
    ],
    "llm.extract": [
        "extract named fields from text as JSON",
        "pull structured data out of unstructured text",
        "turn a document into a JSON object with these keys",
    ],
    "llm.generate": [
        "generate text from a prompt with a chosen model",
        "raw LLM completion with a system prompt",
        "run a prompt on Gemini or Claude and return the text",
    ],
    "code.execute": [
        "run this Python code and return the output",
        "execute a script in a sandbox",
        "compute a result by running code",
    ],
    "image.generate": [
        "generate an image from a text prompt",
        "create an illustration with Imagen",
        "text to image at a given aspect ratio",
    ],
    "speech.synthesize": [
        "convert text to speech audio",
        "generate an MP3 voice-over from text",
        "text to speech with a named voice",
    ],
    "speech.transcribe": [
        "transcribe a short audio clip to text",
        "speech to text for a base64 WAV file",
        "what is said in this audio",
    ],
    "video.generate": [
        "generate a short video clip from a text prompt",
        "text to video with Veo",
        "create a four second video from a description",
    ],
    "stats.probability": [
        "linear regression with p-values on a list of x y points",
        "fit a normal distribution and get the probability a value falls below a threshold",
        "is the correlation between x and y statistically significant at alpha 0.05",
        "predict y at a new x with a prediction interval",
        "regression statistics on two numeric columns of a BigQuery table",
    ],
    "data.query": [
        "run read-only SQL against a BigQuery public dataset",
        "query a BigQuery table and return rows",
        "execute a SELECT on BigQuery with a scan limit",
    ],
    "data.question": [
        "answer a question about a BigQuery table in plain language",
        "natural language to SQL against a dataset and explain the result",
        "ask a data question and get the figures with the SQL that produced them",
    ],
    "data.forecast": [
        "forecast a time series stored in BigQuery",
        "predict the next values of a daily metric with TimesFM",
        "time series forecast from a table with a date and value column",
    ],
    "data.anomalies": [
        "detect anomalies in a time series in BigQuery",
        "flag unusual values in a daily metric against its history",
        "anomaly detection on a table with a timestamp column",
    ],
    "monitor.snapshot": [
        "save a baseline of a web page to detect changes later",
        "start monitoring a URL for content changes",
    ],
    "monitor.check": [
        "has this web page changed since the last check",
        "summarize what changed on a monitored page",
        "diff a URL against its saved baseline",
    ],
    "security.mcp_inspect": [
        "inspect an MCP server for auth and which tools can change state",
        "audit a remote MCP endpoint's tools and annotations",
        "does this MCP server require authentication",
    ],
    "maps.places": [
        "find places matching a query near a location",
        "search for restaurants or shops in a city",
        "look up a business on Google Maps",
    ],
    "maps.route": [
        "driving route and duration between two addresses",
        "how long does it take to walk from A to B",
        "transit directions between two places",
    ],
    "maps.weather": [
        "current weather at a location",
        "what is the temperature in a city right now",
    ],
    "research.brief": [
        "read a web page and write a brief answering a question about it",
        "what does this website offer, who is it for and what does it cost",
        "summarize a URL with the source disclosed",
    ],
    "research.page_facts": [
        "extract specific facts from a web page as JSON fields",
        "get the pricing, contact and product fields from a URL",
        "structured facts about a company from its website",
    ],
    "market.intel": [
        "what do spot and prediction markets together say about bitcoin",
        "market sentiment read combining Coinbase price and Polymarket odds",
        "reconcile a crypto price with prediction market probabilities",
    ],
    "research.web": [
        "research a question on the live web with numbered citations",
        "cited answer from several web sources",
        "find and read sources to answer a research question",
    ],
    "research.company": [
        "research a company from live web sources with citations",
        "what does this company do, what does it sell, anything notable",
        "company due diligence brief with sources",
    ],
    "verify.claims": [
        "fact-check these claims against these source URLs",
        "does this source support or contradict a statement",
        "verify claims with the quote each verdict rests on",
    ],
}
