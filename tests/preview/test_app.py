"""Tests for the recipe-preview service (ADR-027 §5).

recipe-preview runs inside the tenant-api pod and answers only tenant-api, on a
pod-internal Unix socket (the generator container's pattern, ADR-036 §3):
tenant-api authorizes the caller first and then asks for the computation.
So the service reads no identity and asks nobody about access; what it still
owns is protecting the computation — input validation, a per-tenant rate
limit, a bounded number of concurrent evaluations — and the caller's deadline:
it never queues, or answers with a verdict, past the `deadline_ms` tenant-api
sends.

The request logic is exercised through `handle_preview` with an injected
evaluator and clock (no socket, no promtool); the socket layer through a real
Unix socket in a temp dir; one end-to-end test goes through the REAL eval core
and is promtool-gated.
"""
import functools
import http.client
import importlib.util
import json
import os
import shutil
import socket
import stat
import threading
import time

import pytest

# Load components/recipe-preview/app.py under a UNIQUE module name so the
# generic name "app" can't collide with anything else in the pytest run.
_COMP = os.path.join(os.path.dirname(__file__), "..", "..", "components", "recipe-preview")
_APP_PY = os.path.join(_COMP, "app.py")
_spec = importlib.util.spec_from_file_location("recipe_preview_app", _APP_PY)
app = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(app)

_needs_promtool = pytest.mark.skipif(
    shutil.which("promtool") is None, reason="promtool not on PATH")

CALLS = []


def FAKE_EVAL(recipe, tenant, scenario, deadline=None):
    CALLS.append(deadline)
    return {"alertname": "Custom_x", "supported": True,
            "states": [{"state": "firing", "reason": "value 1500 > threshold 1000"}],
            "warnings": []}


RECIPE = {"recipe": "threshold", "metric": "order_queue_depth", "op": ">",
          "window": "5m", "for": "1m", "threshold": "1000:warning", "name": "q"}


def _body(**over):
    b = {"recipe": RECIPE, "tenant": "shop-a", "scenario": {"value": 1500}, "deadline_ms": 5000}
    b.update(over)
    return b


def _preview(body, **kw):
    kw.setdefault("evaluator", FAKE_EVAL)
    return app.handle_preview(body, **kw)


@pytest.fixture(autouse=True)
def _fresh_limits(monkeypatch):
    CALLS.clear()
    monkeypatch.setattr(app, "_rate", app.RateLimiter(1000))
    monkeypatch.setattr(app, "_eval_slots", threading.BoundedSemaphore(4))


# ── input validation ──
class TestValidation:
    def test_non_dict_body(self):
        assert _preview([])[0] == 400

    def test_missing_recipe(self):
        s, r = _preview(_body(recipe=None))
        assert s == 400 and "recipe" in r["error"]

    def test_missing_tenant(self):
        s, r = _preview(_body(tenant=None))
        assert s == 400 and "tenant" in r["error"]

    def test_bad_scenario_type(self):
        assert _preview(_body(scenario=5))[0] == 400

    def test_falsy_nondict_scenario_is_400(self):
        # `or {}` would have coerced these falsy non-dicts past the type check.
        for bad in (0, "", False, []):
            assert _preview(_body(scenario=bad))[0] == 400, repr(bad)

    def test_oversized_tenant_is_400(self):
        assert _preview(_body(tenant="x" * 300))[0] == 400

    @pytest.mark.parametrize("bad", [None, 0, -1, "5000", 1.5, True, "max+1"])
    def test_deadline_ms_must_be_a_positive_bounded_integer(self, bad):
        body = _body()
        if bad is None:
            del body["deadline_ms"]
        elif bad == "max+1":
            body["deadline_ms"] = app.MAX_DEADLINE_MS + 1
        else:
            body["deadline_ms"] = bad
        s, r = _preview(body)
        assert s == 400 and "deadline_ms" in r["error"], (bad, r)
        assert CALLS == []


# ── no identity: tenant-api decides access before it calls ──
class TestNoIdentity:
    def test_the_service_reads_no_identity_and_calls_nobody(self):
        src = open(_APP_PY, encoding="utf-8").read()
        for gone in ("X-Forwarded", "urllib.request", "TENANT_API", "authorize_tenant",
                     "DEV_BYPASS", "AUTH_TOKEN", "CLAIM_HEADERS", "LISTEN_PORT"):
            assert gone not in src, gone

    def test_handle_preview_takes_no_headers(self):
        assert _preview(_body())[0] == 200
        with pytest.raises(TypeError):
            app.handle_preview(_body(), {"X-Forwarded-Email": "a@b"}, evaluator=FAKE_EVAL)


