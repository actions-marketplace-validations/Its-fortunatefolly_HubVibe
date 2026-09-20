"""Text-to-speech via Google Cloud Text-to-Speech.

Connected, not rebuilt. Uses the same ADC this package already resolves --
the `cloud-platform` scope Vertex and BigQuery use also covers this API, so
no new credential or scope is needed.

COST: billed per character, not per token. Default rate matches Google's
published Standard-voice price (verified 2026-09-16); Neural2/WaveNet voices
cost 4x that, so the rate is looked up by voice family rather than asserted
once for everything.
"""

import os
import re
from typing import Optional

import httpx

from .. import runtime
from . import google_auth

_TIMEOUT = float(os.environ.get("WORKER_TTS_TIMEOUT_SECONDS", "45"))
_DEFAULT_VOICE = os.environ.get("WORKER_TTS_DEFAULT_VOICE", "en-US-Standard-C")
# Language is 2 OR 3 letters (fil-PH, yue-HK, cmn-CN are served), and the name
# after the region can carry several segments: en-US-Chirp3-HD-Aoede is Google's
# current top tier. The old pattern rejected both before any call was made.
_VOICE_RE = re.compile(r"^[a-z]{2,3}-[A-Z]{2}(?:-[A-Za-z0-9]+){1,3}$")


def _price_per_mchar(voice: str) -> Optional[float]:
    tier = "NEURAL2" if ("Neural2" in voice or "Wavenet" in voice or "WaveNet" in voice) \
        else "STANDARD"
    raw = os.environ.get(f"WORKER_TTS_PRICE_PER_MCHAR_{tier}_USD",
                         "16.0" if tier == "NEURAL2" else "4.0")
    try:
        return float(raw)
    except ValueError:
        return None


class _CloudTextToSpeech:
    id = "gcp-tts"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def synthesize(self, text: str, voice: Optional[str] = None) -> runtime.ProviderResult:
        if not google_auth.configured():
            raise runtime.ProviderUnavailable(google_auth.unavailable_reason())

        voice_name = voice or _DEFAULT_VOICE
        if not _VOICE_RE.match(voice_name):
            raise runtime.InvalidRequest(
                "`voice` must look like a Google TTS voice name, e.g. en-US-Standard-C.")
        language_code = "-".join(voice_name.split("-")[:2])

        body = {
            "input": {"text": text},
            "voice": {"languageCode": language_code, "name": voice_name},
            "audioConfig": {"audioEncoding": "MP3"},
        }
        try:
            headers = await google_auth.headers()
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(
                    "https://texttospeech.googleapis.com/v1/text:synthesize",
                    headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Cloud TTS timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Cloud TTS unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Cloud TTS returned {response.status_code}", reason="provider_overloaded")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Cloud TTS rejected the request ({response.status_code}): "
                f"{response.text[:200]}")

        data = response.json()
        audio = data.get("audioContent")
        if not audio:
            raise runtime.InvalidProviderResponse("Cloud TTS returned no audio.")

        rate = _price_per_mchar(voice_name)
        cost = int(round((len(text) / 1_000_000) * rate * 1_000_000)) if rate is not None else None
        return runtime.ProviderResult(
            value={"audio_base64": audio, "mime_type": "audio/mpeg",
                   "voice": voice_name, "chars": len(text)},
            cost_micros=cost, cost_measured=rate is not None, usage=f"chars={len(text)}")


PROVIDERS = [_CloudTextToSpeech()]
