"""test_projection_gate_exposer.py — the #908 PR-3a metrics-exposer sidecar
(helm/vector/projection-gate/serve_metrics.py).

The exposer re-serves the init-container's verdict textfile over HTTP so Prometheus
can scrape it (this cluster has no node-exporter textfile collector). These tests
pin the contract that matters for the alert rules:
  - a present verdict file is served verbatim (the degrade alert keys on this body);
  - an ABSENT/unreadable file yields a successful scrape with NO verdict series (the
    init has not written yet) — NOT a 5xx and NOT a fabricated "ok";
  - the file is RE-READ per request (a pod-restart's fresh verdict shows without
    restarting the sidecar).
It does NOT import the validator and never re-evaluates the gate (one-shot-at-boot).
"""
from __future__ import annotations

import importlib.util
import sys
import threading
import urllib.request
from http.server import HTTPServer
from pathlib import Path

import pytest

_EXPOSER = Path(__file__).parent.parent.parent / "helm" / "vector" / "projection-gate" / "serve_metrics.py"


def _load_exposer():
    spec = importlib.util.spec_from_file_location("serve_metrics", _EXPOSER)
    assert spec and spec.loader, f"cannot load exposer module at {_EXPOSER}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


exposer = _load_exposer()

_SAMPLE = (
    "# HELP vector_tenant_projection_gate_info ...\n"
    "# TYPE vector_tenant_projection_gate_info gauge\n"
    'vector_tenant_projection_gate_info{category="mismatch",mode="degrade"} 1\n'
)


def test_exposer_module_present():
    assert _EXPOSER.is_file(), f"exposer script missing at {_EXPOSER}"
    assert _EXPOSER.stat().st_size > 0


def test_read_metrics_present(tmp_path: Path):
    p = tmp_path / "gate.prom"
    p.write_text(_SAMPLE, encoding="utf-8", newline="\n")
    assert exposer._read_metrics(p) == _SAMPLE.encode("utf-8")


def test_read_metrics_absent_yields_no_series(tmp_path: Path):
    """An absent verdict file must NOT raise and must NOT fabricate a verdict — it
    returns a comment-only body so a scrape succeeds with zero gate series."""
    body = exposer._read_metrics(tmp_path / "does-not-exist.prom").decode("utf-8")
    assert "vector_tenant_projection_gate_info" not in body, "must not fabricate a verdict series"
    assert body.startswith("#"), "absent → a Prometheus comment, not an error"


@pytest.fixture()
def _served(tmp_path: Path):
    """Start the real single-threaded HTTPServer the sidecar runs (see serve_metrics.py:
    single-threaded, fixed-footprint, on purpose), on an ephemeral port, serving
    tmp_path/gate.prom. Yields (base_url, metrics_file_path)."""
    metrics_file = tmp_path / "gate.prom"
    server = HTTPServer(("127.0.0.1", 0), exposer._make_handler(metrics_file))
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    host, port = server.server_address
    try:
        yield f"http://127.0.0.1:{port}", metrics_file
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - localhost test server
        return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8")


def test_server_serves_present_verdict(_served):
    base, metrics_file = _served
    metrics_file.write_text(_SAMPLE, encoding="utf-8", newline="\n")
    status, ctype, body = _get(f"{base}/metrics")
    assert status == 200
    assert "text/plain" in ctype
    assert body == _SAMPLE


def test_server_serves_at_any_path(_served):
    """Served at any path so a prometheus.io/path mismatch still exposes the data."""
    base, metrics_file = _served
    metrics_file.write_text(_SAMPLE, encoding="utf-8", newline="\n")
    assert _get(f"{base}/")[2] == _SAMPLE
    assert _get(f"{base}/anything")[2] == _SAMPLE


def test_server_absent_file_is_200_with_no_series(_served):
    base, _metrics_file = _served  # file deliberately not written
    status, _ctype, body = _get(f"{base}/metrics")
    assert status == 200, "absent verdict must be a healthy scrape, not a 5xx"
    assert "vector_tenant_projection_gate_info" not in body


def test_server_rereads_file_each_request(_served):
    """A pod restart writes a fresh verdict; the long-lived sidecar must reflect it
    without a restart (it re-reads per request, no caching)."""
    base, metrics_file = _served
    metrics_file.write_text(_SAMPLE, encoding="utf-8", newline="\n")
    assert "mismatch" in _get(f"{base}/metrics")[2]
    metrics_file.write_text(
        'vector_tenant_projection_gate_info{category="ok",mode="degrade"} 1\n', encoding="utf-8", newline="\n")
    body = _get(f"{base}/metrics")[2]
    assert 'category="ok"' in body and "mismatch" not in body
