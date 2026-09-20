import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDITS_PATH = REPO_ROOT / "wcag-audit-engine" / "app" / "audits.py"

spec = importlib.util.spec_from_file_location("audits", AUDITS_PATH)
audits = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audits)


# --- SEO audit -----------------------------------------------------------

GOOD_HTML = """<html lang="en"><head>
<title>A reasonably short title</title>
<meta name="description" content="A reasonably short description">
<link rel="canonical" href="https://example.com">
<meta property="og:title" content="A"><meta property="og:description" content="B">
<meta property="og:image" content="C"><meta property="og:type" content="website">
<script type="application/ld+json">{}</script>
</head><body><h1>Hello</h1></body></html>"""


def test_seo_audit_passes_on_well_formed_page():
    result = audits.run_seo_audit(GOOD_HTML, None)
    assert result["status"] == "ok"
    assert result["pass"] is True
    assert result["findings"] == []


def test_seo_audit_flags_missing_title_and_description():
    html = "<html><body><h1>Hi</h1></body></html>"
    result = audits.run_seo_audit(html, None)
    ids = {f["id"] for f in result["findings"]}
    assert "missing-title" in ids
    assert "missing-meta-description" in ids
    assert "missing-h1" not in ids
    assert result["pass"] is False


def test_seo_audit_flags_missing_and_multiple_h1():
    no_h1 = "<html><head><title>T</title><meta name=\"description\" content=\"d\"></head><body></body></html>"
    result = audits.run_seo_audit(no_h1, None)
    assert any(f["id"] == "missing-h1" for f in result["findings"])
    assert result["pass"] is False

    two_h1 = (
        "<html><head><title>T</title><meta name=\"description\" content=\"d\"></head>"
        "<body><h1>One</h1><h1>Two</h1></body></html>"
    )
    result = audits.run_seo_audit(two_h1, None)
    assert any(f["id"] == "multiple-h1" for f in result["findings"])


def test_seo_audit_requires_html_or_url():
    with pytest.raises(ValueError):
        audits.run_seo_audit(None, None)


def test_seo_audit_fetches_url_when_no_html_given():
    fake_resp = MagicMock()
    fake_resp.text = GOOD_HTML
    fake_resp.raise_for_status = MagicMock()
    with patch("httpx.get", return_value=fake_resp) as mock_get:
        result = audits.run_seo_audit(None, "https://example.com")
    mock_get.assert_called_once()
    assert result["pass"] is True


# --- Security audit --------------------------------------------------------


def _fake_security_response(url="https://example.com/", headers=None):
    resp = MagicMock()
    resp.url = url
    resp.headers = headers or {}
    resp.raise_for_status = MagicMock()
    return resp


def test_security_audit_passes_with_all_headers_present():
    headers = {
        "strict-transport-security": "max-age=31536000",
        "content-security-policy": "default-src 'self'",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "referrer-policy": "no-referrer",
    }
    with patch("httpx.get", return_value=_fake_security_response(headers=headers)):
        result = audits.run_security_audit("https://example.com")
    assert result["pass"] is True
    assert result["findings"] == []


def test_security_audit_flags_non_https_as_critical():
    with patch("httpx.get", return_value=_fake_security_response(url="http://example.com/")):
        result = audits.run_security_audit("http://example.com")
    ids = {f["id"] for f in result["findings"]}
    assert "no-https" in ids
    assert result["pass"] is False


def test_security_audit_flags_wildcard_cors():
    headers = {"access-control-allow-origin": "*"}
    with patch("httpx.get", return_value=_fake_security_response(headers=headers)):
        result = audits.run_security_audit("https://example.com")
    ids = {f["id"] for f in result["findings"]}
    assert "wildcard-cors" in ids


def test_security_audit_requires_url():
    with pytest.raises(ValueError):
        audits.run_security_audit(None)


# --- Performance audit -----------------------------------------------------


def test_performance_audit_requires_url():
    with pytest.raises(ValueError):
        audits.run_performance_audit(None)