# ── per-tenant rate limit ──
class TestRateLimit:
    def test_per_tenant_limit_and_isolation(self, monkeypatch):
        monkeypatch.setattr(app, "_rate", app.RateLimiter(2))
        assert _preview(_body(), now=100.0)[0] == 200
        assert _preview(_body(), now=100.0)[0] == 200
        assert _preview(_body(), now=100.0)[0] == 429            # 3rd in-window → limited
        assert _preview(_body(tenant="shop-b"), now=100.0)[0] == 200  # other tenant ok
        assert _preview(_body(), now=200.0)[0] == 200            # window slid → ok again


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


# ── queue and deadline semantics ──
class TestQueueAndDeadline:
    """The caller's deadline bounds everything: the wait for an evaluation
    slot is min(QUEUE_TIMEOUT, time left), the evaluation is handed the
    absolute deadline (each promtool / da-guard run is bounded by it), and a
    verdict that arrives at or after the deadline is not sent as a verdict —
    tenant-api has stopped waiting, and a run cut short reads as `error`."""

    def _exhaust(self, monkeypatch):
        sem = threading.BoundedSemaphore(1)
        assert sem.acquire(blocking=False)
        monkeypatch.setattr(app, "_eval_slots", sem)
        return sem

    def test_queue_full_before_the_deadline_is_busy_and_releases_nothing(self, monkeypatch):
        sem = self._exhaust(monkeypatch)
        monkeypatch.setattr(app, "QUEUE_TIMEOUT", 0.05)
        t0 = time.monotonic()
        s, r = _preview(_body(deadline_ms=5000))
        assert s == 503 and r["reason"] == "busy", r
        assert time.monotonic() - t0 < 1.0
        assert sem.acquire(blocking=False) is False   # no stray release

    def test_queue_wait_stops_at_the_deadline(self, monkeypatch):
        self._exhaust(monkeypatch)
        monkeypatch.setattr(app, "QUEUE_TIMEOUT", 30)
        t0 = time.monotonic()
        s, r = _preview(_body(deadline_ms=100))
        waited = time.monotonic() - t0
        assert s == 503 and r["reason"] == "deadline", r
        assert waited < 2.0, waited                   # not QUEUE_TIMEOUT's 30s
        assert CALLS == []

    def test_the_evaluator_gets_the_absolute_deadline(self):
        clock = iter([10.0, 10.0, 10.5]).__next__
        s, _ = _preview(_body(deadline_ms=2000), clock=clock)
        assert s == 200
        assert CALLS == [pytest.approx(12.0)]

    def test_a_verdict_after_the_deadline_is_not_sent(self):
        # start 10.0 → deadline 12.0; the evaluation ends at 12.5
        clock = iter([10.0, 10.0, 12.5]).__next__
        s, r = _preview(_body(deadline_ms=2000), clock=clock)
        assert s == 503 and r["reason"] == "deadline", r

    def test_a_slot_is_released_after_the_evaluation(self, monkeypatch):
        sem = threading.BoundedSemaphore(1)
        monkeypatch.setattr(app, "_eval_slots", sem)
        assert _preview(_body())[0] == 200
        assert sem.acquire(blocking=False) is True


# ── Content-Length guard (a negative value must not reach rfile.read) ──
class TestContentLength:
    def test_absent_is_zero(self):
        assert app.parse_content_length(None) == (None, 0)

    def test_valid_passes_through(self):
        assert app.parse_content_length("50") == (None, 50)

    def test_non_numeric_is_400(self):
        assert app.parse_content_length("abc")[0][0] == 400

    def test_negative_is_400_not_read_to_eof(self):
        assert app.parse_content_length("-1")[0][0] == 400

    def test_oversized_is_413(self):
        assert app.parse_content_length(str(app.MAX_BODY_BYTES + 1))[0][0] == 413


# ── the Unix socket ──
class _UDSConnection(http.client.HTTPConnection):
    def __init__(self, path):
        super().__init__("localhost", timeout=5)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(self._path)


