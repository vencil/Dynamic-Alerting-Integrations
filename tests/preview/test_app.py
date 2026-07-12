"""Tests for the recipe-preview service (#657 PR 2).

The service logic is exercised through `handle_preview` with injected
authorizer/evaluator, so validation / authz / rate-limit / contract-passthrough
all run WITHOUT HTTP, network, or promtool. The PEP (`authorize_tenant`) is
tested with a mocked urlopen (fail-closed + identity-only forwarding). One
end-to-end test goes through the REAL eval core and is promtool-gated.
"""
import importlib.util
import os
import shutil
import urllib.error

import pytest

# Load components/recipe-preview/app.py under a UNIQUE module name so the
# generic name "app" can't collide with anything else in the pytest run.
_COMP = os.path.join(os.path.dirname(__file__), "..", "..", "components", "recipe-preview")
_spec = importlib.util.spec_from_file_location(
    "recipe_preview_app", os.path.join(_COMP, "app.py"))
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

_needs_promtool = pytest.mark.skipif(
    shutil.which("promtool") is None, reason="promtool not on PATH")

ALLOW = lambda headers, tenant: True       # noqa: E731
DENY = lambda headers, tenant: False       # noqa: E731
FAKE_EVAL = lambda recipe, tenant, scenario: {  # noqa: E731
    "alertname": "Custom_x", "supported": True,
    "states": [{"state": "firing", "reason": "value 1500 > threshold 1000"}],
    "warnings": [],
}
RECIPE = {"recipe": "threshold", "metric": "order_queue_depth", "op": ">",
          "window": "5m", "for": "1m", "threshold": "1000:warning", "name": "q"}
HDR = {"X-Forwarded-Email": "u@x", "X-Forwarded-Groups": "admins"}


def _preview(body, headers=HDR, **kw):
    kw.setdefault("authorizer", ALLOW)
    kw.setdefault("evaluator", FAKE_EVAL)
    return app.handle_preview(body, headers, **kw)


# ── input validation (no auth/eval needed) ──
class TestValidation:
    def test_non_dict_body(self):
        assert _preview([])[0] == 400

    def test_missing_recipe(self):
        s, r = _preview({"tenant": "shop-a"})
        assert s == 400 and "recipe" in r["error"]

    def test_missing_tenant(self):
        s, r = _preview({"recipe": RECIPE})
        assert s == 400 and "tenant" in r["error"]

    def test_bad_scenario_type(self):
        assert _preview({"recipe": RECIPE, "tenant": "shop-a", "scenario": 5})[0] == 400

    def test_falsy_nondict_scenario_is_400(self):
        # `or {}` would have coerced these falsy non-dicts past the type check.
        for bad in (0, "", False, []):
            assert _preview({"recipe": RECIPE, "tenant": "shop-a", "scenario": bad})[0] == 400, repr(bad)

    def test_oversized_tenant_is_400(self):
        assert _preview({"recipe": RECIPE, "tenant": "x" * 300, "scenario": {"value": 1}})[0] == 400


# ── identity + tenant-isolation (fail closed) ──
class TestAuthz:
    def test_missing_identity_is_401(self, monkeypatch):
        monkeypatch.setattr(app, "DEV_BYPASS", False)
        assert _preview({"recipe": RECIPE, "tenant": "shop-a"}, headers={})[0] == 401

    def test_denied_is_403(self):
        s, r = _preview({"recipe": RECIPE, "tenant": "shop-a"}, authorizer=DENY)
        assert s == 403 and "shop-a" in r["error"]

    def test_allowed_reaches_eval(self):
        s, r = _preview({"recipe": RECIPE, "tenant": "shop-a", "scenario": {"value": 1500}})
        assert s == 200 and r["states"][0]["state"] == "firing"

    def test_dev_bypass_injects_identity(self, monkeypatch):
        monkeypatch.setattr(app, "DEV_BYPASS", True)
        seen = {}

        def auth(headers, tenant):
            seen.update(headers)
            return True
        s, _ = _preview({"recipe": RECIPE, "tenant": "shop-a"}, headers={}, authorizer=auth)
        assert s == 200
        assert seen.get("X-Forwarded-Email") == app.DEV_BYPASS_EMAIL


