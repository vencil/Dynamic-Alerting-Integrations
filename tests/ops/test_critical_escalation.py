"""#2244：domain policy 的 `require_critical_escalation` 由 check_domain_policies 執行。

釘住的契約（owner 裁決，判準 D）：

* 合規 ⇔ 主 receiver type ∈ ``ESCALATION_TYPES``（``{pagerduty}``），或存在一條
  會產出的 ``routes[j]``，``match`` 含 ``severity: critical`` 且 receiver type ∈
  ``ESCALATION_TYPES``。只換 receiver type 或目標不算升級。
* 不合規與其他 constraint 同形：``--strict`` 為 ERROR（rc 1），預設為 WARN。
* 洩漏（#2312，owner 拍板 (a)，取代 #2244 的「遮蔽」與「部分涵蓋」）：合規後，
  對每個 receiver 不是 PagerDuty 的目的地 N（依 render 順序的 override、route，
  最後是主 receiver，其 match 視為空），令 ``C_N = match(N) ∪ {severity:
  critical}``：N 的 match 帶非 critical 的 severity → 收不到；N 之前有任何子
  路由 P（不論 PD 與否）``match(P) ⊆ C_N``（連同 tenant route 自帶的
  ``tenant=<t>``）→ 攔不到；否則報一則 WARN，點名 N 與它攔走的條件。主
  receiver 是否 PD 不影響子路由的判定。兩種模式都不影響 rc。
* 值為 false / null 不檢查；非 bool 比照 ``_constraint_list``：strict ERROR、
  非 strict 靜默。沒有主 receiver 的 tenant 略過。

Fixture 取自 ADR-007 範例（profiles 用 ``conf.d/examples/_routing_profiles.yaml``），
tenant 名是 demo 名（tenant-agnostic）。policy 只放本鍵，讓訊息不被其他
constraint 混雜。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from generate_alertmanager_routes import check_domain_policies, load_tenant_tree
from _grar_validate import (
    ESCALATION_TYPES,
    EscalationFindings as _F,
    critical_escalation_findings,
)

REPO = Path(__file__).resolve().parents[2]
_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_EXAMPLE_PROFILES = (REPO / "components" / "threshold-exporter" / "config"
                     / "conf.d" / "examples" / "_routing_profiles.yaml")

_EMAIL = {"type": "email", "to": ["dba@example.com"],
          "smarthost": "smtp.example.com:587", "from": "am@example.com"}
_SLACK = {"type": "slack", "api_url": "https://hooks.slack.com/services/T/B/x"}
_PD = {"type": "pagerduty", "service_key": "k"}

# ADR-007 範例的五個 tenant（第 0 步 fixture）。
_TENANTS = {
    "t-sre": {"_routing_profile": "team-sre-apac"},
    "t-dba": {"_routing_profile": "team-dba-global"},
    "t-fin": {"_routing_profile": "domain-finance-tier1"},
    "t-ovr": {"_routing_profile": "team-sre-apac",
              "_routing": {"overrides": [
                  {"alertname": "MariaDBDown", "receiver": _EMAIL}]}},
    "t-livedbb": {"_routing": {
        "receiver": {"type": "webhook",
                     "url": "https://webhook.example.com/alerts"},
        "group_by": ["alertname", "severity"],
        "group_wait": "30s", "repeat_interval": "4h"}},
}


def _write(d: Path, name: str, data) -> None:
    (d / name).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _tree(tmp_path: Path, tenants: list[str] | None = None,
          value=True, extra: dict | None = None) -> Path:
    d = tmp_path / "conf.d"
    d.mkdir()
    shutil.copy(_EXAMPLE_PROFILES, d / "_routing_profiles.yaml")
    _write(d, "_defaults.yaml", {"defaults": {"cpu_usage_percent": 80}})
    for t, body in {**_TENANTS, **(extra or {})}.items():
        _write(d, f"{t}.yaml",
               {"tenants": {t: {**body, "cpu_usage_percent": "85"}}})
    _write(d, "_domain_policy.yaml", {"domain_policies": {"finance": {
        "tenants": sorted(_TENANTS) if tenants is None else tenants,
        "constraints": {"require_critical_escalation": value}}}})
    return d


def _routing(d: Path) -> dict:
    return load_tenant_tree(str(d)).as_tuple()[0]


def _policy(tenants, value=True):
    return {"finance": {"tenants": tenants,
                        "constraints": {"require_critical_escalation": value}}}


def _run(d: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d), "--validate",
         *extra], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)


_NONCOMPLIANT = (
    "domain_policy 'finance', tenant '{t}': require_critical_escalation is "
    "set but severity=critical alerts do not reach a receiver of type "
    "['pagerduty'] (main receiver type '{main}', and no rendered routes entry "
    "matches severity=critical with such a receiver)")
_HINT = (" — fix: add `routes: - match: {severity: critical}` with a "
         "pagerduty receiver to the tenant's _routing or its routing profile, "
         "or switch the main receiver.type to pagerduty")
_OVR_SHADOW = (
    "  WARN: domain_policy 'finance', tenant 't-ovr' override[0] "
    "(alertname=MariaDBDown): receiver type 'email' catches alerts with "
    "severity=critical, alertname=MariaDBDown before any receiver of type "
    "['pagerduty'] does, so they never reach one")


def test_escalation_types_is_pagerduty_only():
    assert ESCALATION_TYPES == frozenset({"pagerduty"})


# ============================================================
# ADR-007 範例 fixture：逐 tenant 判定
# ============================================================
class TestAdr007Fixture:

    @pytest.mark.parametrize("tenant,target,leaks", [
        ("t-fin", "the main receiver", []),
        ("t-sre", "routes[0] (severity=critical)", []),
        ("t-dba", None, []),
        ("t-livedbb", None, []),
        ("t-ovr", "routes[0] (severity=critical)",
         [("override[0]", "alertname=MariaDBDown", "email",
           "severity=critical, alertname=MariaDBDown")]),
    ])
    def test_findings_per_tenant(self, tmp_path, tenant, target, leaks):
        rc = _routing(_tree(tmp_path))[tenant]
        assert critical_escalation_findings(rc, tenant) == _F(target, leaks)

    @pytest.mark.parametrize("strict", [True, False])
    def test_messages(self, tmp_path, strict):
        routing = _routing(_tree(tmp_path))
        msgs = check_domain_policies(routing, _policy(sorted(_TENANTS)),
                                     strict=strict)
        sev, hint = ("ERROR", _HINT) if strict else ("WARN", "")
        assert sorted(msgs) == sorted([
            f"  {sev}: " + _NONCOMPLIANT.format(t="t-dba", main="webhook") + hint,
            f"  {sev}: " + _NONCOMPLIANT.format(t="t-livedbb", main="webhook") + hint,
            _OVR_SHADOW,
        ])

    def test_cli_strict_fails_on_noncompliant(self, tmp_path):
        r = _run(_tree(tmp_path), "--strict")
        assert r.returncode == 1, r.stdout + r.stderr
        assert "  ERROR: " + _NONCOMPLIANT.format(
            t="t-dba", main="webhook") + _HINT in r.stderr
        assert _OVR_SHADOW in r.stderr

    def test_cli_lenient_passes_with_warn(self, tmp_path):
        r = _run(_tree(tmp_path))
        assert r.returncode == 0, r.stdout + r.stderr
        assert "  WARN: " + _NONCOMPLIANT.format(
            t="t-livedbb", main="webhook") in r.stderr

    def test_cli_strict_shadow_warn_does_not_block(self, tmp_path):
        """只約束合規 tenant：洩漏 WARN 照印，`--strict --validate` 仍 rc 0。"""
        r = _run(_tree(tmp_path, tenants=["t-fin", "t-ovr", "t-sre"]),
                 "--strict")
        assert r.returncode == 0, r.stdout + r.stderr
        assert _OVR_SHADOW in r.stderr


# ============================================================
# 值的處理
# ============================================================
class TestConstraintValue:

    @pytest.mark.parametrize("value", [False, None])
    def test_false_or_null_is_not_checked(self, tmp_path, value):
        routing = _routing(_tree(tmp_path))
        for strict in (True, False):
            assert check_domain_policies(
                routing, _policy(sorted(_TENANTS), value), strict=strict) == []

    @pytest.mark.parametrize("value", ["true", 1, ["pagerduty"]])
    def test_non_bool_strict_error_once(self, tmp_path, value):
        routing = _routing(_tree(tmp_path))
        msgs = check_domain_policies(routing, _policy(sorted(_TENANTS), value),
                                     strict=True)
        assert msgs == [
            "  ERROR: domain_policy 'finance': constraint "
            "'require_critical_escalation' must be a boolean, got "
            f"{type(value).__name__} {value!r} — the constraint cannot be "
            "enforced — fix: set 'require_critical_escalation' to true or "
            "false (unquoted)"]

    def test_non_bool_lenient_is_silent(self, tmp_path):
        routing = _routing(_tree(tmp_path))
        assert check_domain_policies(
            routing, _policy(sorted(_TENANTS), "true"), strict=False) == []

    def test_tenant_without_main_receiver_is_skipped(self):
        routing = {"t-x": {"group_wait": "30s"}}
        assert check_domain_policies(routing, _policy(["t-x"]),
                                     strict=True) == []


# ============================================================
# 洩漏判準（#2312）：哪些非 PagerDuty 目的地收得到 critical
# ============================================================
_HOOK = {"type": "webhook", "url": "https://hooks.example.com/main"}
_PD2 = {"type": "pagerduty", "service_key": "k2"}
_MAINS = {"pd": _PD, "slack": _SLACK, "hook": _HOOK}
_T = "t-mx"  # 洩漏表的 demo tenant（tenant-agnostic）

_E_FULL = {"match": {"severity": "critical"}, "receiver": _PD2}
_E_PART = {"match": {"severity": "critical", "team": "db"}, "receiver": _PD2}


def _rc(*routes, overrides=None, main=_SLACK):
    rc = {"receiver": main, "routes": list(routes)}
    if overrides:
        rc["overrides"] = overrides
    return rc


# (id, main, overrides, routes, 期望洩漏 [(ref, 攔走的條件)])。形狀取自
# #2312 量測期的 116 組窮舉（S1 / S2 為 #2244 判準漏報的形狀）。
_LEAK_TABLE = [
    # S1：部分升級後接非 PD route。主 receiver 是 PD 時舊判準完全靜默。
    ("s1-main-pd", "pd", None,
     [_E_PART, {"match": {"team": "app"}, "receiver": _SLACK}],
     [("routes[1]", "severity=critical, team=app")]),
    # 主 receiver 非 PD 時，舊判準只說「其餘落到主 receiver」，漏了 routes[1]。
    ("s1-main-slack", "slack", None,
     [_E_PART, {"match": {"team": "app"}, "receiver": _SLACK}],
     [("routes[1]", "severity=critical, team=app"),
      ("the main receiver", "severity=critical")]),
    # S2：非 PD route 夾在部分升級與 {severity: critical} PD 之間。
    ("s2-main-pd", "pd", None,
     [_E_PART, {"match": {"team": "app"}, "receiver": _SLACK}, _E_FULL],
     [("routes[1]", "severity=critical, team=app")]),
    ("s2-main-slack", "slack", None,
     [_E_PART, {"match": {"severity": "critical", "team": "app"},
                "receiver": _SLACK}, _E_FULL],
     [("routes[1]", "severity=critical, team=app")]),
    # 舊判準已正確的遮蔽：排在升級目的地之前。
    ("shadow-route-before", "slack", None,
     [{"match": {"team": "db"}, "receiver": _EMAIL}, _E_FULL],
     [("routes[0]", "severity=critical, team=db")]),
    ("shadow-override-main-pd", "pd",
     [{"alertname": "MariaDBDown", "receiver": _SLACK}], [],
     [("override[0]", "severity=critical, alertname=MariaDBDown")]),
    # {severity: critical} 的升級 route 之後，任何子路由與主 receiver 都攔不到。
    ("full-escalation-then-anything", "slack", None,
     [_E_FULL, {"match": {"team": "app"}, "receiver": _SLACK},
      {"match": {"severity": "critical", "team": "app"}, "receiver": _SLACK},
      {"match": {"tenant": _T}, "receiver": _SLACK}],
     []),
    # {team: db} 被前面的 {severity: critical, team: db} 升級 route 包住。
    ("teamdb-covered-by-partial", "pd", None,
     [_E_PART, {"match": {"team": "db"}, "receiver": _SLACK}], []),
    # 非 critical 的 severity 收不到 critical。
    ("warning-route", "slack", None,
     [{"match": {"severity": "warning", "team": "app"}, "receiver": _SLACK},
      _E_FULL],
     []),
    # 較早的非 PD 子路由也算「攔走」：同 alertname 的第二條 override 收不到。
    ("override-behind-same-override", "pd",
     [{"alertname": "A", "receiver": _SLACK},
      {"alertname": "A", "receiver": _EMAIL}], [],
     [("override[0]", "severity=critical, alertname=A")]),
    # 升級子路由在前面也一樣攔走。
    ("pagerduty-override-ahead", "slack",
     [{"alertname": "A", "receiver": _PD}], [_E_FULL], []),
    # 部分升級、後接非 PD 的 catch-all：主 receiver 被 catch-all 蓋住不報。
    ("partial-then-nonpd-catch-all", "hook", None,
     [_E_PART, {"match": {"severity": "critical", "team": "web"},
                "receiver": _EMAIL},
      {"match": {"severity": "critical"}, "receiver": _EMAIL}],
     [("routes[1]", "severity=critical, team=web"),
      ("routes[2]", "severity=critical")]),
    # 部分升級、主 receiver 非 PD：其餘 critical 真的落到主 receiver。
    ("partial-main-hook", "hook", None,
     [{"match": {"severity": "critical", "alertname": "Z"}, "receiver": _PD}],
     [("the main receiver", "severity=critical")]),
    ("partial-main-pd", "pd", None,
     [{"match": {"severity": "critical", "alertname": "Z"}, "receiver": _PD}],
     []),
    # tenant route 自帶 tenant=<t>：{tenant: <t>} 攔走一切，主 receiver 收不到；
    # 比對別的 tenant 的 route 什麼都收不到。
    ("own-tenant-route", "slack", None,
     [_E_PART, {"match": {"tenant": _T}, "receiver": _SLACK}],
     [("routes[1]", f"severity=critical, tenant={_T}")]),
    ("other-tenant-route", "pd", None,
     [{"match": {"tenant": "t-other"}, "receiver": _SLACK}], []),
    # override 的值 render 成字串：alertname: 123 與 route 的 "123" 是同一個
    # matcher，後面的 route 被 PD override 攔走、不報。
    ("int-override-value", "pd",
     [{"alertname": 123, "receiver": _PD}],
     [{"match": {"alertname": "123"}, "receiver": _SLACK}], []),
]


def _table_rc(main, overrides, routes):
    return _rc(*routes, overrides=overrides, main=_MAINS[main])


class TestLeaks:

    @pytest.mark.parametrize("main,overrides,routes,expected",
                             [c[1:] for c in _LEAK_TABLE],
                             ids=[c[0] for c in _LEAK_TABLE])
    def test_leak_table(self, main, overrides, routes, expected):
        f = critical_escalation_findings(
            _table_rc(main, overrides, routes), _T)
        assert f.target is not None
        assert [(ref, caught) for ref, _m, _t, caught in f.leaks] == expected

    def test_leak_entry_carries_ref_match_and_type(self):
        rc = _rc({"match": {"team": "db"}, "receiver": _EMAIL}, _E_FULL)
        assert critical_escalation_findings(rc) == _F(
            "routes[1] (severity=critical)",
            [("routes[0]", "team=db", "email", "severity=critical, team=db")])

    def test_without_tenant_the_tenant_label_is_unknown(self):
        """不給 tenant 時不知道 tenant route 的 matcher，只會多報、不會少報。"""
        rc = _rc(_E_PART, {"match": {"tenant": _T}, "receiver": _SLACK})
        assert [ref for ref, *_ in critical_escalation_findings(rc).leaks] == [
            "routes[1]", "the main receiver"]

    def test_main_pagerduty_target_does_not_hide_subroutes(self):
        rc = _rc({"match": {"severity": "warning"}, "receiver": _EMAIL},
                 {"match": {"team": "db"}, "receiver": _SLACK},
                 overrides=[{"metric_group": "g", "receiver": _EMAIL}],
                 main=_PD)
        assert critical_escalation_findings(rc) == _F("the main receiver", [
            ("override[0]", "metric_group=g", "email",
             "severity=critical, metric_group=g"),
            ("routes[1]", "team=db", "slack", "severity=critical, team=db")])

    def test_skipped_route_does_not_count_as_escalation(self):
        rc = _rc({"match": {"severity": "critical"}, "receiver": _PD,
                  "continue": True})
        assert critical_escalation_findings(rc) == _F(None, [])

    @pytest.mark.parametrize("match", [{"alertname": "X"},
                                       {"severity": "warning"}])
    def test_pagerduty_route_not_matching_critical_is_not_escalation(
            self, match):
        rc = _rc({"match": match, "receiver": _PD}, main=_HOOK)
        assert critical_escalation_findings(rc) == _F(None, [])

    def test_escalation_to_another_type_is_not_compliant(self):
        rc = _rc({"match": {"severity": "critical"}, "receiver": _EMAIL})
        assert critical_escalation_findings(rc) == _F(None, [])

    def test_leak_warn_never_trips_validate_mode(self):
        routing = {"t-x": _rc(
            {"match": {"team": "db"}, "receiver": _EMAIL}, _E_FULL)}
        for strict in (True, False):
            msgs = check_domain_policies(routing, _policy(["t-x"]),
                                         strict=strict)
            assert len(msgs) == 1, msgs
            assert msgs[0].startswith("  WARN: ")
            assert "skipping" not in msgs[0]

    def test_s1_main_pd_message_names_the_leaking_route(self):
        """S1：訊息點名真正攔走 critical 的 route，不說「落到主 receiver」。"""
        routing = {_T: _rc(_E_PART, {"match": {"team": "app"},
                                     "receiver": _SLACK}, main=_PD)}
        assert check_domain_policies(routing, _policy([_T])) == [
            f"  WARN: domain_policy 'finance', tenant '{_T}' routes[1] "
            "(team=app): receiver type 'slack' catches alerts with "
            "severity=critical, team=app before any receiver of type "
            "['pagerduty'] does, so they never reach one"]


_PART_TENANT = {"t-part": {"_routing": {
    "receiver": _HOOK,
    "group_by": ["tenant", "alertname", "severity"],
    "routes": [{"match": {"severity": "critical", "alertname": "Z"},
                "receiver": _PD}]}}}
_PART_WARN = (
    "  WARN: domain_policy 'finance', tenant 't-part': severity=critical "
    "alerts that no sub-route catches go to the main receiver (type "
    "'webhook'), not a receiver of type ['pagerduty']")


class TestMainReceiverLeak:

    @pytest.mark.parametrize("strict", [True, False])
    def test_main_leak_is_a_plain_warn(self, tmp_path, strict):
        routing = _routing(_tree(tmp_path, extra=_PART_TENANT))
        msgs = check_domain_policies(routing, _policy(["t-part"]),
                                     strict=strict)
        assert msgs == [_PART_WARN]
        assert "skipping" not in msgs[0]

    def test_cli_strict_main_leak_does_not_block(self, tmp_path):
        r = _run(_tree(tmp_path, tenants=["t-part"], extra=_PART_TENANT),
                 "--strict")
        assert r.returncode == 0, r.stdout + r.stderr
        assert _PART_WARN in r.stderr


# ============================================================
# 對照：amtool（0.34.1）為 oracle——WARN 有無、點名的目的地，與 amtool 下
# critical 探針落到非 PagerDuty receiver 的情形一致
# （PATH 上有 amtool 才跑，同 test_routing_profile_routes.py）
# ============================================================
_AMTOOL = shutil.which("amtool")
_LEAK_LINE = re.compile(
    rf"  WARN: domain_policy 'finance', tenant '{_T}'(?: (\S+) \(|:)")


@pytest.mark.skipif(_AMTOOL is None, reason="amtool not on PATH")
class TestAmtoolOracle:

    @pytest.mark.parametrize("main,overrides,routes,_expected",
                             [c[1:] for c in _LEAK_TABLE],
                             ids=[c[0] for c in _LEAK_TABLE])
    def test_leaks_agree_with_amtool(self, tmp_path, main, overrides, routes,
                                     _expected):
        rc = _table_rc(main, overrides, routes)
        d = tmp_path / "conf.d"
        d.mkdir()
        _write(d, "_defaults.yaml", {"defaults": {"cpu_usage_percent": 80}})
        _write(d, f"{_T}.yaml", {"tenants": {_T: {
            "cpu_usage_percent": "85", "_routing": rc}}})
        _write(d, "_domain_policy.yaml", {"domain_policies": {"finance": {
            "tenants": [_T],
            "constraints": {"require_critical_escalation": True}}}})
        v = _run(d)
        warned = {m.group(1) or "the main receiver"
                  for m in map(_LEAK_LINE.match, v.stderr.splitlines()) if m}
        amtool = _amtool_nonpd_destinations(tmp_path, d, rc)
        assert warned == amtool, (v.stderr, amtool)
        assert warned == {ref for ref, _c in _expected}


def _amtool_nonpd_destinations(tmp_path: Path, d: Path, rc: dict) -> set[str]:
    """Render *d*, route one severity=critical probe per destination through
    amtool, and return the refs of the non-PagerDuty receivers they land on.

    With equality matchers the probes are complete: an alert reaching a
    destination N also reaches it when relabelled to exactly N's match plus
    ``severity=critical`` / ``tenant`` / a fresh ``alertname``, so one probe
    per rendered sub-route (and a bare one for the main receiver) finds
    every leak.
    """
    out = tmp_path / "cm.yaml"
    r = subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d),
         "--output-configmap", "-o", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    am = yaml.safe_load(
        yaml.safe_load(out.read_text(encoding="utf-8"))["data"]["alertmanager.yml"])
    am_yml = tmp_path / "alertmanager.yml"
    am_yml.write_text(yaml.safe_dump(am), encoding="utf-8")
    types = {rcv["name"]: next(
                 (k for k in rcv if k.endswith("_configs")), None)
             for rcv in am["receivers"]}
    matches = [{}]
    for o in rc.get("overrides") or []:
        key = "alertname" if o.get("alertname") else "metric_group"
        matches.append({key: o[key]})
    matches.extend(dict(r["match"]) for r in rc.get("routes") or [])
    refs = {f"tenant-{_T}": "the main receiver"}
    for i in range(len(rc.get("overrides") or [])):
        refs[f"tenant-{_T}-override-{i}"] = f"override[{i}]"
    for i in range(len(rc.get("routes") or [])):
        refs[f"tenant-{_T}-route-{i}"] = f"routes[{i}]"
    leaks = set()
    for m in matches:
        if m.get("severity", "critical") != "critical":
            continue
        probe = {"alertname": "Probe", **m, "severity": "critical",
                 "tenant": _T}
        t = subprocess.run(
            [_AMTOOL, "config", "routes", "test", f"--config.file={am_yml}",
             *[f"{k}={v}" for k, v in probe.items()]],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert t.returncode in (0, 1) and t.stdout.strip(), t.stderr
        names = t.stdout.strip().split(",")
        if not any(types[n] == "pagerduty_configs" for n in names):
            leaks |= {refs[n] for n in names}
    return leaks
