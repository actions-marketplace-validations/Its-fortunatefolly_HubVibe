"""Image generation via Gemini on Vertex.

Connected, not rebuilt: this speaks the same `:generateContent` REST surface
`completion.py` and `gemini.py` already use for text, with the same ADC this
package resolves for every other Google-backed worker. Only two things differ
from a text call -- `responseModalities` asks for an image, and the bytes come
back in a part's `inlineData` instead of its `text`.

Imagen is deliberately NOT used: Google listed every `imagen-4.0-*` endpoint
as discontinued after 2026-06-30 and named a Gemini image model as the
replacement, so an Imagen call is a paid 404 waiting to happen.

COST: unlike token-priced inference, image generation bills a FLAT rate per
image -- there is no usage number to measure per call, the rate itself is the
fact. Overridable because the rate is Google's to change.
"""

import os
from typing import Optional

import httpx

from .. import runtime
from . import google_auth
from .gemini import model_url

# Google's own named migration target off the discontinued imagen-4.0-* line.
# Generated a real 1:1 PNG on `global` 2026-09-18; retires 2027-03-15.
_MODEL = os.environ.get("WORKER_IMAGE_MODEL", "gemini-2.5-flash-image")
_TIMEOUT = float(os.environ.get("WORKER_IMAGE_TIMEOUT_SECONDS", "90"))
_ASPECT_RATIOS = {"1:1", "3:4", "4:3", "16:9", "9:16"}


def _price_per_image() -> Optional[float]:
    raw = os.environ.get("WORKER_IMAGE_PRICE_USD", "0.04")
    try:
        return float(raw)
    except ValueError:
        return None


def _first_image(data: dict) -> Optional[dict]:
    for candidate in data.get("candidates") or []:
        for part in (candidate.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if inline and inline.get("data"):
                return inline
    return None


class _GeminiImage:
    id = f"vertex:{_MODEL}"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def generate(self, prompt: str, aspect_ratio: str = "1:1") -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())
        if aspect_ratio not in _ASPECT_RATIOS:
            raise runtime.InvalidRequest(
                f"`aspect_ratio` must be one of {sorted(_ASPECT_RATIOS)}.")

        project = google_auth.project()
        url = model_url(project, _MODEL, "generateContent")
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["TEXT", "IMAGE"],
                "imageConfig": {"aspectRatio": aspect_ratio},
            },
        }
        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(
                f"Image generation timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(
                f"Image generation unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Image generation returned {response.status_code}",
                reason="provider_overloaded")
        if response.status_code >= 400:
            # A prompt the safety filter refuses is the CALLER's input being
            # wrong for this provider, not a bug worth retrying.
            raise runtime.PermanentProviderError(
                f"Image generation rejected the request ({response.status_code}): "
                f"{response.text[:200]}")

        data = response.json()
        image = _first_image(data)
        if image is None:
            # A filtered prompt comes back 200 with a finishReason and no image
            # part; say which, so the caller learns something from the failure.
            reason = ""
            for candidate in data.get("candidates") or []:
                if candidate.get("finishReason"):
                    reason = f" (finishReason: {candidate['finishReason']})"
                    break
            raise runtime.InvalidProviderResponse(
                f"Image generation returned no image{reason}.")

        rate = _price_per_image()
        cost = int(round(rate * 1_000_000)) if rate is not None else None
        return runtime.ProviderResult(
            value={"image_base64": image["data"],
                   "mime_type": image.get("mimeType") or image.get("mime_type", "image/png"),
                   "model": _MODEL},
            cost_micros=cost, cost_measured=rate is not None, usage="images=1")


PROVIDERS = [_GeminiImage()]