# ── §6 per-tenant rate limit ──
class TestRateLimit:
    def test_per_tenant_limit_and_isolation(self, monkeypatch):
        monkeypatch.setattr(app, "_rate", app.RateLimiter(2))
        body = {"recipe": RECIPE, "tenant": "shop-a", "scenario": {"value": 1500}}
        assert _preview(body, now=100.0)[0] == 200
        assert _preview(body, now=100.0)[0] == 200
        assert _preview(body, now=100.0)[0] == 429            # 3rd in-window → limited
        assert _preview(dict(body, tenant="shop-b"), now=100.0)[0] == 200  # other tenant ok
        assert _preview(body, now=200.0)[0] == 200            # window slid → ok again

    def test_denied_request_does_not_consume_budget(self, monkeypatch):
        # Authz runs BEFORE the rate limit, so an UNAUTHORIZED request must NOT
        # decrement the victim tenant's budget (else cross-tenant rate-limit DoS).
        monkeypatch.setattr(app, "_rate", app.RateLimiter(1))
        body = {"recipe": RECIPE, "tenant": "shop-a", "scenario": {"value": 1500}}
        assert _preview(body, authorizer=DENY, now=100.0)[0] == 403   # denied
        assert _preview(body, authorizer=ALLOW, now=100.0)[0] == 200  # budget intact


class TestRateLimiterUnit:
    def test_sliding_window(self):
        rl = app.RateLimiter(2)
        assert rl.allow("k", 0.0) and rl.allow("k", 1.0)
        assert not rl.allow("k", 2.0)
        assert rl.allow("k", 61.0)        # the 0.0 hit aged out of the 60s window

    def test_disabled_when_zero(self):
        rl = app.RateLimiter(0)
        assert all(rl.allow("k", float(i)) for i in range(100))

    def test_expired_keys_are_gced_past_cap(self):
        # A new key past max_keys sweeps keys whose window fully expired, so the
        # map can't grow without bound from sprayed distinct tenants.
        rl = app.RateLimiter(5, max_keys=2)
        rl.allow("a", 0.0)
        rl.allow("b", 0.0)
        rl.allow("c", 100.0)                       # a,b expired (cutoff 40) → GC'd
        assert "a" not in rl._hits and "b" not in rl._hits and "c" in rl._hits

    def test_active_keys_survive_gc(self):
        rl = app.RateLimiter(5, max_keys=2)
        rl.allow("a", 90.0)                         # still in-window at t=100
        rl.allow("b", 0.0)                          # expired by t=100
        rl.allow("c", 100.0)
        assert "a" in rl._hits and "b" not in rl._hits


# ── §6 bounded concurrency (eval slots) ──
class TestEvalSlots:
    def test_exhausted_slots_return_503_and_do_not_release(self, monkeypatch):
        """Queue-full path: acquire times out → (503, busy), AND the finally must
        NOT release a slot it never acquired (a stray release would over-credit
        the BoundedSemaphore → concurrency cap silently widens). Pins the
        acquire-guarded try/finally in handle_preview."""
        import threading
        sem = threading.BoundedSemaphore(1)
        assert sem.acquire(blocking=False)              # exhaust the only slot
        monkeypatch.setattr(app, "_eval_slots", sem)
        monkeypatch.setattr(app, "QUEUE_TIMEOUT", 0.01)  # don't sit out 10s in tests
        s, r = _preview({"recipe": RECIPE, "tenant": "shop-a", "scenario": {"value": 1500}})
        assert s == 503 and "busy" in r["error"]
        # still exhausted: the failed request must not have released our hold
        assert sem.acquire(blocking=False) is False


