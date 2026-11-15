#!/usr/bin/env python3
"""test_k8s_pair_severity_dedup.py — kubernetes pack 的 4 對 warning/critical 告警，
產生出來的 severity-dedup inhibit rule 真的會抑制 warning（#1199）。

`check_metric_group_pairs` 只驗「兩邊都有、值相同」這個標籤形狀；本檔驗行為：
拿出貨規則檔的 static labels（經 lint 同一支 Prometheus 讀法的 collector），
加上租戶標籤，用 Alertmanager 的 inhibit 語意（matcher 全錨定的 regex、
`equal` 裡缺標籤視同空字串、告警不抑制自己）評估 `generate_inhibit_rules` 的
輸出。這裡沒有 Alertmanager／docker，所以 oracle 是本檔的小型評估器，
不是 amtool——它只涵蓋 inhibit rule 用到的 `=`／`=~`／`!=`／`!~` 四種 matcher，
其他形狀一律讓測試失敗而非略過。

斷言三件事，各對應一種失效：
  - 同租戶、同一對的 critical 抑制它的 warning（缺 metric_group → 紅）；
  - repo 裡任何其他 critical 都不抑制它（兩對共用同一值，或撞到別包的值 → 紅）；
  - 別的租戶的同名 critical 不抑制它（租戶隔離）。
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from generate_alertmanager_routes import generate_inhibit_rules

REPO_ROOT = Path(__file__).resolve().parents[2]
_LINT = REPO_ROOT / "scripts" / "tools" / "lint" / "check_metric_group_pairs.py"
_spec = importlib.util.spec_from_file_location("check_metric_group_pairs", _LINT)
_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gate)

PAIRS = ("PodContainerHighCPU", "PodContainerHighMemory",
         "PodContainerCPUThrottled", "ContainerOOMKilled")
TENANT = "tenant-x"
OTHER_TENANT = "tenant-y"

_MATCHER = re.compile(r'^([A-Za-z_][A-Za-z0-9_]*)(=~|!~|!=|=)"(.*)"$')


def _matches(matcher: str, labels: dict) -> bool:
    m = _MATCHER.match(matcher)
    assert m, f"matcher shape this evaluator does not model: {matcher!r}"
    name, op, value = m.groups()
    got = labels.get(name, "")  # Alertmanager: a missing label is ""
    if op == "=":
        return got == value
    if op == "!=":
        return got != value
    hit = re.fullmatch(f"(?:{value})", got) is not None  # anchored, like AM
    return hit if op == "=~" else not hit


def _inhibited(target: dict, firing: list[dict], rules: list[dict]) -> bool:
    for rule in rules:
        if not all(_matches(m, target) for m in rule["target_matchers"]):
            continue
        for source in firing:
            if source is target:
                continue
            if not all(_matches(m, source) for m in rule["source_matchers"]):
                continue
            if all(source.get(l, "") == target.get(l, "") for l in rule["equal"]):
                return True
    return False


def _instance(name: str, static: dict, tenant: str) -> dict:
    """One firing alert: the rule's static labels (templated ones dropped),
    plus the series labels the pairs aggregate to."""
    labels = {k: str(v) for k, v in static.items() if "{{" not in str(v)}
    labels.update(alertname=name, tenant=tenant, version="default")
    return labels


@pytest.fixture(scope="module")
def alerts() -> dict[str, dict]:
    return _gate.collect_alerts()


@pytest.fixture(scope="module")
def rules() -> list[dict]:
    out, _ = generate_inhibit_rules({TENANT: "enable", OTHER_TENANT: "enable"})
    assert len(out) == 1  # #3899: one rule for every tenant
    return out


@pytest.mark.parametrize("base", PAIRS)
def test_warning_inhibited_by_its_own_critical(base, alerts, rules):
    warning = _instance(base, alerts[base], TENANT)
    critical = _instance(base + "Critical", alerts[base + "Critical"], TENANT)
    assert warning["severity"] == "warning" and critical["severity"] == "critical"
    assert _inhibited(warning, [warning, critical], rules), (
        f"{base}: its critical does not inhibit it — both notify (#1199)")


@pytest.mark.parametrize("base", PAIRS)
def test_warning_not_inhibited_by_any_other_critical(base, alerts, rules):
    warning = _instance(base, alerts[base], TENANT)
    others = [_instance(n, l, TENANT) for n, l in alerts.items()
              if (l or {}).get("severity") == "critical" and n != base + "Critical"]
    assert others, "collector returned no other critical alerts"
    culprits = [o["alertname"] for o in others if _inhibited(warning, [o], rules)]
    assert culprits == [], f"{base} warning inhibited by unrelated critical(s): {culprits}"


@pytest.mark.parametrize("base", PAIRS)
def test_warning_not_inhibited_across_tenants(base, alerts, rules):
    warning = _instance(base, alerts[base], TENANT)
    foreign = _instance(base + "Critical", alerts[base + "Critical"], OTHER_TENANT)
    assert not _inhibited(warning, [warning, foreign], rules)
