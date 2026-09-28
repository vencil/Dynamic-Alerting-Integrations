"""#2244：domain policy 的 `require_critical_escalation` 由 check_domain_policies 執行。

釘住的契約（owner 裁決，判準 D）：

* 合規 ⇔ 主 receiver type ∈ ``ESCALATION_TYPES``（``{pagerduty}``），或存在一條
  會產出的 ``routes[j]``，``match`` 含 ``severity: critical`` 且 receiver type ∈
  ``ESCALATION_TYPES``。只換 receiver type 或目標不算升級。
* 不合規與其他 constraint 同形：``--strict`` 為 ERROR（rc 1），預設為 WARN。
* 遮蔽：排在第一個升級目的地之前（overrides → routes → 主 receiver）、receiver
  不是 PagerDuty、又可能攔下 critical 的子路由（override 一律算；route 沒寫
  ``severity`` 或寫 ``critical`` 才算）各報一則 WARN，兩種模式都不影響 rc。
* 部分升級：升級目的地是 ``routes[j]`` 且 match 除 ``severity`` 外還有別的
  label 時，報一則 WARN，列出它涵蓋的 label 與
  其餘 critical 落到哪裡（後面第一條 match 恰為 ``{severity: critical}`` 的
  route，否則主 receiver）；那裡若是 PagerDuty 就不報。不影響 rc。
* 值為 false / null 不檢查；非 bool 比照 ``_constraint_list``：strict ERROR、
  非 strict 靜默。沒有主 receiver 的 tenant 略過。

Fixture 取自 ADR-007 範例（profiles 用 ``conf.d/examples/_routing_profiles.yaml``），
tenant 名是 demo 名（tenant-agnostic）。policy 只放本鍵，讓訊息不被其他
constraint 混雜。
"""
from __future__ import annotations

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
         *extra], capture_output=True, text=True, timeout=300)


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
    "(alertname=MariaDBDown): receiver type 'email' is matched before the "
    "critical escalation target routes[0] (severity=critical), so "
    "severity=critical alerts it matches never reach a receiver of type "
    "['pagerduty']")


def test_escalation_types_is_pagerduty_only():
    assert ESCALATION_TYPES == frozenset({"pagerduty"})


# ============================================================
# ADR-007 範例 fixture：逐 tenant 判定
# ============================================================
class TestAdr007Fixture:

    @pytest.mark.parametrize("tenant,target,shadows", [
        ("t-fin", "the main receiver", []),
        ("t-sre", "routes[0] (severity=critical)", []),
        ("t-dba", None, []),
        ("t-livedbb", None, []),
        ("t-ovr", "routes[0] (severity=critical)",
         [("override[0]", "alertname=MariaDBDown", "email")]),
    ])
    def test_findings_per_tenant(self, tmp_path, tenant, target, shadows):
        rc = _routing(_tree(tmp_path))[tenant]
        assert critical_escalation_findings(rc) == _F(target, shadows)

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
        """只約束合規 tenant：遮蔽 WARN 照印，`--strict --validate` 仍 rc 0。"""
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
# 遮蔽判準：哪些排在前面的子路由算數
# ============================================================
def _rc(*routes, overrides=None, main=_SLACK):
    rc = {"receiver": main, "routes": list(routes)}
    if overrides:
        rc["overrides"] = overrides
    return rc


