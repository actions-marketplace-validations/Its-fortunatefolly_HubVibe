"""Speech-to-text via Google Cloud Speech-to-Text v2 (synchronous recognize).

Connected, not rebuilt. Same ADC as every other Google-backed provider here.

SYNCHRONOUS ONLY, ON PURPOSE: the v2 sync `recognize` call is capped by
Google at 60 seconds / 10MB of inline audio (verified 2026-09-16). That cap
IS the worker's own limit -- a longer file needs the async/batch API, which
this deployment does not offer, so it is refused before payment rather than
silently truncated.
"""

import base64
import binascii
import os
from typing import Optional

import httpx

from .. import runtime
from . import google_auth

_TIMEOUT = float(os.environ.get("WORKER_STT_TIMEOUT_SECONDS", "60"))
_MODEL = os.environ.get("WORKER_STT_MODEL", "long")
MAX_AUDIO_BYTES = 10 * 1024 * 1024


def _price_per_min() -> Optional[float]:
    raw = os.environ.get("WORKER_STT_PRICE_PER_MIN_USD")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


class _SpeechToText:
    id = "gcp-stt"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def recognize(self, audio_base64: str, language_code: str = "en-US"
                        ) -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())
        try:
            raw = base64.b64decode(audio_base64, validate=True)
        except (binascii.Error, ValueError):
            raise runtime.InvalidRequest("`audio_base64` is not valid base64.")
        if not raw:
            raise runtime.InvalidRequest("`audio_base64` decoded to zero bytes.")
        if len(raw) > MAX_AUDIO_BYTES:
            raise runtime.InvalidRequest(
                f"Audio is {len(raw)} bytes, over this worker's {MAX_AUDIO_BYTES} byte "
                "(and ~60 second) synchronous limit.")

        project = google_auth.project()
        url = (f"https://speech.googleapis.com/v2/projects/{project}"
               "/locations/global/recognizers/_:recognize")
        body = {
            "config": {"autoDecodingConfig": {}, "languageCodes": [language_code],
                      "model": _MODEL},
            "content": audio_base64,
        }
        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Speech-to-Text timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Speech-to-Text unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Speech-to-Text returned {response.status_code}",
                reason="provider_overloaded")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Speech-to-Text rejected the request ({response.status_code}): "
                f"{response.text[:200]}")

        data = response.json()
        results = data.get("results") or []
        transcript_parts, confidences = [], []
        for result in results:
            alternatives = result.get("alternatives") or []
            if alternatives:
                transcript_parts.append(alternatives[0].get("transcript", ""))
                if alternatives[0].get("confidence") is not None:
                    confidences.append(alternatives[0]["confidence"])
        transcript = " ".join(p.strip() for p in transcript_parts if p.strip())
        if not transcript:
            raise runtime.InvalidProviderResponse(
                "No speech was recognized in this audio (silence, or an unsupported "
                "encoding/language).")

        # v2 reports what Google bills ("15s"); measured, never guessed from
        # byte count. Cost is measured when the operator states the rate.
        billed = (data.get("metadata") or {}).get("totalBilledDuration")
        billed_seconds = None
        if isinstance(billed, str) and billed.endswith("s"):
            try:
                billed_seconds = float(billed[:-1])
            except ValueError:
                billed_seconds = None
        rate = _price_per_min()
        cost = (int(round(billed_seconds / 60 * rate * 1_000_000))
                if billed_seconds is not None and rate is not None else None)
        return runtime.ProviderResult(
            value={"transcript": transcript, "language_code": language_code,
                   "confidence": (sum(confidences) / len(confidences)) if confidences else None,
                   "model": _MODEL, "billed_seconds": billed_seconds},
            cost_micros=cost, cost_measured=cost is not None,
            usage=f"bytes={len(raw)} billed_s={billed_seconds}")


PROVIDERS = [_SpeechToText()]