# ── Content-Length guard (a negative value must not reach rfile.read) ──
class TestContentLength:
    def test_absent_is_zero(self):
        assert app.parse_content_length(None) == (None, 0)

    def test_valid_passes_through(self):
        assert app.parse_content_length("50") == (None, 50)

    def test_non_numeric_is_400(self):
        assert app.parse_content_length("abc")[0][0] == 400

    def test_negative_is_400_not_read_to_eof(self):
        # int("-1") would otherwise reach rfile.read(-1) → read to EOF, bypassing
        # the size cap (memory-exhaustion). It must be rejected up front.
        assert app.parse_content_length("-1")[0][0] == 400

    def test_oversized_is_413(self):
        assert app.parse_content_length(str(app.MAX_BODY_BYTES + 1))[0][0] == 413


# ── PEP: authorize_tenant must fail closed + forward only identity headers ──
class _Resp:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestPEPFailClosed:
    def test_200_allows(self, monkeypatch):
        monkeypatch.setattr(app.urllib.request, "urlopen", lambda req, timeout=None: _Resp(200))
        assert app.authorize_tenant(HDR, "shop-a") is True

    def test_403_denies(self, monkeypatch):
        def raise_403(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        monkeypatch.setattr(app.urllib.request, "urlopen", raise_403)
        assert app.authorize_tenant(HDR, "shop-a") is False

    def test_unreachable_fails_closed(self, monkeypatch):
        def boom(req, timeout=None):
            raise OSError("connection refused")
        monkeypatch.setattr(app.urllib.request, "urlopen", boom)
        assert app.authorize_tenant(HDR, "shop-a") is False

    def test_only_identity_headers_forwarded(self, monkeypatch):
        """Confused-deputy guard: arbitrary client headers are NOT relayed."""
        seen = {}

        def capture(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.headers.items()})
            return _Resp(200)
        monkeypatch.setattr(app.urllib.request, "urlopen", capture)
        app.authorize_tenant(
            {"X-Forwarded-Email": "u@x", "X-Forwarded-Groups": "g",
             "X-Evil": "1", "Cookie": "c", "Authorization": "Bearer z"}, "shop-a")
        assert "x-forwarded-email" in seen
        assert "x-evil" not in seen and "cookie" not in seen and "authorization" not in seen

    def test_org_claim_header_forwarded_when_configured(self, monkeypatch):
        """P4c: a configured org-claim header IS relayed so tenant-api's
        read-by-id gate can org-scope /access. Only the NAMED header — arbitrary
        client headers stay blocked (confused-deputy guard preserved)."""
        monkeypatch.setattr(app, "_CLAIM_HEADERS", ("X-Auth-Request-Org",))
        seen = {}

        def capture(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.headers.items()})
            return _Resp(200)
        monkeypatch.setattr(app.urllib.request, "urlopen", capture)
        app.authorize_tenant(
            {"X-Forwarded-Email": "u@x", "X-Forwarded-Groups": "g",
             "X-Auth-Request-Org": "ORG-ALPHA", "X-Evil": "1"}, "shop-a")
        assert seen.get("x-auth-request-org") == "ORG-ALPHA"
        assert "x-evil" not in seen

    def test_claim_header_absent_is_noop(self, monkeypatch):
        """A configured-but-absent claim header is not forwarded (no-op), and the
        default (no PREVIEW_CLAIM_HEADERS) forwards nothing extra."""
        monkeypatch.setattr(app, "_CLAIM_HEADERS", ("X-Auth-Request-Org",))
        seen = {}

        def capture(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.headers.items()})
            return _Resp(200)
        monkeypatch.setattr(app.urllib.request, "urlopen", capture)
        app.authorize_tenant({"X-Forwarded-Email": "u@x", "X-Forwarded-Groups": "g"}, "shop-a")
        assert "x-auth-request-org" not in seen

    def test_claim_headers_env_parsing(self):
        """PREVIEW_CLAIM_HEADERS parse: trim, drop empties, and strip reserved
        names (identity headers + Authorization/Cookie) so a misconfigured value
        can't smuggle a client-supplied Authorization/Cookie into the forward."""
        assert app._parse_claim_headers(
            " X-Auth-Request-Org , ,X-Foo,Authorization,Cookie,X-Forwarded-Email"
        ) == ("X-Auth-Request-Org", "X-Foo")
        assert app._parse_claim_headers("") == ()
        # case-insensitive reserved match
        assert app._parse_claim_headers("authorization, X-Ok") == ("X-Ok",)