class TestShadowing:

    def test_warning_route_ahead_is_not_a_shadow(self):
        rc = _rc({"match": {"severity": "warning"}, "receiver": _EMAIL},
                 {"match": {"severity": "critical"}, "receiver": _PD})
        assert critical_escalation_findings(rc) == _F(
            "routes[1] (severity=critical)", [])

    def test_route_without_severity_ahead_is_a_shadow(self):
        rc = _rc({"match": {"team": "db"}, "receiver": _EMAIL},
                 {"match": {"severity": "critical"}, "receiver": _PD})
        assert critical_escalation_findings(rc) == _F(
            "routes[1] (severity=critical)",
            [("routes[0]", "team=db", "email")])

    def test_non_pagerduty_critical_route_ahead_is_a_shadow(self):
        rc = _rc({"match": {"severity": "critical", "team": "db"},
                  "receiver": _SLACK},
                 {"match": {"severity": "critical"}, "receiver": _PD})
        assert critical_escalation_findings(rc)[1] == [
            ("routes[0]", "severity=critical,team=db", "slack")]

    def test_pagerduty_subroute_ahead_is_not_a_shadow(self):
        rc = _rc({"match": {"severity": "critical"}, "receiver": _PD},
                 overrides=[{"alertname": "A", "receiver": _PD}])
        assert critical_escalation_findings(rc) == _F(
            "routes[0] (severity=critical)", [])

    def test_routes_after_the_target_are_not_shadows(self):
        rc = _rc({"match": {"severity": "critical"}, "receiver": _PD},
                 {"match": {"team": "db"}, "receiver": _EMAIL})
        assert critical_escalation_findings(rc)[1] == []

    def test_main_pagerduty_target_is_shadowed_by_every_catching_subroute(self):
        rc = _rc({"match": {"severity": "warning"}, "receiver": _EMAIL},
                 {"match": {"team": "db"}, "receiver": _SLACK},
                 overrides=[{"metric_group": "g", "receiver": _EMAIL}],
                 main=_PD)
        assert critical_escalation_findings(rc) == _F("the main receiver", [
            ("override[0]", "metric_group=g", "email"),
            ("routes[1]", "team=db", "slack")])

    def test_skipped_route_does_not_count_as_escalation(self):
        rc = _rc({"match": {"severity": "critical"}, "receiver": _PD,
                  "continue": True})
        assert critical_escalation_findings(rc) == _F(None, [])

    @pytest.mark.parametrize("match", [{"alertname": "X"},
                                       {"severity": "warning"}])
    def test_pagerduty_route_not_matching_critical_is_not_escalation(
            self, match):
        rc = _rc({"match": match, "receiver": _PD},
                 main={"type": "webhook",
                       "url": "https://hooks.example.com/main"})
        assert critical_escalation_findings(rc) == _F(None, [])

    def test_escalation_to_another_type_is_not_compliant(self):
        rc = _rc({"match": {"severity": "critical"}, "receiver": _EMAIL})
        assert critical_escalation_findings(rc) == _F(None, [])

    def test_shadow_warn_never_trips_validate_mode(self):
        routing = {"t-x": _rc(
            {"match": {"team": "db"}, "receiver": _EMAIL},
            {"match": {"severity": "critical"}, "receiver": _PD})}
        for strict in (True, False):
            msgs = check_domain_policies(routing, _policy(["t-x"]),
                                         strict=strict)
            assert len(msgs) == 1, msgs
            assert msgs[0].startswith("  WARN: ")
            assert "skipping" not in msgs[0]


# ============================================================
# 部分升級：升級路由的 match 除 severity 外還有別的 label
# ============================================================
_HOOK = {"type": "webhook", "url": "https://hooks.example.com/main"}
_PART_TENANT = {"t-part": {"_routing": {
    "receiver": _HOOK,
    "group_by": ["tenant", "alertname", "severity"],
    "routes": [{"match": {"severity": "critical", "alertname": "Z"},
                "receiver": _PD}]}}}
_PDMAIN_TENANT = {"t-pdmain": {"_routing": {
    "receiver": _PD,
    "group_by": ["tenant", "alertname", "severity"],
    "routes": [{"match": {"severity": "critical", "alertname": "Z"},
                "receiver": _PD},
               {"match": {"severity": "critical"}, "receiver": _EMAIL}]}}}
_PART_WARN = (
    "  WARN: domain_policy 'finance', tenant 't-part' routes[0] "
    "(severity=critical,alertname=Z): the critical escalation only covers "
    "alerts with alertname=Z; other severity=critical alerts go to the main "
    "receiver (type 'webhook'), not a receiver of type ['pagerduty']")


