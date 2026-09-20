"""Gemini on Vertex AI -- the reasoning provider behind every analysis worker.

Connected, not rebuilt: this speaks the same REST surface the Vertex SDKs
call (`:generateContent`), with ADC for auth.

COST HONESTY

Token USAGE is measured exactly, from the response's own usageMetadata. Token
PRICE is not asserted: a published rate this code has not verified would turn
into a fabricated margin the moment it changed. So the rate comes from
configuration (GEMINI_PRICE_PER_MTOK_IN / _OUT), and when it is unset the
attempt is recorded with real usage and cost_measured=False -- the ledger then
reports that worker's margin as unknown rather than as pure profit.
"""

import json
import datetime
import logging
import os
from typing import Optional

import httpx

from .. import runtime
from . import google_auth

log = logging.getLogger("hubvibe.workers.gemini")

# `global`, not a region. Probed on resolver-time 2026-09-18 with :countTokens:
# us-central1 serves ONLY the 2.5 line (which retires 2026-10-20) -- every 3.x
# model and every -latest alias 404s there -- while `global` serves all of
# them. Google also bills regional endpoints +10% over global.
DEFAULT_REGION = os.environ.get("WORKER_VERTEX_REGION", "global")


def model_url(project: str, model: str, method: str, region: Optional[str] = None) -> str:
    """The Vertex publisher-model URL. `global` has no region prefix on the host."""
    region = region or DEFAULT_REGION
    host = ("aiplatform.googleapis.com" if region == "global"
            else f"{region}-aiplatform.googleapis.com")
    return (f"https://{host}/v1/projects/{project}/locations/{region}"
            f"/publishers/google/models/{model}:{method}")


# Order is the fallback order. The first entry is Google's auto-updating
# alias: hot-swapped to the current Flash release with two weeks' notice, so
# this list stops needing a retirement chase. The second is a pinned GA model
# (no retirement before 2027-05) in case the alias ever fails to resolve.
# Both generated real output on `global` 2026-09-18.
_TEXT_MODELS = os.environ.get(
    "WORKER_GEMINI_MODELS", "gemini-flash-latest,gemini-3.5-flash"
).split(",")

_TIMEOUT = float(os.environ.get("WORKER_GEMINI_TIMEOUT_SECONDS", "120"))


def _rate(name: str) -> Optional[float]:
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        log.warning("%s is not a number; treating Gemini cost as unmeasured", name)
        return None


# Google's published Vertex rates, USD per 1M tokens (input, output), global
# endpoint, standard on-demand tier -- read off cloud.google.com/vertex-ai/
# generative-ai/pricing on 2026-09-19. "Output" is response AND reasoning,
# so thinking tokens are billed as output (see output_tokens_of).
_INTRO_ENDS = datetime.date(2027, 1, 1)  # 3.8 Flash introductory pricing ends
_GEMINI_RATES = {
    "gemini-3.8-flash": ((0.75, 3.75), (1.50, 7.50)),  # (until 2026-12-31, from 2027)
    "gemini-3.5-flash": ((1.50, 9.00),) * 2,
    "gemini-3.5-flash-lite": ((0.30, 2.50),) * 2,
    # Google does not publish which release an alias resolves to, and the
    # response names only the alias. Costed at the dearest current Flash /
    # Flash-Lite rate so the ledger can overstate cost, never profit.
    "gemini-flash-latest": ((1.50, 9.00),) * 2,
    "gemini-flash-lite-latest": ((0.30, 2.50),) * 2,
}


def output_tokens_of(usage: dict) -> int:
    """Billed output: the visible answer plus the model's reasoning tokens."""
    return int(usage.get("candidatesTokenCount") or 0) + int(usage.get("thoughtsTokenCount") or 0)


def _cost_micros(prompt_tokens: int, output_tokens: int, model: Optional[str] = None):
    """(micros, measured). An operator rate (both env vars) wins; otherwise
    the model's published rate; an unknown model stays unmeasured."""
    rate_in = _rate("GEMINI_PRICE_PER_MTOK_IN")
    rate_out = _rate("GEMINI_PRICE_PER_MTOK_OUT")
    if rate_in is None or rate_out is None:
        rates = _GEMINI_RATES.get((model or "").strip())
        if rates is None:
            return None, False
        rate_in, rate_out = rates[0] if datetime.date.today() < _INTRO_ENDS else rates[1]
    usd = (prompt_tokens / 1e6) * rate_in + (output_tokens / 1e6) * rate_out
    return int(round(usd * 1_000_000)), True


class _GeminiModel:
    """One model, as one provider entry, so fallback between models is the
    same machinery as fallback between vendors."""

    def __init__(self, model: str):
        self.model = model.strip()
        self.id = f"vertex:{self.model}"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def generate(self, prompt: str, system: Optional[str] = None,
                       json_output: bool = False,
                       temperature: float = 0.2) -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())

        project = google_auth.project()
        url = model_url(project, self.model, "generateContent")
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if json_output:
            body["generationConfig"]["responseMimeType"] = "application/json"

        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
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
                f"Vertex rejected the request ({response.status_code}): "
                f"{response.text[:200]}")

        data = response.json()
        candidates = data.get("candidates") or []
        if not candidates:
            # A safety block lands here: a real answer with no content. Not
            # retryable, and emphatically not something to bill for.
            raise runtime.InvalidProviderResponse(
                f"Vertex returned no candidate ({json.dumps(data)[:200]})")
        parts = (candidates[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            raise runtime.InvalidProviderResponse("Vertex returned an empty answer.")

        usage = data.get("usageMetadata") or {}
        prompt_tokens = int(usage.get("promptTokenCount") or 0)
        output_tokens = output_tokens_of(usage)
        cost, measured = _cost_micros(prompt_tokens, output_tokens, self.model)

        return runtime.ProviderResult(
            value={"text": text, "model": self.model,
                   "prompt_tokens": prompt_tokens, "output_tokens": output_tokens},
            cost_micros=cost, cost_measured=measured,
            usage=f"in={prompt_tokens} out={output_tokens}")

    async def generate_json(self, prompt: str, system: Optional[str] = None,
                            temperature: float = 0.2) -> runtime.ProviderResult:
        """Same call, but the answer must parse as JSON.

        Worth its own method because a worker that promises structured output
        and returns prose is selling a shape the buyer's parser will reject --
        the failure surfaces at the customer, not here. Parsing it on this
        side turns that into a retry against a provider we already have.
        """
        result = await self.generate(prompt, system=system, json_output=True,
                                     temperature=temperature)
        text = result.value["text"]
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start == -1 or end <= start:
                raise runtime.InvalidProviderResponse(
                    "Model did not return JSON.") from None
            try:
                parsed = json.loads(text[start:end + 1])
            except json.JSONDecodeError as exc:
                raise runtime.InvalidProviderResponse(
                    f"Model returned malformed JSON: {exc}") from exc
        result.value = {**result.value, "json": parsed}
        return result


PROVIDERS = [_GeminiModel(m) for m in _TEXT_MODELS if m.strip()]


def primary():
    return PROVIDERS[0] if PROVIDERS else None
