"""stats.probability -- the predictive probability engine.

Ordinary least squares, a fitted normal distribution and exact p-values over
coordinate points, either sent inline or read from a BigQuery table. Pure
arithmetic: no model, no sampling randomness, no LLM anywhere in the path, so
the same points always produce the same numbers.

HOW "THE SAME NUMBERS" IS GUARANTEED

Every sum goes through math.fsum, which is exactly rounded -- its result does
not depend on the order the terms arrive in. So the payload is a function of
the multiset of points alone, not of how BigQuery happened to page the rows
back. A table larger than `max_rows` is reduced by a fingerprint ordering
(FARM_FINGERPRINT of the pair), not by LIMIT alone, so the rows chosen are
also a function of the data, and the payload says when that happened.

THE DISTRIBUTIONS, WITHOUT SCIPY

Student's t tail probabilities come from the regularised incomplete beta
function (continued fraction, modified Lentz), which is how every statistics
library computes them; the critical value for an interval is found by
bisection on that same function. The Jarque-Bera statistic is chi-square
with 2 degrees of freedom, whose survival function is exactly exp(-x/2).
The normal model uses the standard library's statistics.NormalDist. Every
one of these is checked against published reference values in
tests/test_workers_stats.py.
"""

import asyncio
import math
import re
from statistics import NormalDist

from .. import runtime
from ..providers import bigquery

MAX_POINTS = 100_000
DEFAULT_TABLE_ROWS = 10_000
MAX_PREDICT = 100
MAX_QUERIES = 50

METRICS = ("linear_regression", "normal_distribution", "p_values", "prediction")
DISTRIBUTION_OF = ("y", "x", "residuals")
QUANTILES = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)

_TABLE = re.compile(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_]+\.[A-Za-z0-9_]+$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

METHOD = (
    "Ordinary least squares (closed form). Student t p-values from the "
    "regularised incomplete beta function; interval critical values by "
    "bisection on it. Normal model: sample mean and standard deviation "
    "(n-1). Normality: Jarque-Bera, chi-square(2) p = exp(-JB/2). Every sum "
    "is exactly rounded (math.fsum), so identical points always give "
    "identical output. No model, no sampling randomness, no LLM.")


# --- input -------------------------------------------------------------------

def _number(value, where: str) -> float:
    # bool is an int in Python; `true` is not a coordinate.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise runtime.InvalidRequest(f"{where} must be a number.")
    number = float(value)
    if not math.isfinite(number):
        raise runtime.InvalidRequest(f"{where} must be finite.")
    return number


def _points(raw) -> tuple:
    if not isinstance(raw, list) or not raw:
        raise runtime.InvalidRequest("`points` must be a non-empty array of [x, y] pairs.")
    if len(raw) > MAX_POINTS:
        raise runtime.InvalidRequest(
            f"`points` has {len(raw)} entries; the limit is {MAX_POINTS}.")
    xs, ys = [], []
    for i, point in enumerate(raw):
        if isinstance(point, dict):
            x, y = point.get("x"), point.get("y")
        elif isinstance(point, list) and len(point) == 2:
            x, y = point
        else:
            raise runtime.InvalidRequest(
                f"points[{i}] must be [x, y] or {{\"x\": ..., \"y\": ...}}.")
        xs.append(_number(x, f"points[{i}] x"))
        ys.append(_number(y, f"points[{i}] y"))
    return xs, ys