class TestPartialEscalation:

    def test_partial_escalation_names_the_main_receiver(self):
        rc = _rc({"match": {"severity": "critical", "alertname": "Z"},
                  "receiver": _PD}, main=_HOOK)
        assert critical_escalation_findings(rc) == _F(
            "routes[0] (severity=critical,alertname=Z)", [],
            ("alertname=Z", "the main receiver (type 'webhook')"))

    def test_plain_critical_route_is_not_partial(self):
        rc = _rc({"match": {"severity": "critical"}, "receiver": _PD},
                 main=_HOOK)
        assert critical_escalation_findings(rc).partial is None

    def test_fallthrough_to_a_later_catch_all_route(self):
        rc = _rc({"match": {"severity": "critical", "team": "db"},
                  "receiver": _PD},
                 {"match": {"severity": "critical", "team": "web"},
                  "receiver": _EMAIL},
                 {"match": {"severity": "critical"}, "receiver": _EMAIL},
                 main=_HOOK)
        assert critical_escalation_findings(rc).partial == (
            "team=db", "routes[2] (severity=critical, receiver type 'email')")

    def test_later_pagerduty_catch_all_is_not_partial(self):
        rc = _rc({"match": {"severity": "critical", "team": "db"},
                  "receiver": _PD},
                 {"match": {"severity": "critical"}, "receiver": _PD},
                 main=_HOOK)
        assert critical_escalation_findings(rc).partial is None

    def test_main_pagerduty_fallthrough_is_not_partial(self):
        rc = _rc({"match": {"severity": "critical", "alertname": "Z"},
                  "receiver": _PD}, main=_PD)
        assert critical_escalation_findings(rc).partial is None

    def test_main_pagerduty_but_later_catch_all_takes_critical(self):
        rc = _rc({"match": {"severity": "critical", "alertname": "Z"},
                  "receiver": _PD},
                 {"match": {"severity": "critical"}, "receiver": _EMAIL},
                 main=_PD)
        assert critical_escalation_findings(rc) == _F(
            "routes[0] (severity=critical,alertname=Z)", [],
            ("alertname=Z",
             "routes[1] (severity=critical, receiver type 'email')"))

    @pytest.mark.parametrize("strict", [True, False])
    def test_partial_is_a_plain_warn(self, tmp_path, strict):
        routing = _routing(_tree(tmp_path, extra=_PART_TENANT))
        msgs = check_domain_policies(routing, _policy(["t-part"]),
                                     strict=strict)
        assert msgs == [_PART_WARN]
        assert "skipping" not in msgs[0]

    def test_cli_strict_partial_does_not_block(self, tmp_path):
        r = _run(_tree(tmp_path, tenants=["t-part"], extra=_PART_TENANT),
                 "--strict")
        assert r.returncode == 0, r.stdout + r.stderr
        assert _PART_WARN in r.stderr


# ============================================================
# 對照：amtool 下，被報遮蔽的告警確實落在非 PagerDuty receiver
# （PATH 上有 amtool 才跑，同 test_routing_profile_routes.py）
# ============================================================
_AMTOOL = shutil.which("amtool")


@pytest.mark.skipif(_AMTOOL is None, reason="amtool not on PATH")
class TestAmtoolShadow:

    def test_reported_shadow_routes_critical_to_email(self, tmp_path):
        d = _tree(tmp_path, tenants=["t-ovr"])
        stderr = _check_routes(tmp_path, d, "t-ovr", [
            ("MariaDBDown", "tenant-t-ovr-override-0", "email_configs"),
            ("HighCPU", "tenant-t-ovr-route-0", "pagerduty_configs"),
        ])
        assert _OVR_SHADOW in stderr

    def test_reported_partial_escalation_leaves_critical_on_webhook(
            self, tmp_path):
        d = _tree(tmp_path, tenants=["t-part"], extra=_PART_TENANT)
        stderr = _check_routes(tmp_path, d, "t-part", [
            ("Y", "tenant-t-part", "webhook_configs"),
            ("Z", "tenant-t-part-route-0", "pagerduty_configs"),
        ])
        assert _PART_WARN in stderr

    def test_partial_with_pagerduty_main_leaves_critical_on_email(
            self, tmp_path):
        d = _tree(tmp_path, tenants=["t-pdmain"], extra=_PDMAIN_TENANT)
        stderr = _check_routes(tmp_path, d, "t-pdmain", [
            ("Y", "tenant-t-pdmain-route-1", "email_configs"),
            ("Z", "tenant-t-pdmain-route-0", "pagerduty_configs"),
        ])
        assert ("tenant 't-pdmain' routes[0] (severity=critical,alertname=Z): "
                "the critical escalation only covers alerts with alertname=Z; "
                "other severity=critical alerts go to routes[1] "
                "(severity=critical, receiver type 'email')") in stderr


def _check_routes(tmp_path: Path, d: Path, tenant: str, cases) -> str:
    """Render *d*, then amtool-route each (alertname, receiver, config key)
    at severity=critical; return the generator's stderr."""
    out = tmp_path / "cm.yaml"
    r = subprocess.run(
        [sys.executable, str(_GAR), "--config-dir", str(d),
         "--output-configmap", "-o", str(out)],
        capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    am = yaml.safe_load(
        yaml.safe_load(out.read_text(encoding="utf-8"))["data"]["alertmanager.yml"])
    am_yml = tmp_path / "alertmanager.yml"
    am_yml.write_text(yaml.safe_dump(am), encoding="utf-8")
    types = {rcv["name"]: next(
                 (k for k in rcv if k.endswith("_configs")), None)
             for rcv in am["receivers"]}
    for alertname, expected, cfg in cases:
        t = subprocess.run(
            [_AMTOOL, "config", "routes", "test", f"--config.file={am_yml}",
             f"--verify.receivers={expected}", f"alertname={alertname}",
             "severity=critical", f"tenant={tenant}"],
            capture_output=True, text=True, timeout=300)
        assert t.returncode == 0, (alertname, t.stdout + t.stderr)
        assert types[expected] == cfg
    return r.stderr
