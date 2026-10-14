"""config-diff 的載體表靠這支自我測試維持（ADR-037 決定 3，#1516 P3b）。

`config_diff.RENDERED_CARRIERS` 宣稱每個載體（`_` 開頭檔的頂層鍵）由哪個面向渲染。
這裡在出貨的範例 conf.d 上，對每個載體各改一次，斷言：

* 表上宣稱的每個面向都真的出現差異，而且該列的狀態是 `rendered`；
* 標為完整涵蓋（full）的載體，再對它的每個子鍵各改一次。

刪掉表中任一列、或渲染程式改版後看不到某個載體或子鍵，這裡會先紅。
`test_every_rendered_carrier_has_a_case` 守住「表上每一列都有案例」：只加列不加案例也紅。

完整與部分涵蓋也決定結束碼（#2962）：完整涵蓋而渲染沒有差異只附註（no_effect），
部分涵蓋仍 rc 1；所以完整涵蓋的載體在這裡逐一測子鍵。寫在哪個位置有人讀，由
tests/shared/test_parse_tree_rendered_by.py 以實際渲染驗證。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "tools" / "ops"))
sys.path.insert(0, str(ROOT / "scripts" / "tools"))

import config_diff as cd  # noqa: E402
import _render_diff as rd  # noqa: E402

SAMPLE = ROOT / "components" / "threshold-exporter" / "config" / "conf.d"
AT = "2026-10-10T00:00:00Z"
SERVED, ROUTES = rd.VIEW_SERVED, rd.VIEW_ROUTES

pytestmark = pytest.mark.usefixtures("da_guard_env")


def _sub(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"{old!r} occurs {text.count(old)} times in {path.name}: the sample moved"
    path.write_text(text.replace(old, new), encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _append(path: Path, text: str) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


ENFORCED = ("\n_routing_enforced:\n  enabled: true\n  receiver:\n    type: webhook\n"
            "    url: \"https://noc.example.com/hook\"\n  group_by: [alertname]\n"
            "  group_wait: 30s\n  group_interval: 5m\n  repeat_interval: 4h\n")
PROFILE = "profiles:\n  p1:\n    container_memory: 50\n"


def _bind_profile(d: Path) -> None:
    _write(d / "_profiles.yaml", PROFILE)
    _sub(d / "db-a.yaml", '    mysql_connections: "70"\n',
         '    mysql_connections: "70"\n    _profile: p1\n')


def _tenant_overlay(d: Path) -> None:
    _write(d / "_platform.yaml", "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n")


def _optional_override_in_use(d: Path) -> None:
    _sub(d / "db-a.yaml", '    mysql_connections: "70"\n',
         '    mysql_connections: "70"\n    oracle_process_count: "100"\n')


FINANCE = "examples/_routing_profiles.yaml"

# (case id, carrier, views it must change, setup on both sides, edit on the PR side)
CASES = [
    ("defaults", "defaults", {SERVED}, None,
     lambda d: _sub(d / "_defaults.yaml", "container_memory: 85", "container_memory: 86")),
    ("optional_overrides", "optional_overrides", {SERVED}, _optional_override_in_use,
     lambda d: _sub(d / "_defaults.yaml", "  - oracle_process_count", "  - oracle_process_countx")),
    ("state_filters.severity", "state_filters", {SERVED}, None,
     lambda d: _sub(d / "_defaults.yaml", 'reasons: ["CrashLoopBackOff"]\n    severity: "critical"',
                    'reasons: ["CrashLoopBackOff"]\n    severity: "warning"')),
    ("_routing_defaults.group_wait", "_routing_defaults", {ROUTES}, None,
     lambda d: _sub(d / "_defaults.yaml", 'group_wait: "30s"', 'group_wait: "99s"')),
    # full: every sub-key once
    ("_routing_enforced.enabled", "_routing_enforced", {ROUTES}, None,
     lambda d: _append(d / "_defaults.yaml", ENFORCED)),
    ("_routing_enforced.receiver", "_routing_enforced", {ROUTES},
     lambda d: _append(d / "_defaults.yaml", ENFORCED),
     lambda d: _sub(d / "_defaults.yaml", "https://noc.example.com/hook", "https://noc.example.com/h2")),
    ("_routing_enforced.group_by", "_routing_enforced", {ROUTES},
     lambda d: _append(d / "_defaults.yaml", ENFORCED),
     lambda d: _sub(d / "_defaults.yaml", "group_by: [alertname]", "group_by: [alertname, tenant]")),
    ("_routing_enforced.group_wait", "_routing_enforced", {ROUTES},
     lambda d: _append(d / "_defaults.yaml", ENFORCED),
     lambda d: _sub(d / "_defaults.yaml", "  group_wait: 30s\n  group_interval: 5m\n  repeat_interval: 4h",
                    "  group_wait: 31s\n  group_interval: 5m\n  repeat_interval: 4h")),
    ("_routing_enforced.group_interval", "_routing_enforced", {ROUTES},
     lambda d: _append(d / "_defaults.yaml", ENFORCED),
     lambda d: _sub(d / "_defaults.yaml", "  group_interval: 5m\n  repeat_interval: 4h",
                    "  group_interval: 4m\n  repeat_interval: 4h")),  # guardrail: 5s–5m
    ("_routing_enforced.repeat_interval", "_routing_enforced", {ROUTES},
     lambda d: _append(d / "_defaults.yaml", ENFORCED),
     lambda d: _sub(d / "_defaults.yaml", "  repeat_interval: 4h\n", "  repeat_interval: 5h\n")),
    ("routing_profiles.receiver", "routing_profiles", {ROUTES}, None,
     lambda d: _sub(d / FINANCE, 'service_key: "finance-tier1-key"', 'service_key: "finance-tier1-k2"')),
    ("routing_profiles.group_by", "routing_profiles", {ROUTES}, None,
     lambda d: _sub(d / FINANCE, "    group_by: [tenant, alertname, severity]\n    group_wait: 30s\n"
                    "    group_interval: 5m\n    repeat_interval: 1h",
                    "    group_by: [tenant, alertname]\n    group_wait: 30s\n"
                    "    group_interval: 5m\n    repeat_interval: 1h")),
    ("routing_profiles.group_wait", "routing_profiles", {ROUTES}, None,
     lambda d: _sub(d / FINANCE, "    group_wait: 30s\n    group_interval: 5m\n    repeat_interval: 1h",
                    "    group_wait: 31s\n    group_interval: 5m\n    repeat_interval: 1h")),
    ("routing_profiles.group_interval", "routing_profiles", {ROUTES}, None,
     lambda d: _sub(d / FINANCE, "    group_interval: 5m\n    repeat_interval: 1h",
                    "    group_interval: 4m\n    repeat_interval: 1h")),  # guardrail: 5s–5m
    ("routing_profiles.repeat_interval", "routing_profiles", {ROUTES}, None,
     lambda d: _sub(d / FINANCE, "repeat_interval: 1h", "repeat_interval: 2h")),
    ("routing_profiles.routes", "routing_profiles", {ROUTES}, None,
     lambda d: _sub(d / FINANCE, "repeat_interval: 1h",
                    "repeat_interval: 1h\n    routes:\n      - match: { severity: critical }\n"
                    "        receiver:\n          type: pagerduty\n          service_key: \"finance-crit\"")),
    ("tenants.threshold", "tenants", {SERVED}, None,
     # a key db-a's own file does not set: the tenant's own value wins over the overlay
     lambda d: _write(d / "_platform.yaml", "tenants:\n  db-a:\n    container_memory: \"60\"\n")),
    ("tenants._silent_mode", "tenants", {SERVED}, _tenant_overlay,
     lambda d: _sub(d / "_platform.yaml", '_silent_mode: "warning"', '_silent_mode: "critical"')),
    ("tenants._metadata", "tenants", {SERVED}, None,
     lambda d: _write(d / "_platform.yaml", "tenants:\n  db-a:\n    _metadata:\n      owner: \"team-x\"\n")),
    ("tenants._namespaces", "tenants", {SERVED}, None,
     lambda d: _write(d / "_platform.yaml", "tenants:\n  db-a:\n    _namespaces: [\"ns-x\"]\n")),
    ("tenants._severity_dedup", "tenants", {SERVED, ROUTES}, None,
     lambda d: _write(d / "_platform.yaml", "tenants:\n  db-a:\n    _severity_dedup: \"disable\"\n")),
    ("tenants._routing", "tenants", {SERVED, ROUTES}, None,
     lambda d: _write(d / "_platform.yaml", "tenants:\n  db-a:\n    _routing:\n      receiver:\n"
                      "        type: webhook\n        url: \"https://hooks.example.com/a\"\n")),
    ("profiles.threshold", "profiles", {SERVED}, _bind_profile,
     lambda d: _sub(d / "_profiles.yaml", "container_memory: 50", "container_memory: 51")),
    ("profiles._routing", "profiles", {SERVED}, _bind_profile,
     lambda d: _append(d / "_profiles.yaml", "    _routing:\n      receiver:\n        type: webhook\n"
                       "        url: \"https://hooks.example.com/p\"\n")),
]


def test_every_rendered_carrier_has_a_case() -> None:
    assert {c[1] for c in CASES} == set(cd.RENDERED_CARRIERS), (
        "a row of RENDERED_CARRIERS has no self-test case here, or a case names a carrier "
        "the table no longer has")
    for case_id, carrier, views, *_ in CASES:
        assert views <= set(cd.RENDERED_CARRIERS[carrier][0]), (
            f"{case_id}: the case expects views the table does not claim for {carrier}")


@pytest.mark.parametrize("case_id,carrier,views,setup,edit", CASES, ids=[c[0] for c in CASES])
def test_each_carrier_rings_in_the_views_the_table_claims(tmp_path, case_id, carrier, views, setup,
                                                          edit) -> None:
    old, new = tmp_path / "old", tmp_path / "new"
    shutil.copytree(SAMPLE, old)
    shutil.copytree(SAMPLE, new)
    if setup:
        setup(old)
        setup(new)
    edit(new)
    diff = rd.compare(old, new, AT, served_report=cd.served_sections)
    report = cd.build_report(diff, old, new)
    statuses = {name: v.status for name, v in report["views"].items()}
    assert set(statuses.values()) == {"computed"}, statuses
    changed = {SERVED} if cd._served_changed(report) else set()
    if report["routes"] or report["receivers"] or report["inhibits"]:
        changed.add(ROUTES)
    assert views <= changed, (
        f"{case_id}: the table says {carrier} is rendered by {sorted(views)}, "
        f"but only {sorted(changed)} changed")
    rows = [r for r in report["platform_settings"] if r["key"] == carrier]
    assert rows and {r["status"] for r in rows} == {cd.SETTING_RENDERED}, report["platform_settings"]