def parse(payload: dict) -> dict:
    """Validate the whole request without touching data or payment.

    Raises InvalidRequest for anything the caller must fix, so the router can
    refuse it before a payment is read (see skills.PRECHECKS).
    """
    has_points = payload.get("points") is not None
    has_table = payload.get("table") is not None
    if has_points == has_table:
        raise runtime.InvalidRequest(
            "Send exactly one source: `points` (an array of [x, y] pairs) or "
            "`table` with `x_column` and `y_column`.")

    request = {"points": None, "table": None}
    if has_points:
        for key in ("x_column", "y_column", "max_rows"):
            if payload.get(key) is not None:
                raise runtime.InvalidRequest(f"`{key}` applies only to `table`.")
        request["points"] = _points(payload["points"])
    else:
        table = payload.get("table")
        if not isinstance(table, str) or not _TABLE.match(table.strip()):
            raise runtime.InvalidRequest(
                "`table` must be a fully qualified BigQuery table: project.dataset.table "
                "(for example bigquery-public-data.samples.natality).")
        columns = []
        for key in ("x_column", "y_column"):
            name = payload.get(key)
            if not isinstance(name, str) or not _IDENT.match(name):
                raise runtime.InvalidRequest(
                    f"`{key}` is required with `table` and must be a plain column name.")
            columns.append(name)
        max_rows = payload.get("max_rows", DEFAULT_TABLE_ROWS)
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) \
                or not 3 <= max_rows <= MAX_POINTS:
            raise runtime.InvalidRequest(
                f"`max_rows` must be an integer from 3 to {MAX_POINTS}.")
        request["table"] = {"table": table.strip(), "x_column": columns[0],
                            "y_column": columns[1], "max_rows": max_rows}

    predict_x = payload.get("predict_x")
    if predict_x is not None:
        if not isinstance(predict_x, list) or not predict_x or len(predict_x) > MAX_PREDICT:
            raise runtime.InvalidRequest(
                f"`predict_x` must be an array of 1 to {MAX_PREDICT} numbers.")
        predict_x = [_number(v, f"predict_x[{i}]") for i, v in enumerate(predict_x)]

    metrics = payload.get("metrics")
    if metrics is None:
        metrics = ["linear_regression", "normal_distribution", "p_values"]
        if predict_x:
            metrics.append("prediction")
    elif not isinstance(metrics, list) or not metrics \
            or any(m not in METRICS for m in metrics):
        raise runtime.InvalidRequest(
            f"`metrics` must be a non-empty array drawn from {', '.join(METRICS)}.")
    metrics = [m for m in METRICS if m in metrics]  # canonical order, no duplicates
    if "prediction" in metrics and not predict_x:
        raise runtime.InvalidRequest("`prediction` needs `predict_x`: the x values to predict at.")

    alpha = payload.get("alpha", 0.05)
    if isinstance(alpha, bool) or not isinstance(alpha, (int, float)) \
            or not 0 < float(alpha) < 1:
        raise runtime.InvalidRequest("`alpha` must be a number strictly between 0 and 1.")

    distribution_of = payload.get("distribution_of", "y")
    if distribution_of not in DISTRIBUTION_OF:
        raise runtime.InvalidRequest(
            f"`distribution_of` must be one of {', '.join(DISTRIBUTION_OF)}.")

    queries = payload.get("probability_queries") or []
    if not isinstance(queries, list) or len(queries) > MAX_QUERIES:
        raise runtime.InvalidRequest(
            f"`probability_queries` must be an array of at most {MAX_QUERIES} objects.")
    parsed_queries = []
    for i, query in enumerate(queries):
        if not isinstance(query, dict) or len(query) != 1:
            raise runtime.InvalidRequest(
                f"probability_queries[{i}] must be one of {{\"below\": v}}, "
                "{\"above\": v} or {\"between\": [a, b]}.")
        (kind, value), = query.items()
        if kind in ("below", "above"):
            parsed_queries.append({kind: _number(value, f"probability_queries[{i}].{kind}")})
        elif kind == "between" and isinstance(value, list) and len(value) == 2:
            low = _number(value[0], f"probability_queries[{i}].between[0]")
            high = _number(value[1], f"probability_queries[{i}].between[1]")
            if low > high:
                raise runtime.InvalidRequest(
                    f"probability_queries[{i}].between must be [low, high].")
            parsed_queries.append({"between": [low, high]})
        else:
            raise runtime.InvalidRequest(
                f"probability_queries[{i}] must be one of {{\"below\": v}}, "
                "{\"above\": v} or {\"between\": [a, b]}.")
    if parsed_queries and "normal_distribution" not in metrics:
        raise runtime.InvalidRequest("`probability_queries` needs the `normal_distribution` metric.")

    request.update(metrics=metrics, alpha=float(alpha), predict_x=predict_x or [],
                   distribution_of=distribution_of, queries=parsed_queries)
    if request["points"] is not None:
        _check_computable(*request["points"], request)
    return request


def _check_computable(xs: list, ys: list, request: dict) -> None:
    """The data-dependent refusals, for inline points (a table's rows are
    only known after the query)."""
    n = len(xs)
    needs_line = any(m in request["metrics"] for m in ("linear_regression", "p_values", "prediction")) \
        or request["distribution_of"] == "residuals"
    if needs_line:
        if n < 3:
            raise runtime.InvalidRequest(
                f"A regression with p-values needs at least 3 points; got {n}.")
        if min(xs) == max(xs):
            raise runtime.InvalidRequest(
                "Every x is the same value, so no line can be fitted (the slope is undefined).")
    elif n < 2:
        raise runtime.InvalidRequest(f"A distribution needs at least 2 points; got {n}.")