def test_performance_audit_flags_high_dom_complexity_and_heavy_page():
    fake_page = MagicMock()
    fake_page.evaluate.return_value = 2000  # over the 1500 threshold

    def _capture_response_handler(event_name, handler):
        # Simulate one large response so total bytes crosses the 3MB threshold.
        fake_response = MagicMock()
        fake_response.headers = {"content-length": "4000000"}
        handler(fake_response)

    fake_page.on.side_effect = _capture_response_handler

    # The audit now runs on a pooled browser (see app/browser_pool.py) rather
    # than launching its own, so stand in for the pool's page handout.
    def _fake_with_page(fn, **context_kwargs):
        return fn(fake_page)

    with patch.object(audits.browser_pool, "with_page", _fake_with_page):
        result = audits.run_performance_audit("https://example.com")

    ids = {f["id"] for f in result["findings"]}
    assert "high-dom-complexity" in ids
    assert "heavy-page-weight" in ids
    assert result["pass"] is False
    assert result["metrics"]["dom_node_count"] == 2000
    assert result["metrics"]["total_bytes_transferred"] == 4000000


def test_performance_audit_gets_an_isolated_context_not_a_shared_cache():
    """Reusing a warmed cache across audits would under-report transferred
    bytes and request count, quietly scoring a heavy page as light."""
    fake_page = MagicMock()
    fake_page.evaluate.return_value = 10
    fake_page.on.side_effect = lambda event_name, handler: None

    seen = {}

    def _fake_with_page(fn, **context_kwargs):
        seen.update(context_kwargs)
        return fn(fake_page)

    with patch.object(audits.browser_pool, "with_page", _fake_with_page):
        audits.run_performance_audit("https://example.com")

    # A per-call context is what provides isolation; assert we asked for one
    # with our own user agent rather than reusing a default shared page.
    assert seen.get("user_agent") == audits._USER_AGENT


# --- redirect safety -----------------------------------------------------


