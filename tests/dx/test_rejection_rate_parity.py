"""Rejection-rate panels count the same statuses as their anomaly alerts (#2482).

The "Rejection Rate (5m)" stat on the Federation Audit and Tenant Log Query
dashboards and the ``*RejectionRateAnomaly`` alerts answer the same question —
"what fraction of requests did the platform reject?" — so they must agree on
which ``status`` values count as a rejection. They drifted once: the panels
also counted ``backend_error`` (a storage 5xx, alerted separately as a platform
fault) while the alerts did not, and the docs said the two were aligned.

Only the status SET is pinned. Window, aggregation and threshold legitimately
differ (the panel is fleet-wide over 5m; the alert is per tenant over 10m with a
traffic floor), and the docs say so.

Both sides are READ from their source files, never copied here.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_MON = _REPO / "k8s" / "03-monitoring"
_RULES = _MON / "configmap-rules-platform.yaml"
_PANEL = "Rejection Rate (5m)"

CASES = [
    ("federation-audit-dashboard.json", "FederationRejectionRateAnomaly"),
    ("tenant-log-query-dashboard.json", "TenantLogQueryRejectionRateAnomaly"),
]

_STATUS_RE = re.compile(r'status=~"([^"]+)"')


def _statuses(expr: str, where: str) -> set[str]:
    found = {frozenset(m.split("|")) for m in _STATUS_RE.findall(expr)}
    assert len(found) == 1, f"{where}: expected one status=~ matcher set, got {found}"
    return set(found.pop())


def _alert_expr(name: str) -> str:
    cm = yaml.safe_load(_RULES.read_text(encoding="utf-8"))
    for body in cm["data"].values():
        for group in yaml.safe_load(body).get("groups", []):
            for rule in group.get("rules", []):
                if rule.get("alert") == name:
                    return rule["expr"]
    raise AssertionError(f"alert {name} not found in {_RULES.name}")


def _panel_expr(dashboard: str) -> str:
    dash = json.loads((_MON / dashboard).read_text(encoding="utf-8"))
    hits = [p for p in dash["panels"] if p.get("title") == _PANEL]
    assert len(hits) == 1, f"{dashboard}: expected one '{_PANEL}' panel, got {len(hits)}"
    return hits[0]["targets"][0]["expr"]


@pytest.mark.parametrize(("dashboard", "alert"), CASES)
def test_panel_counts_same_statuses_as_alert(dashboard: str, alert: str) -> None:
    panel = _statuses(_panel_expr(dashboard), f"{dashboard} '{_PANEL}'")
    rule = _statuses(_alert_expr(alert), alert)
    assert panel == rule, (
        f"{dashboard} '{_PANEL}' counts {sorted(panel)} but {alert} counts "
        f"{sorted(rule)}; a rejection means the same thing on both"
    )