class TestPEPFailClosedLogsReason:
    """R7: authorize_tenant no longer swallows the failure silently — it logs the
    ANOMALOUS cases so a self-hoster can tell an authz-backend problem (unreachable /
    tenant-api 5xx) apart from a genuine deny. A genuine 401/403 deny is NOT logged
    (it is expected and already shows as a 403 in the access log). Every log line
    carries tenant id / host / status / class ONLY — never the identity headers or
    the Bearer token."""

    def test_unreachable_logs_exception_class_and_host(self, monkeypatch, capsys):
        def boom(req, timeout=None):
            raise OSError("connection refused")
        monkeypatch.setattr(app.urllib.request, "urlopen", boom)
        assert app.authorize_tenant(HDR, "shop-a") is False
        err = capsys.readouterr().err
        assert "shop-a" in err and "OSError" in err and app.TENANT_API_URL in err
        assert "probe to tenant-api" in err and "failed" in err
        assert "connection refused" not in err   # str(exc) must NEVER be logged

    def test_non_deny_status_is_logged(self, monkeypatch, capsys):
        # anything other than a 401/403 deny is anomalous and must surface: a 5xx
        # (tenant-api fault) AND a 4xx like 404 (misconfigured tenant-api URL/route).
        for code in (503, 404):
            def raise_code(req, timeout=None, _c=code):
                raise urllib.error.HTTPError(req.full_url, _c, "x", {}, None)
            monkeypatch.setattr(app.urllib.request, "urlopen", raise_code)
            assert app.authorize_tenant(HDR, "shop-a") is False
            assert str(code) in capsys.readouterr().err, code

    def test_genuine_deny_403_is_not_logged(self, monkeypatch, capsys):
        # 401/403 = expected deny; the access log already records the 403, so
        # authorize_tenant stays quiet (no double-log, no deny-spam flooding).
        def raise_403(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
        monkeypatch.setattr(app.urllib.request, "urlopen", raise_403)
        assert app.authorize_tenant(HDR, "shop-a") is False
        assert capsys.readouterr().err == ""

    def test_unexpected_error_is_logged_neutrally_not_as_unreachable(self, monkeypatch, capsys):
        # a NON-network exception (a bug) must NOT be mislabeled "could not reach";
        # it fails closed, logs a neutral message + a traceback.
        def bug(req, timeout=None):
            raise TypeError("not a network error")
        monkeypatch.setattr(app.urllib.request, "urlopen", bug)
        assert app.authorize_tenant(HDR, "shop-a") is False
        err = capsys.readouterr().err
        assert "unexpected error" in err and "TypeError" in err
        assert "probe to tenant-api" not in err     # a bug must NOT read as a net fault
        # no-leak also holds on the traceback branch: identity headers never logged
        assert "u@x" not in err and "admins" not in err

    def test_log_never_leaks_identity_or_token(self, monkeypatch, capsys, tmp_path):
        """RED LINE: the reason log must never echo the forwarded email, groups, or
        Bearer token, nor str(exc) (which can carry a URL) — only tenant/host/status."""
        def boom(req, timeout=None):
            raise OSError("connection refused: secret-host-internal:8080")
        monkeypatch.setattr(app.urllib.request, "urlopen", boom)
        # a NON-empty token so the Bearer-attach path is actually exercised — the old
        # test set AUTH_TOKEN_FILE="" which made `"Bearer" not in err` vacuous.
        tok = tmp_path / "tok"
        tok.write_text("SEKRET-JWT-VALUE", encoding="utf-8")
        monkeypatch.setattr(app, "AUTH_TOKEN_FILE", str(tok))
        app.authorize_tenant(
            {"X-Forwarded-Email": "secret@corp.example",
             "X-Forwarded-Groups": "secret-admins"}, "shop-a")
        err = capsys.readouterr().err
        assert "secret@corp.example" not in err         # identity header value
        assert "secret-admins" not in err               # identity header value
        assert "SEKRET-JWT-VALUE" not in err            # Bearer token value
        assert "Bearer" not in err
        assert "secret-host-internal" not in err        # str(exc) content

    def test_genuine_deny_401_is_not_logged(self, monkeypatch, capsys):
        # the {401,403} silence exemption has TWO endpoints; pin 401 too (403 above).
        def raise_401(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)
        monkeypatch.setattr(app.urllib.request, "urlopen", raise_401)
        assert app.authorize_tenant(HDR, "shop-a") is False
        assert capsys.readouterr().err == ""

    def test_malformed_http_response_is_not_labeled_a_bug(self, monkeypatch, capsys):
        # a truncated/malformed tenant-api reply raises http.client.BadStatusLine, which
        # is NOT an OSError → it must be caught by the network/fault branch, NOT the
        # "unexpected bug" branch (which would print a misleading traceback).
        import http.client
        def raise_badstatus(req, timeout=None):
            raise http.client.BadStatusLine("garbage")
        monkeypatch.setattr(app.urllib.request, "urlopen", raise_badstatus)
        assert app.authorize_tenant(HDR, "shop-a") is False
        err = capsys.readouterr().err
        assert "probe to tenant-api" in err and "BadStatusLine" in err
        assert "unexpected error" not in err            # NOT the bug branch

    def test_crafted_tenant_cannot_inject_a_log_line(self, monkeypatch, capsys):
        # a tenant with a newline / ANSI escape must be `!r`-quoted so it cannot forge
        # a second log line or emit an active escape. The substring asserts elsewhere
        # pass with or without !r; THIS pins the escaping.
        def raise_503(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 503, "x", {}, None)
        monkeypatch.setattr(app.urllib.request, "urlopen", raise_503)
        assert app.authorize_tenant(HDR, "shop\nFAKE-INJECTED-LINE\x1b[31m") is False
        err = capsys.readouterr().err
        assert "\nFAKE-INJECTED-LINE" not in err         # no forged second line
        assert "\x1b[31m" not in err                     # no active ANSI escape
        assert "\\n" in err and "\\x1b" in err            # present but repr-escaped

    def test_logging_failure_does_not_break_control_flow(self, monkeypatch):
        # a logging call must NEVER turn a fail-closed deny into a raised exception:
        # even if stderr is broken (non-UTF-8 / closed pipe), authorize_tenant returns
        # False rather than propagating (the _safe_stderr swallow, end-to-end).
        class _BrokenStderr:
            def write(self, *a, **k):
                raise OSError("stderr is broken")
            def flush(self, *a, **k):
                raise OSError("stderr is broken")
        def boom(req, timeout=None):
            raise OSError("connection refused")
        monkeypatch.setattr(app.urllib.request, "urlopen", boom)
        monkeypatch.setattr(app.sys, "stderr", _BrokenStderr())
        assert app.authorize_tenant(HDR, "shop-a") is False   # no exception propagates


class TestDoPost500FailSafe:
    """The 500 handler must (a) always return a clean JSON 500 even when the request
    logic raises, and (b) log tenant + path ONLY — never the recipe body or identity
    headers. This whole do_POST branch had zero coverage before."""

    def test_500_is_fail_safe_and_logs_only_tenant_path(self, monkeypatch, capfd):
        import json as _json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer

        def boom(body, headers, **kw):
            raise RuntimeError("kaboom in handler")
        monkeypatch.setattr(app, "handle_preview", boom)

        srv = ThreadingHTTPServer(("127.0.0.1", 0), app._Handler)
        port = srv.server_address[1]
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        status, resp_body = None, None
        try:
            body = _json.dumps({"recipe": {"metric": "SECRET_METRIC_XYZ"},
                                "tenant": "shop-a"}).encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/preview", data=body, method="POST",
                headers={"X-Forwarded-Email": "leak@corp.example"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    status, resp_body = r.status, _json.loads(r.read())
            except urllib.error.HTTPError as e:
                status, resp_body = e.code, _json.loads(e.read())
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(timeout=5)

        assert status == 500                                 # fail-safe still responds
        assert resp_body == {"error": "internal error"}      # clean body, no traceback
        err = capfd.readouterr().err
        assert "shop-a" in err and "/preview" in err         # tenant + path logged
        assert "SECRET_METRIC_XYZ" not in err                # recipe body NEVER logged
        assert "leak@corp.example" not in err                # identity header NEVER logged


# ── machine-identity Bearer (#962 b2): audience-bound SA token → tenant-api audit ──
# The token feeds tenant-api's AUDIT only; it is orthogonal to the /access authz
# decision and reads FAIL-OPEN (unset/unreadable → no Bearer, never blocks).
class TestMachineIdentityToken:
    def test_unset_env_returns_empty(self, monkeypatch):
        monkeypatch.setattr(app, "AUTH_TOKEN_FILE", "")
        assert app._read_auth_token() == ""

    def test_present_file_returns_stripped_token(self, monkeypatch, tmp_path):
        p = tmp_path / "tenant-api-token"
        p.write_text("  eyJhbGciOiJSUzI1\n", encoding="utf-8")   # trailing WS stripped
        monkeypatch.setattr(app, "AUTH_TOKEN_FILE", str(p))
        assert app._read_auth_token() == "eyJhbGciOiJSUzI1"

    def test_unreadable_file_fails_open(self, monkeypatch, tmp_path):
        # Point at a path that does not exist → open() raises OSError → "" (no
        # exception propagates; the caller proceeds without a Bearer).
        monkeypatch.setattr(app, "AUTH_TOKEN_FILE", str(tmp_path / "nope"))
        assert app._read_auth_token() == ""

    def test_malformed_utf8_file_fails_open(self, monkeypatch, tmp_path):
        # A non-UTF-8 / corrupt token file makes .read() raise UnicodeDecodeError,
        # which is NOT an OSError subclass. The fail-open contract must still hold:
        # degrade to no Bearer, never crash this request-path PEP (a kubelet JWT is
        # always ASCII, but a mis-configured mount / partial write must not 500).
        p = tmp_path / "tenant-api-token"
        p.write_bytes(b"\xff\xfe not valid utf-8")
        monkeypatch.setattr(app, "AUTH_TOKEN_FILE", str(p))
        assert app._read_auth_token() == ""

    def test_authorize_attaches_bearer_when_token_present(self, monkeypatch, tmp_path):
        p = tmp_path / "tenant-api-token"
        p.write_text("tok-123", encoding="utf-8")
        monkeypatch.setattr(app, "AUTH_TOKEN_FILE", str(p))
        seen = {}

        def capture(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.headers.items()})
            return _Resp(200)
        monkeypatch.setattr(app.urllib.request, "urlopen", capture)
        assert app.authorize_tenant(HDR, "shop-a") is True
        assert seen.get("authorization") == "Bearer tok-123"
        # the identity headers are still forwarded alongside the Bearer
        assert seen.get("x-forwarded-email") == "u@x"

    def test_no_bearer_when_token_unset(self, monkeypatch):
        # Default state (no mounted token): authz still works, no Authorization sent.
        monkeypatch.setattr(app, "AUTH_TOKEN_FILE", "")
        seen = {}

        def capture(req, timeout=None):
            seen.update({k.lower(): v for k, v in req.headers.items()})
            return _Resp(200)
        monkeypatch.setattr(app.urllib.request, "urlopen", capture)
        assert app.authorize_tenant(HDR, "shop-a") is True
        assert "authorization" not in seen

    def test_fail_closed_unchanged_regardless_of_token(self, monkeypatch, tmp_path):
        # REGRESSION PIN: a token-read never flips the authz result. tenant-api
        # returning 403 / raising → authorize_tenant STILL denies (fail-closed),
        # with OR without a mounted Bearer.
        p = tmp_path / "tenant-api-token"
        p.write_text("tok-123", encoding="utf-8")
        for token_path in (str(p), ""):    # token present, then absent
            monkeypatch.setattr(app, "AUTH_TOKEN_FILE", token_path)

            def raise_403(req, timeout=None):
                raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, None)
            monkeypatch.setattr(app.urllib.request, "urlopen", raise_403)
            assert app.authorize_tenant(HDR, "shop-a") is False, token_path

            def boom(req, timeout=None):
                raise OSError("connection refused")
            monkeypatch.setattr(app.urllib.request, "urlopen", boom)
            assert app.authorize_tenant(HDR, "shop-a") is False, token_path