def precheck(payload: dict) -> None:
    """Refuse, before payment, anything this worker cannot deliver."""
    request = parse(payload)
    if request["table"] is not None and not bigquery.PROVIDER.available():
        raise runtime.ProviderUnavailable(
            "Reading a BigQuery table is not available on this deployment: "
            f"{bigquery.PROVIDER.unavailable_reason()} Send `points` instead.")


# --- distributions -----------------------------------------------------------

def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (modified Lentz)."""
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    d = 1.0 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 100_000):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        d = 1.0 / (d if abs(d) > tiny else tiny)
        c = 1.0 + aa / c
        c = c if abs(c) > tiny else tiny
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            return h
    raise runtime.PermanentProviderError(
        "Incomplete beta did not converge.", reason="numerical_failure")


def betainc(a: float, b: float, x: float, y: float) -> float:
    """Regularised incomplete beta I_x(a, b), with y = 1 - x passed in
    exactly by the caller so no precision is lost forming it."""
    if x <= 0.0:
        return 0.0
    if y <= 0.0:
        return 1.0
    log_front = (math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
                 + a * math.log(x) + b * math.log(y))
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(log_front) * _betacf(a, b, x) / a
    return 1.0 - math.exp(log_front) * _betacf(b, a, y) / b


def t_two_sided_p(t: float, df: float) -> float:
    """P(|T| >= |t|) for Student's t with df degrees of freedom."""
    if math.isinf(t):
        return 0.0
    t2 = t * t
    return min(1.0, betainc(df / 2.0, 0.5, df / (df + t2), t2 / (df + t2)))


def t_critical(alpha: float, df: float) -> float:
    """The t with P(|T| >= t) = alpha, by bisection on t_two_sided_p."""
    low, high = 0.0, 1.0
    while t_two_sided_p(high, df) > alpha:
        low, high = high, high * 2.0
        if high > 1e300:  # pragma: no cover - alpha below float resolution
            break
    for _ in range(200):
        mid = (low + high) / 2.0
        if mid in (low, high):
            break
        if t_two_sided_p(mid, df) > alpha:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0


def _mean(values: list) -> float:
    """Exactly rounded mean; exactly the value itself when all are equal, so a
    constant column has deviations of exactly zero rather than rounding noise."""
    first = values[0]
    if all(v == first for v in values):
        return first
    return math.fsum(values) / len(values)


def _moments(values: list) -> dict:
    """Mean, sample variance, and the population moments Jarque-Bera uses."""
    n = len(values)
    mean = _mean(values)
    dev = [v - mean for v in values]
    m2 = math.fsum(d * d for d in dev) / n
    m3 = math.fsum(d * d * d for d in dev) / n
    m4 = math.fsum(d * d * d * d for d in dev) / n
    variance = m2 * n / (n - 1)
    skewness = m3 / m2 ** 1.5 if m2 > 0 else None
    kurtosis = m4 / (m2 * m2) - 3.0 if m2 > 0 else None
    return {"n": n, "mean": mean, "variance": variance, "m2": m2,
            "skewness": skewness, "excess_kurtosis": kurtosis}


def _jarque_bera(moments: dict, alpha: float):
    if moments["skewness"] is None:
        return None
    statistic = moments["n"] / 6.0 * (moments["skewness"] ** 2
                                      + moments["excess_kurtosis"] ** 2 / 4.0)
    p = math.exp(-statistic / 2.0)
    return {"test": "jarque_bera", "statistic": statistic, "p_value": p,
            "null_hypothesis": "the values are normally distributed",
            "reject_at_alpha": p < alpha}


