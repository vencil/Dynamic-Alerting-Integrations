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
