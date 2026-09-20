"""Live web search, via Gemini's Google Search grounding tool on Vertex.

Connected, not rebuilt: Google's own search index answers the query; this
adapter only asks Gemini to ground its answer in it and reports back the
sources it used. No separate search API, no separate key.
"""

import os

import httpx

from .. import runtime
from . import google_auth
from .gemini import _cost_micros, model_url, output_tokens_of

log = __import__("logging").getLogger("hubvibe.workers.search_grounding")

# Auto-updating alias; generated real output on `global` 2026-09-18.
_MODEL = os.environ.get("WORKER_SEARCH_MODEL", "gemini-flash-latest")
_TIMEOUT = float(os.environ.get("WORKER_SEARCH_TIMEOUT_SECONDS", "45"))
_SEARCH_QUERY_USD = float(os.environ.get("WORKER_SEARCH_PRICE_PER_QUERY_USD", "0.014"))


class _GeminiGroundedSearch:
    id = f"vertex-search:{_MODEL}"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def search(self, query: str) -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())

        project = google_auth.project()
        url = model_url(project, _MODEL, "generateContent")
        body = {
            "contents": [{"role": "user", "parts": [{"text": (
                "Answer this from current web search results, concisely and "
                f"factually: {query}")}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.1},
        }
        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Vertex search timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Vertex search unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Vertex search returned {response.status_code}", reason="provider_overloaded")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Vertex search rejected the request ({response.status_code}): "
                f"{response.text[:200]}")

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise runtime.InvalidProviderResponse("Search grounding returned no candidate.")
        candidate = candidates[0]
        parts = (candidate.get("content") or {}).get("parts") or []
        answer = "".join(p.get("text", "") for p in parts).strip()
        if not answer:
            raise runtime.InvalidProviderResponse("Search grounding returned an empty answer.")

        grounding = candidate.get("groundingMetadata") or {}
        sources = []
        seen_uris = set()
        for chunk in grounding.get("groundingChunks") or []:
            web = chunk.get("web") or {}
            uri = web.get("uri")
            if not uri or uri in seen_uris:
                continue
            seen_uris.add(uri)
            sources.append({"url": uri, "title": web.get("title")})

        usage = data.get("usageMetadata") or {}
        prompt_tokens = int(usage.get("promptTokenCount") or 0)
        output_tokens = output_tokens_of(usage)
        cost, measured = _cost_micros(prompt_tokens, output_tokens, _MODEL)
        # Gemini 3 grounding bills each web query Google runs, not the prompt:
        # $14 per 1,000 past 5,000 free a month (Vertex pricing, 2026-09-19).
        # Charged at list price so the free allowance never flatters margins.
        queries = grounding.get("webSearchQueries") or []
        if cost is not None:
            cost += int(round(len(queries) * _SEARCH_QUERY_USD * 1_000_000))

        return runtime.ProviderResult(
            value={"answer": answer, "sources": sources, "model": _MODEL,
                   "queries": queries},
            cost_micros=cost, cost_measured=measured,
            usage=f"in={prompt_tokens} out={output_tokens} queries={len(queries)} sources={len(sources)}")


PROVIDERS = [_GeminiGroundedSearch()]
