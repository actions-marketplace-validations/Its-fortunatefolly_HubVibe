"""One Google credential path for every Google-backed provider.

WHY NOT A CLIENT LIBRARY PER SERVICE

`google-auth` is already in the image (it arrives with google-genai and
google-cloud-firestore, both already pinned). Vertex and BigQuery are then
reachable over plain REST with httpx, which this service already depends on.
That is the difference between adding two heavyweight SDKs and adding none --
and the REST surface is the same one the SDKs call.

CREDENTIALS

Application Default Credentials, resolved once and refreshed as needed:
  - on the VPS: a service-account JSON at GOOGLE_APPLICATION_CREDENTIALS
  - in Cloud Shell / on Cloud Run: ambient ADC
Fail-closed everywhere: if no credential resolves, the Google-backed workers
report unavailable and are never advertised, rather than 500ing a caller who
has already paid.
"""

import asyncio
import logging
import os
import threading
from typing import Optional

log = logging.getLogger("hubvibe.workers.google_auth")

_SCOPE = "https://www.googleapis.com/auth/cloud-platform"

_lock = threading.Lock()
_creds = None
_project: Optional[str] = None
_resolved = False
_error: Optional[str] = None

# How long credential resolution may take before we give up and report this
# deployment as having no Google credentials.
#
# THIS BOUND IS NOT OPTIONAL. google.auth.default() ends its search by probing
# the GCE metadata service at 169.254.169.254, and that probe can CONNECT and
# then never answer -- on a host where the address is routable but nothing
# serves it, the socket simply stays open. Observed here: a test run sat in
# do_sys_poll on two ESTABLISHED sockets to 169.254.169.254:80 for 24 minutes.
# On a node that would be /health and /work hanging forever, so the bound is a
# correctness requirement rather than a tuning knob.
_RESOLVE_TIMEOUT = float(os.environ.get("WORKER_GCP_AUTH_TIMEOUT_SECONDS", "5"))

# Bound the metadata probe itself too, for the same reason. google-auth reads
# this env var; setting it before the first resolve keeps the underlying
# socket from outliving our own deadline. Never overrides an operator value.
os.environ.setdefault("GCE_METADATA_TIMEOUT", os.environ.get(
    "WORKER_GCP_METADATA_TIMEOUT_SECONDS", "2"))


def _resolve_blocking():
    import google.auth

    creds, project = google.auth.default(scopes=[_SCOPE])
    return creds, os.environ.get("WORKER_GCP_PROJECT") or project


def _resolve():
    """Resolve ADC once, under a hard deadline. Never blocks indefinitely.

    The work happens on a DAEMON thread: if the metadata probe is wedged, we
    abandon it rather than join it, so neither a request nor process exit can
    be held hostage by a socket that will never answer. The result is cached
    including the failure, so a node without credentials does not pay the
    timeout again on every call.
    """
    global _creds, _project, _resolved, _error
    with _lock:
        if _resolved:
            return _creds, _project
        _resolved = True

        box = {}

        def _worker():
            try:
                box["value"] = _resolve_blocking()
            except Exception as exc:  # noqa: BLE001 - reported below
                box["error"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=_worker, name="hubvibe-gcp-auth", daemon=True)
        thread.start()
        thread.join(_RESOLVE_TIMEOUT)

        if thread.is_alive():
            _error = (
                f"Google credential lookup did not finish within "
                f"{_RESOLVE_TIMEOUT:.0f}s (commonly a wedged GCE metadata probe); "
                "Google-backed workers stay off on this deployment."
            )
            log.warning("%s", _error)
            return None, None
        if "error" in box:
            _error = box["error"]
            log.info("Google ADC not available; Google-backed workers stay off (%s)",
                     _error)
            return None, None

        creds, project = box.get("value", (None, None))
        if not project:
            _error = ("Google credentials resolved but no project; set "
                      "WORKER_GCP_PROJECT.")
            return None, None
        _creds, _project = creds, project
        return _creds, _project


def prime() -> None:
    """Resolve credentials in the background at startup.

    Called from router.configure() so the one-off cost is paid while the
    process is starting rather than inside the first agent's paid request.
    """
    threading.Thread(target=_resolve, name="hubvibe-gcp-auth-prime",
                     daemon=True).start()


def configured() -> bool:
    creds, project = _resolve()
    return creds is not None and bool(project)


def unavailable_reason() -> str:
    _resolve()
    return _error or "no Google credentials on this deployment"


def project() -> Optional[str]:
    return _resolve()[1]


def reset_for_tests() -> None:
    global _creds, _project, _resolved, _error
    with _lock:
        _creds = _project = _error = None
        _resolved = False
        _scoped.clear()


def _token_blocking() -> str:
    creds, _ = _resolve()
    if creds is None:
        raise RuntimeError(unavailable_reason())
    if not creds.valid:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
    return creds.token


async def token() -> str:
    """Bearer token, refreshed off the event loop.

    The refresh is a blocking HTTPS call; doing it inline would stall every
    other in-flight request on this worker process.
    """
    return await asyncio.to_thread(_token_blocking)


async def headers() -> dict:
    """Auth headers including the quota project.

    X-Goog-User-Project is not optional for user-flavoured ADC: without it
    Vertex answers 403 "requires a quota project" on list calls and 404 on
    publisher-model reads, which reads exactly like "the model does not
    exist". Sending it always removes a whole class of phantom outage.
    """
    return {
        "Authorization": f"Bearer {await token()}",
        "X-Goog-User-Project": project() or "",
        "Content-Type": "application/json",
    }


_scoped = {}


def _scoped_token_blocking(scope: str) -> str:
    """A token for ONE extra OAuth scope, from the same credential.

    The shared credential is minted for `cloud-platform`, which some Google
    APIs do not accept (Maps Grounding Lite wants `maps-platform.mapstools`). A
    service-account credential can be re-scoped without a second key; ambient
    Compute/Cloud Shell credentials ignore requested scopes, which is why a
    worker relying on this must be proven on the deployment that serves it.
    """
    creds, _ = _resolve()
    if creds is None:
        raise RuntimeError(unavailable_reason())
    with _lock:
        scoped = _scoped.get(scope)
        if scoped is None:
            scoped = (creds.with_scopes([_SCOPE, scope])
                      if hasattr(creds, "with_scopes") else creds)
            _scoped[scope] = scoped
    if not scoped.valid:
        from google.auth.transport.requests import Request

        scoped.refresh(Request())
    return scoped.token


async def scoped_headers(scope: str) -> dict:
    """`headers()`, but with a token carrying `scope` as well."""
    return {
        "Authorization": f"Bearer {await asyncio.to_thread(_scoped_token_blocking, scope)}",
        "X-Goog-User-Project": project() or "",
        "Content-Type": "application/json",
    }