# ── end-to-end through the REAL eval core (promtool-gated) ──
@_needs_promtool
class TestEndToEnd:
    def test_threshold_fires_through_real_core(self):
        # default evaluator = core.preview_recipe → real compiler + promtool
        s, r = app.handle_preview(
            {"recipe": RECIPE, "tenant": "shop-a", "scenario": {"value": 1500}},
            HDR, authorizer=ALLOW)
        assert s == 200 and r["states"][0]["state"] == "firing"

    def test_unsupported_type_is_supported_false(self):
        rate = {"recipe": "rate", "metric": "http_requests_total", "op": ">",
                "window": "5m", "threshold": "1:warning", "name": "r"}
        s, r = app.handle_preview(
            {"recipe": rate, "tenant": "shop-a", "scenario": {"value": 5}},
            HDR, authorizer=ALLOW)
        assert s == 200 and r["supported"] is False


# ── ADR-022-style containment: dev-bypass must refuse to start inside K8s ──
def test_dev_bypass_poison_pill_refuses_to_start_in_k8s(monkeypatch):
    """app.py's import-time guard: PREVIEW_DEV_BYPASS_AUTH on + KUBERNETES_SERVICE_HOST
    present → SystemExit at module exec, so a direct-to-pod caller can never be
    auto-injected the demo admin identity. The no-op branch (bypass off / not in
    K8s) is implicitly pinned by this file's own top-level exec_module succeeding
    on every run."""
    monkeypatch.setenv("PREVIEW_DEV_BYPASS_AUTH", "1")
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    spec = importlib.util.spec_from_file_location(
        "recipe_preview_app_poison_pill", os.path.join(_COMP, "app.py"))
    mod = importlib.util.module_from_spec(spec)
    with pytest.raises(SystemExit, match="must not be enabled inside Kubernetes"):
        spec.loader.exec_module(mod)


# ── HTTP layer: /healthz reports build provenance (no promtool needed) ──
def test_healthz_reports_promtool_and_git_sha():
    """GET /healthz → 200 {status, promtool, git_sha}. git_sha echoes the image's
    GIT_SHA build-arg (drift observability, PR-D2); defaults to "unknown" locally."""
    import json
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), app._Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/healthz", timeout=5) as resp:
            assert resp.status == 200
            body = json.loads(resp.read())
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=5)

    assert body["status"] == "ok"
    assert "promtool" in body
    assert body["git_sha"] == app._GIT_SHA   # module reads env GIT_SHA, default "unknown"
