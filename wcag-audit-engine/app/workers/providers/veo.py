"""Video generation via Veo on Vertex -- predictLongRunning, polled with
fetchPredictOperation until the operation completes.

PROVEN, NOT ASSUMED. On resolver-time, 2026-09-18, a real call to
`veo-3.1-fast-generate-001` in us-central1 (4s, 16:9) was accepted, finished
in ~30s -- well inside the ~240s a paid x402 call can wait -- and returned the
clip inline as `response.videos[0].bytesBase64Encoded` (video/mp4). That run
is what retired the old WORKER_VEO_ENABLED gate: the gate existed only because
the model id was unverified (the old default, veo-3.1-generate-preview, was
discontinued 2026-04-02).

REGION is Veo's own setting. Gemini moved to the `global` endpoint; Veo is
served regionally, so it must not follow WORKER_VERTEX_REGION there.
"""

import asyncio
import os
import time
from typing import Optional

import httpx

from .. import runtime
from . import google_auth

DEFAULT_REGION = os.environ.get("WORKER_VEO_REGION", "us-central1")
_MODEL = os.environ.get("WORKER_VEO_MODEL", "veo-3.1-fast-generate-001")
_TIMEOUT = float(os.environ.get("WORKER_VEO_TIMEOUT_SECONDS", "30"))
_POLL_INTERVAL = float(os.environ.get("WORKER_VEO_POLL_SECONDS", "8"))
_ASPECT_RATIOS = {"16:9", "9:16"}
_DURATIONS = {4, 6, 8}


def _price_per_second() -> Optional[float]:
    # Google's published Veo 3.1 Fast rate at the default 720p (1080p is $0.12,
    # 4K $0.30). Overridable, because the rate is Google's to change.
    raw = os.environ.get("WORKER_VEO_PRICE_PER_SECOND_USD", "0.10")
    try:
        return float(raw)
    except ValueError:
        return None


class _Veo:
    id = f"vertex:{_MODEL}"

    def available(self) -> bool:
        return google_auth.configured()

    def unavailable_reason(self) -> str:
        return google_auth.unavailable_reason()

    async def _post(self, url: str, headers: dict, body: dict) -> dict:
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise runtime.TransientProviderError(f"Veo timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise runtime.TransientProviderError(f"Veo unreachable: {exc}") from exc

        if response.status_code in (429, 500, 502, 503, 504):
            raise runtime.TransientProviderError(
                f"Veo returned {response.status_code}", reason="provider_overloaded")
        if response.status_code >= 400:
            raise runtime.PermanentProviderError(
                f"Veo rejected the request ({response.status_code}): {response.text[:200]}")
        return response.json()

    async def generate(self, prompt: str, aspect_ratio: str = "16:9",
                       duration_seconds: int = 6, generate_audio: bool = False,
                       deadline_seconds: float = 200) -> runtime.ProviderResult:
        if not self.available():
            raise runtime.ProviderUnavailable(self.unavailable_reason())
        if aspect_ratio not in _ASPECT_RATIOS:
            raise runtime.InvalidRequest(f"`aspect_ratio` must be one of {sorted(_ASPECT_RATIOS)}.")
        if duration_seconds not in _DURATIONS:
            raise runtime.InvalidRequest(f"`duration_seconds` must be one of {sorted(_DURATIONS)}.")

        project = google_auth.project()
        base = (f"https://{DEFAULT_REGION}-aiplatform.googleapis.com/v1/projects/{project}"
               f"/locations/{DEFAULT_REGION}/publishers/google/models/{_MODEL}")
        headers = await google_auth.headers()

        started = await self._post(f"{base}:predictLongRunning", headers, {
            "instances": [{"prompt": prompt}],
            "parameters": {"aspectRatio": aspect_ratio, "durationSeconds": duration_seconds,
                           "sampleCount": 1, "generateAudio": generate_audio},
        })
        operation_name = started.get("name")
        if not operation_name:
            raise runtime.InvalidProviderResponse(
                "Veo did not return an operation to poll for.")

        deadline = time.monotonic() + deadline_seconds
        operation = None
        while time.monotonic() < deadline:
            await asyncio.sleep(_POLL_INTERVAL)
            # Fresh headers per poll: a generation can outlive the access
            # token fetched at submit time, and a poll sent with the stale
            # one is a 401 on a job the caller has already been waiting on
            # (seen live 2026-09-19, 34s in). headers() refreshes only when
            # the token has actually expired, so this costs nothing otherwise.
            headers = await google_auth.headers()
            operation = await self._post(f"{base}:fetchPredictOperation", headers,
                                         {"operationName": operation_name})
            if operation.get("done"):
                break
        else:
            raise runtime.DeadlineExceeded(
                f"Veo had not finished generating after {deadline_seconds:.0f}s.")

        if operation is None or not operation.get("done"):
            raise runtime.DeadlineExceeded("Veo did not finish within this job's time budget.")
        if "error" in operation and operation["error"]:
            raise runtime.PermanentProviderError(
                f"Veo generation failed: {operation['error'].get('message', 'unknown error')}")

        payload = operation.get("response") or {}
        videos = payload.get("videos") or payload.get("predictions") or []
        if not videos or not isinstance(videos, list):
            raise runtime.InvalidProviderResponse(
                f"Veo finished but returned no video (response keys: {sorted(payload.keys())}).")
        video = videos[0]
        video_b64 = video.get("bytesBase64Encoded") or video.get("bytesBase64")
        gcs_uri = video.get("gcsUri")
        if not video_b64 and not gcs_uri:
            raise runtime.InvalidProviderResponse(
                f"Veo's video entry had neither inline bytes nor a GCS uri (keys: "
                f"{sorted(video.keys())}).")

        rate = _price_per_second()
        cost = int(round(rate * duration_seconds * 1_000_000)) if rate is not None else None
        return runtime.ProviderResult(
            value={"video_base64": video_b64, "gcs_uri": gcs_uri,
                   "mime_type": video.get("mimeType", "video/mp4"), "model": _MODEL,
                   "duration_seconds": duration_seconds},
            cost_micros=cost, cost_measured=rate is not None,
            usage=f"duration={duration_seconds}s")


PROVIDERS = [_Veo()]
