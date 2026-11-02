"""Operator tools count threshold-exporter rows once, however many replicas serve them (#3483).

threshold-exporter runs several replicas and the platform's Prometheus scrapes
every pod, so each user_threshold / user_state_filter row arrives once per
replica, differing only in instance / pod. A tool that counts or lists those
series raw reports the replica count times the truth.

DRIFT-PROOF: the queries are the tools' own (module constants / builders),
run by the real Prometheus engine (promtool) against a two-replica fixture.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest
import yaml

_OPS = os.path.join(os.path.dirname(__file__), "..", "..", "scripts", "tools", "ops")
sys.path.insert(0, _OPS)

import byo_check as bc  # noqa: E402
import cutover_tenant as ct  # noqa: E402
import federation_check as fc  # noqa: E402
import shadow_verify as sv  # noqa: E402

_PROMTOOL = shutil.which("promtool")


def _replicated(series: str) -> list[dict]:
    """*series* as two exporter replicas serve it."""
    return [{"series": series[:-1] + f', instance="10.0.0.{i}:8080", pod="threshold-exporter-{i}"}}',
             "values": "1x10"} for i in (1, 2)]


_INPUT = (
    _replicated('user_threshold{tenant="t1", component="mysql", metric="connections", severity="warning"}')
    + _replicated('user_threshold{tenant="t1", component="mysql", metric="cpu", severity="warning"}')
    + _replicated('user_threshold{tenant="t2", component="mysql", metric="connections", severity="warning"}')
    + _replicated('user_state_filter{tenant="t1", filter="maintenance"}')
)

_CASES = [
    ("byo_check THRESHOLD_ROWS_QUERY", bc.THRESHOLD_ROWS_QUERY, [("{}", 3)]),
    ("federation_check THRESHOLD_ROWS_QUERY", fc.THRESHOLD_ROWS_QUERY, [("{}", 3)]),
    ("cutover_tenant threshold_rows_query", ct.threshold_rows_query("t1"), [("{}", 2)]),
    ("shadow_verify MAINTENANCE_QUERY", sv.MAINTENANCE_QUERY,
     [('{tenant="t1", filter="maintenance"}', 1)]),
]


@pytest.mark.skipif(_PROMTOOL is None, reason="promtool not on PATH")
def test_tool_queries_count_rows_not_replicas(tmp_path):
    doc = {
        "evaluation_interval": "15s",
        "tests": [{
            "interval": "15s",
            "input_series": _INPUT,
            "promql_expr_test": [
                {"expr": q, "eval_time": "1m",
                 "exp_samples": [{"labels": lbl, "value": v} for lbl, v in exp]}
                for _name, q, exp in _CASES
            ],
        }],
    }
    f = tmp_path / "replica_queries_test.yaml"
    f.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    r = subprocess.run([_PROMTOOL, "test", "rules", str(f)], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=60)
    assert r.returncode == 0, f"{[c[0] for c in _CASES]}\n{r.stdout}\n{r.stderr}"