def _redirector(location):
    """A server whose only job is to hand back one 302 to `location`."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("content-length", "0")
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/" % server.server_address[1]


def _ok_server(body=b"<html><title>t</title></html>"):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, "http://127.0.0.1:%d/" % server.server_address[1]


def test_fetch_once_rechecks_every_redirect_hop(monkeypatch):
    """The target gate validated the caller's URL and nothing after it, while
    httpx followed redirects itself. A public host answering `302 Location:
    http://169.254.169.254/...` therefore walked the fetch past the gate and
    into the deployment -- the exact proxy the gate exists to refuse.

    Here hop 1 is allowed and hop 2 is not, so the only way to pass is to
    check the destination rather than the request.
    """
    secret, secret_url = _ok_server(b"<html><title>internal</title></html>")
    redirector, entry = _redirector(secret_url)
    try:
        seen = []

        def _fake_reason(url):
            seen.append(url)
            if url and url.startswith(secret_url):
                return "resolves to a private, loopback, link-local or reserved address"
            return None

        monkeypatch.setattr(audits, "blocked_target_reason", _fake_reason)

        with pytest.raises(audits.TargetNotFetchable):
            audits.fetch_once(entry)

        assert any(u.startswith(secret_url) for u in seen), (
            "the redirect destination was never checked"
        )
    finally:
        redirector.shutdown()
        redirector.server_close()
        secret.shutdown()
        secret.server_close()


def test_fetch_once_still_follows_an_allowed_redirect():
    """The guard must not break ordinary sites: a plain 302 between two
    permitted addresses still lands on the final response."""
    target, target_url = _ok_server(b"<html><title>landed</title></html>")
    redirector, entry = _redirector(target_url)
    try:
        import os

        os.environ["ALLOW_PRIVATE_TARGETS"] = "1"
        try:
            response = audits.fetch_once(entry)
        finally:
            os.environ.pop("ALLOW_PRIVATE_TARGETS", None)
        assert response.status_code == 200
        assert b"landed" in response.content
    finally:
        redirector.shutdown()
        redirector.server_close()
        target.shutdown()
        target.server_close()


# --- page weight is actually weighed --------------------------------------


class _Resp:
    """A Playwright-ish response: headers, and a body the browser holds."""

    def __init__(self, headers, body=b"", body_raises=False):
        self.headers = headers
        self._body = body
        self._raises = body_raises

    def body(self):
        if self._raises:
            raise RuntimeError("no body available")
        return self._body


def test_a_chunked_response_is_weighed_not_counted_as_zero():
    """Transferred bytes came only from `content-length`, which HTTP/1.1
    chunked responses -- the normal shape for compressed or streamed HTML --
    do not send. Those counted as zero, so a genuinely heavy page stayed under
    the threshold and was reported clean: a check that never ran, passing."""
    chunked = _Resp({"transfer-encoding": "chunked"}, body=b"x" * 4_000_000)
    assert audits.response_bytes(chunked) == 4_000_000


def test_content_length_is_preferred_when_present():
    sized = _Resp({"content-length": "1234"}, body=b"ignored")
    assert audits.response_bytes(sized) == 1234


def test_an_unweighable_response_is_unknown_not_zero():
    """None, never 0: the caller must be able to tell 'nothing' from
    'unknown', because only one of those is safe to report as light."""
    assert audits.response_bytes(_Resp({}, body_raises=True)) is None


def test_a_heavy_chunked_page_now_trips_the_weight_finding():
    result = audits.performance_result_from_metrics(
        dom_node_count=10, resource_bytes=4_000_000, request_count=5
    )
    ids = {f["id"] for f in result["findings"]}
    assert "heavy-page-weight" in ids
    assert result["pass"] is False


def test_unmeasured_responses_are_reported_in_the_metrics():
    result = audits.performance_result_from_metrics(
        dom_node_count=10, resource_bytes=1000, request_count=5, unmeasured_responses=3
    )
    assert result["metrics"]["unmeasured_responses"] == 3


# --- presence is not protection -------------------------------------------


def _headers_response(headers, url="https://example.com/"):
    return type("R", (), {"headers": headers, "url": url, "status_code": 200})()


def test_hsts_max_age_zero_is_not_protection():
    """max-age=0 is the spec's own way to switch HSTS OFF and tell browsers to
    forget the pin. Testing only for the header name reported it as protected."""
    resp = _headers_response({"strict-transport-security": "max-age=0"})
    result = audits.run_security_audit("https://example.com", response=resp)
    ids = {f["id"] for f in result["findings"]}
    assert "hsts-disabled" in ids
    assert result["pass"] is False


def test_hsts_without_a_readable_max_age_is_flagged():
    resp = _headers_response({"strict-transport-security": "includeSubDomains"})
    result = audits.run_security_audit("https://example.com", response=resp)
    assert "invalid-hsts" in {f["id"] for f in result["findings"]}


def test_a_real_hsts_value_still_passes():
    resp = _headers_response({"strict-transport-security": "max-age=31536000; includeSubDomains"})
    result = audits.run_security_audit("https://example.com", response=resp)
    ids = {f["id"] for f in result["findings"]}
    assert "missing-hsts" not in ids and "hsts-disabled" not in ids and "invalid-hsts" not in ids


def test_x_frame_options_allowall_is_not_frame_protection():
    """Browsers ignore ALLOWALL and every unrecognised token, so the page is
    framable -- the same as sending no header at all."""
    resp = _headers_response({"x-frame-options": "ALLOWALL"})
    result = audits.run_security_audit("https://example.com", response=resp)
    assert "missing-frame-protection" in {f["id"] for f in result["findings"]}


def test_x_frame_options_deny_and_sameorigin_are_protection():
    for value in ("DENY", "sameorigin", " SAMEORIGIN "):
        resp = _headers_response({"x-frame-options": value})
        result = audits.run_security_audit("https://example.com", response=resp)
        assert "missing-frame-protection" not in {f["id"] for f in result["findings"]}, value