def _clean(value):
    """JSON has no NaN or infinity; an undefined quantity is null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean(v) for v in value]
    return value


# --- the computation -----------------------------------------------------------

def compute(xs: list, ys: list, request: dict) -> dict:
    """Everything the caller asked for, from the points. Pure and deterministic."""
    _check_computable(xs, ys, request)
    n = len(xs)
    alpha = request["alpha"]
    metrics = request["metrics"]
    notes = []
    out = {"n": n, "alpha": alpha, "confidence_level": 1.0 - alpha, "metrics": metrics,
           "linear_regression": None, "normal_distribution": None,
           "p_values": None, "prediction": None}

    fit = None
    if any(m in metrics for m in ("linear_regression", "p_values", "prediction")) \
            or request["distribution_of"] == "residuals":
        x_mean = _mean(xs)
        y_mean = _mean(ys)
        sxx = math.fsum((x - x_mean) ** 2 for x in xs)
        syy = math.fsum((y - y_mean) ** 2 for y in ys)
        sxy = math.fsum((x - x_mean) * (y - y_mean) for x, y in zip(xs, ys))
        slope = sxy / sxx
        intercept = y_mean - slope * x_mean
        residuals = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
        sse = math.fsum(e * e for e in residuals)
        df = n - 2
        s2 = sse / df
        s = math.sqrt(s2)
        se_slope = s / math.sqrt(sxx)
        se_intercept = s * math.sqrt(1.0 / n + x_mean * x_mean / sxx)
        r = max(-1.0, min(1.0, sxy / math.sqrt(sxx * syy))) if syy > 0 else None
        if r is None:
            notes.append("Every y is the same value: r and R^2 are undefined.")

        def t_of(estimate, se):
            if se > 0:
                return estimate / se
            return math.copysign(math.inf, estimate) if estimate != 0 else math.nan

        if sse == 0:
            notes.append("The points lie exactly on a line: the residual variance "
                         "is zero, so t statistics are infinite (null) and nonzero "
                         "coefficients have p = 0.")
        t_crit = t_critical(alpha, df)
        fit = {"x_mean": x_mean, "y_mean": y_mean, "sxx": sxx, "slope": slope,
               "intercept": intercept, "residuals": residuals, "df": df, "s": s,
               "t_crit": t_crit,
               "slope_t": t_of(slope, se_slope), "intercept_t": t_of(intercept, se_intercept)}

        if "linear_regression" in metrics:
            r2 = r * r if r is not None else None
            out["linear_regression"] = {
                "slope": slope,
                "intercept": intercept,
                "r": r,
                "r_squared": r2,
                "adjusted_r_squared": (1.0 - (1.0 - r2) * (n - 1) / df) if r2 is not None else None,
                "slope_std_error": se_slope,
                "intercept_std_error": se_intercept,
                "residual_std_error": s,
                "degrees_of_freedom": df,
                "slope_t": fit["slope_t"],
                "intercept_t": fit["intercept_t"],
                "t_critical": t_crit,
                "slope_ci": [slope - t_crit * se_slope, slope + t_crit * se_slope],
                "intercept_ci": [intercept - t_crit * se_intercept,
                                 intercept + t_crit * se_intercept],
                "f_statistic": fit["slope_t"] ** 2 if not math.isnan(fit["slope_t"]) else math.nan,
                "sse": sse,
                "sst": syy,
                "x_mean": x_mean,
                "y_mean": y_mean,
            }

        if "p_values" in metrics:
            def test(t, null):
                p = t_two_sided_p(t, df) if not math.isnan(t) else None
                return {"null_hypothesis": null, "t": t, "p_value": p,
                        "significant_at_alpha": (p < alpha) if p is not None else None}

            residual_moments = _moments(residuals)
            out["p_values"] = {
                "slope": test(fit["slope_t"], "slope = 0 (no linear relationship)"),
                "intercept": test(fit["intercept_t"], "intercept = 0"),
                "normality_of_residuals": _jarque_bera(residual_moments, alpha),
            }

        if "prediction" in metrics:
            rows = []
            for x0 in request["predict_x"]:
                y_hat = intercept + slope * x0
                lever = 1.0 / n + (x0 - x_mean) ** 2 / sxx
                se_mean = s * math.sqrt(lever)
                se_new = s * math.sqrt(1.0 + lever)
                rows.append({"x": x0, "y_hat": y_hat,
                             "mean_ci": [y_hat - t_crit * se_mean, y_hat + t_crit * se_mean],
                             "prediction_interval": [y_hat - t_crit * se_new,
                                                     y_hat + t_crit * se_new]})
            out["prediction"] = rows

    if "normal_distribution" in metrics:
        of = request["distribution_of"]
        values = ys if of == "y" else xs if of == "x" else fit["residuals"]
        moments = _moments(values)
        std_dev = math.sqrt(moments["variance"])
        ordered = sorted(values)
        half = n // 2
        median = ordered[half] if n % 2 else (ordered[half - 1] + ordered[half]) / 2.0
        model = {
            "of": of,
            "mean": moments["mean"],
            "std_dev": std_dev,
            "variance": moments["variance"],
            "min": ordered[0],
            "max": ordered[-1],
            "median": median,
            "skewness": moments["skewness"],
            "excess_kurtosis": moments["excess_kurtosis"],
            "quantiles": None,
            "probabilities": [],
            "normality": _jarque_bera(moments, alpha),
        }
        if std_dev > 0:
            dist = NormalDist(moments["mean"], std_dev)
            model["quantiles"] = [{"p": p, "value": dist.inv_cdf(p)} for p in QUANTILES]
            for query in request["queries"]:
                if "below" in query:
                    probability = dist.cdf(query["below"])
                elif "above" in query:
                    # The upper tail by symmetry, not 1 - cdf: no cancellation
                    # far out in the tail.
                    probability = dist.cdf(2 * moments["mean"] - query["above"])
                else:
                    low, high = query["between"]
                    probability = max(0.0, dist.cdf(high) - dist.cdf(low))
                model["probabilities"].append({"query": query, "probability": probability})
        else:
            notes.append(f"Every {of} value is the same: the normal model is "
                         "degenerate (std_dev 0), so no quantiles or probabilities.")
        out["normal_distribution"] = model

    out["notes"] = notes
    out["method"] = METHOD
    return _clean(out)


# --- the worker ----------------------------------------------------------------

class _LocalArithmetic:
    """The provider of the arithmetic: this process. Run through the envelope
    like any provider so the ledger records the attempt with a MEASURED cost
    of zero -- the margin report excludes unmeasured calls, and this one is
    genuinely free to serve."""
    id = "local-arithmetic"

    def available(self) -> bool:
        return True

    def unavailable_reason(self) -> str:
        return ""


COMPUTE = [_LocalArithmetic()]


def _table_sql(table: dict) -> str:
    # Identifiers are regex-validated in parse(); backticks quote them.
    x, y = f"`{table['x_column']}`", f"`{table['y_column']}`"
    return (
        "SELECT x, y, COUNT(*) OVER () AS rows_available FROM ("
        f"SELECT CAST({x} AS FLOAT64) AS x, CAST({y} AS FLOAT64) AS y "
        f"FROM `{table['table']}`) "
        "WHERE x IS NOT NULL AND y IS NOT NULL "
        "AND NOT IS_NAN(x) AND NOT IS_NAN(y) AND NOT IS_INF(x) AND NOT IS_INF(y) "
        "ORDER BY FARM_FINGERPRINT(FORMAT('%T,%T', x, y)), x, y "
        f"LIMIT {int(table['max_rows'])}")


async def run(ctx, payload: dict) -> dict:
    request = parse(payload)
    source = {"type": "points", "table": None, "x_column": None, "y_column": None,
              "sql": None, "rows_available": None, "rows_used": None,
              "sampled": False, "gib_processed": None}

    if request["table"] is not None:
        table = request["table"]
        sql = _table_sql(table)

        async def call(provider):
            return await provider.rows(sql, max_rows=table["max_rows"])

        data = await ctx.run("query", bigquery.PROVIDERS, call, per_attempt_seconds=150)
        rows = data["rows"]
        if not rows:
            raise runtime.InvalidRequest(
                f"`{table['table']}` has no rows where both {table['x_column']} and "
                f"{table['y_column']} are finite numbers.")
        try:
            xs = [float(row[0]) for row in rows]
            ys = [float(row[1]) for row in rows]
            available = int(rows[0][2])
        except (TypeError, ValueError, IndexError) as exc:
            raise runtime.InvalidProviderResponse(
                f"BigQuery returned rows this worker could not read: {exc}") from exc
        source.update(type="bigquery", table=table["table"], x_column=table["x_column"],
                      y_column=table["y_column"], sql=sql, rows_available=available,
                      sampled=available > len(xs), gib_processed=data["gib_processed"])
    else:
        xs, ys = request["points"]

    async def arithmetic(provider):
        # CPU work off the event loop: 100k points is a few hundred
        # milliseconds of arithmetic, and the loop answers /health and every 402.
        value = await asyncio.get_running_loop().run_in_executor(None, compute, xs, ys, request)
        return runtime.ProviderResult(value, cost_micros=0, cost_measured=True,
                                      usage=f"points={len(xs)}")

    result = await ctx.run("compute", COMPUTE, arithmetic, per_attempt_seconds=60,
                           max_attempts=1)
    source["rows_used"] = len(xs)
    if source["sampled"]:
        result["notes"].append(
            f"The table has {source['rows_available']} usable rows; {len(xs)} were used, "
            "chosen by FARM_FINGERPRINT of each (x, y) pair so the same table always "
            "yields the same rows. Raise max_rows (up to 100000) to use more.")
    return {"source": source, **result}


SKILLS = {"stats.probability": run}
PRECHECKS = {"stats.probability": precheck}