_REAL_HANDLE = app.handle_preview


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "handle_preview", functools.partial(_REAL_HANDLE, evaluator=FAKE_EVAL))
    path = str(tmp_path / "preview.sock")
    srv = app.make_server(path)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv, path
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)


def _request(path, method, target, body=None):
    conn = _UDSConnection(path)
    try:
        payload = None if body is None else json.dumps(body).encode()
        conn.request(method, target, body=payload,
                     headers={"Content-Type": "application/json"} if payload else {})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"null")
    finally:
        conn.close()


class TestSocket:
    def test_post_preview_answers_over_the_socket(self, server):
        _, path = server
        s, r = _request(path, "POST", "/v1/preview", _body())
        assert s == 200 and r["states"][0]["state"] == "firing"

    def test_the_socket_is_owner_only(self, server):
        _, path = server
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode & 0o077 == 0, oct(mode)

    def test_status_and_unknown_paths(self, server):
        _, path = server
        s, r = _request(path, "GET", "/v1/status")
        assert s == 200 and r["status"] == "ok" and "promtool" in r and "git_sha" in r
        assert _request(path, "GET", "/healthz")[0] == 404
        assert _request(path, "POST", "/preview", _body())[0] == 404

    def test_a_peer_with_another_uid_is_closed_unanswered(self, server, monkeypatch):
        srv, path = server
        monkeypatch.setattr(app, "peer_uid", lambda sock: os.getuid() + 1)
        with pytest.raises((http.client.RemoteDisconnected, ConnectionError, http.client.BadStatusLine)):
            _request(path, "GET", "/v1/status")
        assert srv.peer_rejected >= 1

    def test_an_existing_non_socket_path_is_not_replaced(self, tmp_path):
        p = tmp_path / "not-a-socket"
        p.write_text("x", encoding="utf-8")
        with pytest.raises(RuntimeError, match="not a socket"):
            app.make_server(str(p))
        assert p.read_text(encoding="utf-8") == "x"

    def test_a_live_socket_is_not_taken_over(self, server):
        _, path = server
        with pytest.raises(RuntimeError, match="served by another process"):
            app.make_server(path)
        assert _request(path, "GET", "/v1/status")[0] == 200   # still the first one

    def test_a_stale_socket_is_replaced(self, tmp_path):
        path = str(tmp_path / "stale.sock")
        s = socket.socket(socket.AF_UNIX)
        s.bind(path)
        s.close()                       # bound, nobody accepting: stale
        srv = app.make_server(path)
        srv.server_close()
        assert not os.path.exists(path)

    def test_closing_does_not_remove_a_socket_it_did_not_create(self, tmp_path):
        path = str(tmp_path / "p.sock")
        first = app.make_server(path)
        os.unlink(path)                 # an operator, or another instance, rebinds the path
        second = app.make_server(path)
        first.server_close()
        assert os.path.exists(path)
        second.server_close()
        assert not os.path.exists(path)

    def test_500_is_fail_safe_and_logs_only_tenant_path(self, server, monkeypatch, capfd):
        _, path = server

        def boom(body, **kw):
            raise RuntimeError("kaboom in handler")
        monkeypatch.setattr(app, "handle_preview", boom)
        s, r = _request(path, "POST", "/v1/preview",
                        _body(recipe={"metric": "SECRET_METRIC_XYZ"}))
        assert s == 500 and r == {"error": "internal error"}
        err = capfd.readouterr().err
        assert "shop-a" in err and "/v1/preview" in err
        assert "SECRET_METRIC_XYZ" not in err


# ── end-to-end through the REAL eval core (promtool-gated) ──
@_needs_promtool
class TestEndToEnd:
    def test_threshold_fires_through_real_core(self):
        s, r = app.handle_preview(_body(deadline_ms=120000))
        assert s == 200 and r["states"][0]["state"] == "firing"

    def test_unsupported_type_is_supported_false(self):
        rate = {"recipe": "rate", "metric": "http_requests_total", "op": ">",
                "window": "5m", "threshold": "1:warning", "name": "r"}
        s, r = app.handle_preview(_body(recipe=rate, scenario={"value": 5}, deadline_ms=120000))
        assert s == 200 and r["supported"] is False
