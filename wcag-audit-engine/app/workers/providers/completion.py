"""Raw text completion, model chosen by the caller when given.

ONE WORKER, SEVERAL VENDORS. `llm.analyze`/`llm.extract` are a fixed
Gemini-backed shape (answer from THIS material, only from it). `llm.generate`
is the raw completion primitive underneath that, exposed directly for a
caller who wants to write their own prompt and pick a vendor.

Wave 1 ships one entry (Gemini, already live on this box). A second vendor
(Claude on Vertex, once Model Garden access is enabled) is added to
PROVIDERS the same way every other fallback provider here is added -- append
to the list, nothing else changes.
"""

import os
from typing import Optional

import httpx

from .. import runtime
from . import google_auth
from .gemini import _cost_micros, model_url, output_tokens_of


class _VertexGeminiCompletion:
    """Gemini, called as a raw completion rather than through the
    answer-from-material shape `llm.analyze` uses."""

    provider_name = "gemini"
    # The auto-updating alias plus the pinned GA models a caller may name
    # explicitly. All generated real output on `global` 2026-09-18.
    models = {m.strip() for m in os.environ.get(
        "WORKER_LLM_GENERATE_GEMINI_MODELS",
        "gemini-flash-latest,gemini-flash-lite-latest,gemini-3.5-flash,gemini-3.5-flash-lite"
    ).split(",") if m.strip()}
    default_model = os.environ.get("WORKER_LLM_GENERATE_GEMINI_MODEL", "gemini-flash-latest")
    id = f"vertex:{default_model}"
    _timeout = float(os.environ.get("WORKER_LLM_GENERATE_TIMEOUT_SECONDS", "60"))

    def matches(self, requested_provider: Optional[str], requested_model: Optional[str]) -> bool:
        if requested_provider not in (None, self.provider_name):
            return False
        return requested_model is None or requested_model in self.models

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def generate(self, prompt: str, system: Optional[str], max_tokens: int,
                       temperature: float, model: Optional[str] = None) -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())
        model = model or self.default_model

        project = google_auth.project()
        url = model_url(project, model, "generateContent")
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}

        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Vertex timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Vertex unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Vertex returned {response.status_code}", reason="provider_overloaded")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Vertex rejected the request ({response.status_code}): {response.text[:200]}")

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise runtime.InvalidProviderResponse("Vertex returned no candidate.")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise runtime.InvalidProviderResponse("Vertex returned an empty completion.")

        usage = data.get("usageMetadata") or {}
        prompt_tokens = int(usage.get("promptTokenCount") or 0)
        output_tokens = output_tokens_of(usage)
        cost, measured = _cost_micros(prompt_tokens, output_tokens, model)

        return runtime.ProviderResult(
            value={"text": text, "model": model, "provider": self.provider_name,
                   "finish_reason": (candidates[0].get("finishReason") or "").lower() or None,
                   "prompt_tokens": prompt_tokens, "output_tokens": output_tokens},
            cost_micros=cost, cost_measured=measured,
            usage=f"in={prompt_tokens} out={output_tokens}")


# Anthropic's own published per-MTok list rates (verified 2026-09-16),
# overridable because they are Anthropic's to change, not this file's.
_ANTHROPIC_RATES = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-5": (5.0, 25.0),
}

# Sampling parameters were removed on the current Claude generation: sending
# `temperature` to Sonnet 5 or Opus 5 is a 400. Haiku 4.5 still accepts it.
_CLAUDE_ACCEPTS_TEMPERATURE = {"claude-haiku-4-5"}


def _anthropic_cost_micros(model: str, input_tokens: int, output_tokens: int):
    rate_in = os.environ.get(f"WORKER_ANTHROPIC_PRICE_PER_MTOK_IN_{model.upper().replace('-', '_')}")
    rate_out = os.environ.get(f"WORKER_ANTHROPIC_PRICE_PER_MTOK_OUT_{model.upper().replace('-', '_')}")
    try:
        rate_in = float(rate_in) if rate_in else _ANTHROPIC_RATES.get(model, (None, None))[0]
        rate_out = float(rate_out) if rate_out else _ANTHROPIC_RATES.get(model, (None, None))[1]
    except ValueError:
        rate_in = rate_out = None
    if rate_in is None or rate_out is None:
        return None, False
    usd = (input_tokens / 1e6) * rate_in + (output_tokens / 1e6) * rate_out
    return int(round(usd * 1_000_000)), True


class _ClaudeOnVertex:
    """Claude, through Vertex's Model Garden -- same anthropic_version
    Messages-API shape as the first-party Claude API, billed to this
    project's own Google Cloud account rather than a separate Anthropic key.

    FAILS CLOSED PAST availability: Model Garden access for Claude is an
    explicit per-project enable (console: Vertex AI > Model Garden > Claude
    > Enable, plus accepting data-sharing), and a 403 before that is
    reported as ProviderUnavailable with the exact fix rather than a
    provider error -- this is a one-time setup step, not a health blip.
    """

    provider_name = "anthropic"
    models = {m.strip() for m in os.environ.get(
        "WORKER_ANTHROPIC_MODELS", "claude-haiku-4-5,claude-sonnet-5,claude-opus-5"
    ).split(",") if m.strip()}
    # Cheapest first: with no explicit model, the caller's $0.25 ceiling
    # should go as far as possible.
    default_model = os.environ.get("WORKER_ANTHROPIC_DEFAULT_MODEL", "claude-haiku-4-5")
    id = "vertex:anthropic"
    _timeout = float(os.environ.get("WORKER_LLM_GENERATE_TIMEOUT_SECONDS", "60"))
    _anthropic_version = "vertex-2023-10-16"

    def matches(self, requested_provider: Optional[str], requested_model: Optional[str]) -> bool:
        if requested_provider != self.provider_name:
            return False
        return requested_model is None or requested_model in self.models

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def generate(self, prompt: str, system: Optional[str], max_tokens: int,
                       temperature: float, model: Optional[str] = None) -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())
        model = model or self.default_model

        project = google_auth.project()
        url = (f"https://aiplatform.googleapis.com/v1/projects/{project}"
               f"/locations/global/publishers/anthropic/models/{model}:rawPredict")
        body = {
            "anthropic_version": self._anthropic_version, "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": prompt}],
        }
        if model in _CLAUDE_ACCEPTS_TEMPERATURE:
            body["temperature"] = temperature
        if system:
            body["system"] = system

        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Claude on Vertex timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Claude on Vertex unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Claude on Vertex returned {response.status_code}", reason="provider_overloaded")
        if response.status_code == 403:
            raise runtime.ProviderUnavailable(
                "Claude on Vertex returned 403 -- enable Claude in Vertex AI's Model "
                "Garden and accept its data-sharing terms for this project first.")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Claude on Vertex rejected the request ({response.status_code}): "
                f"{response.text[:200]}")

        data = response.json()
        content = data.get("content") or []
        text = "".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
        if not text:
            raise runtime.InvalidProviderResponse("Claude on Vertex returned no text.")

        usage = data.get("usage") or {}
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        cost, measured = _anthropic_cost_micros(model, input_tokens, output_tokens)

        return runtime.ProviderResult(
            value={"text": text, "model": model, "provider": self.provider_name,
                   "finish_reason": (data.get("stop_reason") or "").lower() or None,
                   "prompt_tokens": input_tokens, "output_tokens": output_tokens},
            cost_micros=cost, cost_measured=measured,
            usage=f"in={input_tokens} out={output_tokens}")


PROVIDERS = [_VertexGeminiCompletion(), _ClaudeOnVertex()]
