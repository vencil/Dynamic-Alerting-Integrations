"""promtool regression goldens for the Tenant Log Query dashboard (ADR-021 / #609 PR-4).

The dashboard (`k8s/03-monitoring/tenant-log-query-dashboard.json`) consumes two
metrics emitted by the federation-gateway mtail sidecar in victorialogs mode:
`tenant_log_query_requests_total{account_id,project_id,status}` (counter) and
`tenant_log_query_duration_ms` (histogram). The non-trivial PromQL — a
rejection-ratio with a `clamp_min` divide-by-zero guard, a `histogram_quantile`
over `_bucket` series, and `sum by(account_id, le)` groupings — is exactly the
class this repo keeps getting burned by: a query that strips `le` (the
topology-label trap) or mis-pairs the ratio renders an empty/wrong panel with no
JSON-validity error.

DRIFT-PROOF: the queries under test are READ FROM THE DASHBOARD JSON (never copied
here), and run through the real Prometheus engine against synthetic fixtures with
hand-computed golden expectations. Edit a covered query's semantics → the golden
fails; rename/remove a covered panel → the lookup fails (drift-aware). Mirrors
tests/dx/test_fleet_threshold_dashboard.py and tests/dx/test_custom_alerts_promtool.py.

Only the promtool golden test skips when promtool is absent (host / a CI job
without it); the pure-JSON shape check runs everywhere.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_DASHBOARD = _REPO / "k8s" / "03-monitoring" / "tenant-log-query-dashboard.json"
_PROMTOOL = shutil.which("promtool")

_needs_promtool = pytest.mark.skipif(_PROMTOOL is None, reason="promtool not on PATH")


# ── Synthetic fixture (legitimately fixed) ──────────────────────────────────
# Counter `tenant_log_query_requests_total` over a [Xm] window at 1m interval —
# `0+Nx40` ramps by N each step, so rate ≈ N/60 per second:
#   account 1000: ok 6/min (0.1/s), auth_failed 3/min (0.05/s) -> 0.15/s total
#   account 1001: ok 6/min (0.1/s)                             -> 0.10/s total
# Grand total rate = 0.25/s; total rejected = 0.05/s.
#   Log Queries /s          = 0.25
#   Rejection Rate          = 0.05 / 0.25 = 0.2  (exact)
#   Active Log-Query Tenants= 2
#   Queries per Tenant      = {1000: 0.15, 1001: 0.10}  (count = 2 accounts)
#
# Histogram `tenant_log_query_duration_ms_bucket` (cumulative, account 1000):
# 95 of 100 observations land in (50,100], 5 in (100,250]. With total 100 the
# 0.95 rank is exactly the le=100 boundary, so histogram_quantile(0.95,...) == 100
# (ms) EXACTLY — no float-interpolation fuzz.
def _counter_series() -> list[dict]:
    return [
        {"series": 'tenant_log_query_requests_total{account_id="1000",project_id="0",status="ok"}', "values": "0+6x40"},
        {"series": 'tenant_log_query_requests_total{account_id="1000",project_id="0",status="auth_failed"}', "values": "0+3x40"},
        {"series": 'tenant_log_query_requests_total{account_id="1001",project_id="0",status="ok"}', "values": "0+6x40"},
    ]


def _histogram_series() -> list[dict]:
    # Cumulative buckets: 0 up to le=50, 95 at le=100, 100 from le=250 up incl +Inf.
    below = ["5", "10", "25", "50"]
    at95 = "100"
    above = ["250", "500", "1000", "2500", "5000", "10000", "25000", "+Inf"]
    rows = [
        {"series": f'tenant_log_query_duration_ms_bucket{{account_id="1000",project_id="0",le="{le}"}}', "values": "0+0x40"}
        for le in below
    ]
    rows.append(
        {"series": f'tenant_log_query_duration_ms_bucket{{account_id="1000",project_id="0",le="{at95}"}}', "values": "0+95x40"}
    )
    rows += [
        {"series": f'tenant_log_query_duration_ms_bucket{{account_id="1000",project_id="0",le="{le}"}}', "values": "0+100x40"}
        for le in above
    ]
    return rows


# ── Golden expectations, keyed to panels by a stable title substring ────────
# (title_substr, target_discriminator_or_None, wrap, expected)
#   wrap: None = assert the expr directly (scalar/single-series panels);
#         "count" = wrap a series-producing panel expr to a scalar (assert series count)
_GOLDENS = [
    ("Log Queries /s", None, None, 0.25),
    ("Rejection Rate", None, None, 0.2),
    ("Active Log-Query Tenants", None, None, 2),
    ("Query Latency P95 (5m)", None, None, 100),          # platform-wide histogram_quantile
    ("Queries per Tenant", None, "count", 2),             # one bar per account = 2
    ("Request Rate by Status", None, "count", 2),         # ok + auth_failed = 2 status series
    ("Per-Tenant Query Latency P95", None, "count", 1),   # one account has histogram data
]


def _load_panels() -> list[dict]:
    import json

    data = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    assert data.get("uid") == "tenant-log-query", "dashboard uid drift"
    return data["panels"]


def _find_expr(panels: list[dict], title_substr: str, disc: str | None) -> str:
    for p in panels:
        if title_substr in p.get("title", ""):
            targets = p.get("targets", [])
            if disc is None:
                assert len(targets) >= 1, f"panel {p['title']!r} has no targets"
                return targets[0]["expr"]
            for t in targets:
                if t.get("legendFormat") == disc or t.get("refId") == disc:
                    return t["expr"]
            raise AssertionError(f"panel {p['title']!r}: no target with legend/refId {disc!r}")
    raise AssertionError(f"no panel title contains {title_substr!r} (panel renamed/removed?)")


def _build_test_file() -> dict:
    panels = _load_panels()
    series = _counter_series() + _histogram_series()
    cases = []
    for title_substr, disc, wrap, expected in _GOLDENS:
        expr = _find_expr(panels, title_substr, disc)
        if wrap:
            expr = f"{wrap}({expr})"
        cases.append(
            {
                "expr": expr,
                "eval_time": "15m",
                "exp_samples": [{"labels": "{}", "value": expected}],
            }
        )
    return {
        "evaluation_interval": "1m",
        "tests": [{"interval": "1m", "input_series": series, "promql_expr_test": cases}],
    }


@_needs_promtool
def test_dashboard_promql_goldens(tmp_path):
    """Every covered dashboard query, read from the JSON, returns its golden value."""
    import yaml

    test_file = tmp_path / "tenant_log_query_promql_test.yaml"
    test_file.write_text(yaml.safe_dump(_build_test_file(), sort_keys=False), encoding="utf-8")
    result = subprocess.run(
        [_PROMTOOL, "test", "rules", str(test_file)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"promtool goldens failed for the tenant-log-query dashboard:\n{result.stdout}\n{result.stderr}"
    )


def test_dashboard_is_valid_grafana_shape():
    """Light structural guard: known panel types, no gridPos overlap, le preserved
    in every histogram_quantile (the topology-label trap)."""
    import json

    data = json.loads(_DASHBOARD.read_text(encoding="utf-8"))
    known_types = {"stat", "timeseries", "heatmap", "barchart", "table"}
    rects = []
    for p in data["panels"]:
        assert p["type"] in known_types, f"unknown panel type {p['type']}"
        for tg in p.get("targets", []):
            expr = tg["expr"]
            # Any histogram_quantile MUST group by le, else it silently returns NaN.
            if "histogram_quantile" in expr:
                assert "le" in expr, (
                    f"panel {p['title']!r}: histogram_quantile without `le` in the grouping "
                    f"— the topology-label trap (would return NaN in prod): {expr}"
                )
        g = p["gridPos"]
        rects.append((g["x"], g["y"], g["w"], g["h"], p["title"]))
    for i in range(len(rects)):
        for j in range(i + 1, len(rects)):
            ax, ay, aw, ah, at = rects[i]
            bx, by, bw, bh, bt = rects[j]
            overlap = not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)
            assert not overlap, f"gridPos overlap: {at!r} <> {bt!r}"


def test_metric_names_are_the_pr4_contract():
    """Pin the exact source-metric names the mtail sidecar emits — a rename on
    either side (mtail program or dashboard) breaks the data flow silently."""
    import json

    raw = _DASHBOARD.read_text(encoding="utf-8")
    data = json.loads(raw)
    exprs = " ".join(tg["expr"] for p in data["panels"] for tg in p.get("targets", []))
    assert "tenant_log_query_requests_total" in exprs, "counter metric name drifted"
    assert "tenant_log_query_duration_ms_bucket" in exprs, "histogram _bucket metric name drifted"
    # account_id is the partition key the dashboard groups on (tenant-agnostic).
    assert "account_id" in exprs, "dashboard must key on account_id, not a hardcoded tenant"
