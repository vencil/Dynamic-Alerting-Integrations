"""#2245：ADR-007 routing profile / tenant `_routing` 的 `routes:`（依 label 等值分流）。

釘住的契約：

* 每條 `routes` 產成 tenant 主路由底下的子路由，排在 overrides 之後
  （overrides → routes → 主路由自己的 receiver），只帶自己 `match` 轉成的
  等值 matcher；`tenant="<id>"` 只在父節點；沒寫的 timing / group_by 不寫出
  （由 Alertmanager 從主路由繼承），有寫的走同一套 timing 護欄。
* 合併是淺合併：tenant `_routing.routes` 整份取代 profile 的 `routes`。
* 不合法的條目以 `WARN … skipping` 略過（`--validate` 失敗）；
  `_routing_defaults.routes` 在解析時就丟掉並報同一種錯。
* domain policy（`list_tenant_subroutes`）與 `--policy` 網域檢查涵蓋這些
  receiver，沒寫的值依 #2252 的補值規則用主路由的值檢查。
* `routing-profiles.schema.json` 只 `$ref` tenant 的 `routing` 定義；
  `check_confd_schema` 與 validate-config 的引號檢查涵蓋 `_routing_profiles.yaml`。
* `explain_route` 列出實際產出的子路由，不把被略過的條目當成已生效。

Fixture 的 tenant 名一律是 demo 名（tenant-agnostic）。
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from generate_alertmanager_routes import (
    check_domain_policies,
    expand_routing_routes,
    generate_routes,
    load_tenant_tree,
)
from _grar_validate import list_tenant_subroutes, route_entry_matchers

REPO = Path(__file__).resolve().parents[2]
_GAR = REPO / "scripts" / "tools" / "ops" / "generate_alertmanager_routes.py"
_EXPLAIN = REPO / "scripts" / "tools" / "ops" / "explain_route.py"
_EXAMPLE_PROFILES = (REPO / "components" / "threshold-exporter" / "config"
                     / "conf.d" / "examples" / "_routing_profiles.yaml")
_SCHEMAS = REPO / "docs" / "schemas"

sys.path.insert(0, str(REPO / "scripts" / "tools" / "lint"))
import check_confd_schema as ccs  # noqa: E402

_T = "demo-apac"
_MAIN = {"type": "slack", "api_url": "https://hooks.slack.com/services/T/B/main"}
_PD = {"type": "pagerduty", "service_key": "k"}
_HOOK = {"type": "webhook", "url": "https://hooks.example.com/ov"}


def _route(match=None, receiver=None, **kw):
    entry = {"match": {"severity": "critical"} if match is None else match,
             "receiver": _PD if receiver is None else receiver}
    entry.update(kw)
    return entry


def _cfg(*routes, overrides=None, **main):
    cfg = {"receiver": _MAIN, **main}
    if routes:
        cfg["routes"] = list(routes)
    if overrides:
        cfg["overrides"] = list(overrides)
    return {_T: cfg}


def _write(d: Path, name: str, data) -> None:
    (d / name).write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _profiles_tree(tmp_path: Path, tenant_routing: dict | None = None) -> Path:
    """範例 `_routing_profiles.yaml` + 一個引用 team-sre-apac 的 demo tenant。"""
    d = tmp_path / "conf.d"
    d.mkdir()
    shutil.copy(_EXAMPLE_PROFILES, d / "_routing_profiles.yaml")
    body = {"_routing_profile": "team-sre-apac"}
    if tenant_routing is not None:
        body["_routing"] = tenant_routing
    _write(d, f"{_T}.yaml", {"tenants": {_T: body}})
    return d


# ============================================================
# 產生器：子路由的位置、形狀與略過規則
# ============================================================
class TestRoutesRenderAsChildren:

    def test_example_profile_routes_render_after_overrides(self, tmp_path):
        """驗收：team-sre-apac 範例 → 主路由底下 severity="critical" 子路由，
        在 override 之後；只帶自己的 matcher 與自己宣告的 repeat_interval。"""
        d = _profiles_tree(tmp_path, {"overrides": [
            {"alertname": "DiskFull", "receiver": _HOOK}]})
        tree = load_tenant_tree(str(d))
        routes, receivers, warnings = generate_routes(tree.routing_configs)
        assert warnings == []
        parent = routes[0]
        assert parent["matchers"] == [f'tenant="{_T}"']
        assert parent["repeat_interval"] == "4h"
        assert parent["routes"] == [
            {"matchers": ['alertname="DiskFull"'],
             "receiver": f"tenant-{_T}-override-0"},
            {"matchers": ['severity="critical"'],
             "receiver": f"tenant-{_T}-route-0", "repeat_interval": "15m"},
        ]
        by_name = {r["name"]: r for r in receivers}
        assert by_name[f"tenant-{_T}-route-0"]["pagerduty_configs"] == [
            {"service_key": "sre-apac-critical-key"}]

    def test_undeclared_timing_and_group_by_are_left_to_the_parent(self):
        routes, _, _ = generate_routes(_cfg(
            _route(), repeat_interval="30m", group_by=["alertname"]))
        child = routes[0]["routes"][0]
        assert set(child) == {"matchers", "receiver"}, child

    def test_declared_timing_goes_through_the_guardrails(self):
        routes, _, warnings = generate_routes(_cfg(
            _route(group_wait="1s", group_by=["alertname", "instance"])))
        child = routes[0]["routes"][0]
        assert child["group_wait"] == "5s"
        assert child["group_by"] == ["alertname", "instance"]
        assert any(f"{_T}-route-0: group_wait '1s' below minimum" in w
                   for w in warnings), warnings

    def test_multi_label_match_and_value_escaping(self):
        routes, _, warnings = generate_routes(_cfg(
            _route(match={"severity": "critical", "team": 'a"b\\c'})))
        assert warnings == []
        assert routes[0]["routes"][0]["matchers"] == [
            'severity="critical"', 'team="a\\"b\\\\c"']

    def test_override_and_route_receivers_do_not_collide(self):
        routes, receivers, _ = generate_routes(_cfg(
            _route(), overrides=[{"alertname": "A", "receiver": _HOOK}]))
        names = [r["name"] for r in receivers]
        assert len(names) == len(set(names)), names
        assert [c["receiver"] for c in routes[0]["routes"]] == [
            f"tenant-{_T}-override-0", f"tenant-{_T}-route-0"]

    def test_empty_routes_list_renders_nothing(self):
        routes, _, warnings = generate_routes({_T: {"receiver": _MAIN,
                                                    "routes": []}})
        assert warnings == []
        assert "routes" not in routes[0]

    @pytest.mark.parametrize("entry, needle", [
        ({"match": {}, "receiver": _PD}, "non-empty 'match'"),
        ({"receiver": _PD}, "non-empty 'match'"),
        ({"match": "severity=critical", "receiver": _PD}, "non-empty 'match'"),
        ({"match": {"severity": True}, "receiver": _PD},
         "match value for 'severity' must be a string"),
        ({"match": {"severity": ""}, "receiver": _PD},
         "match value for 'severity' is empty"),
        ({"match": {"bad-label": "x"}, "receiver": _PD}, "not a valid label name"),
        ({"match": {"severity": "critical"}, "receiver": _PD, "continue": True},
         "unsupported key(s) ['continue']"),
        ({"match_re": {"severity": "crit.*"}, "receiver": _PD},
         "unsupported key(s) ['match_re']"),
        ({"match": {"severity": "critical"}}, "missing 'receiver'"),
        ("not-a-mapping", "must be a dict"),
    ])
    def test_invalid_entry_is_skipped_loudly(self, entry, needle):
        routes, receivers, warnings = generate_routes(_cfg(entry))
        assert "routes" not in routes[0]
        assert [r["name"] for r in receivers] == [f"tenant-{_T}"]
        hits = [w for w in warnings if needle in w]
        assert hits and all("skipping" in w for w in hits), warnings

    def test_non_list_routes_is_skipped_loudly(self):
        _, _, warnings = generate_routes({_T: {"receiver": _MAIN,
                                               "routes": {"severity": "x"}}})
        assert warnings == [f"  WARN: {_T}: 'routes' must be a list, skipping"]

    def test_allowed_domains_blocks_a_routes_webhook(self):
        bad = {"type": "webhook", "url": "https://evil.example.net/x"}
        routes, receivers, warnings = generate_routes(
            _cfg(_route(receiver=bad)),
            allowed_domains=["hooks.slack.com"])
        assert "routes" not in routes[0]
        assert f"tenant-{_T}-route-0" not in [r["name"] for r in receivers]
        assert any("not in allowed_domains" in w and f"{_T}-route-0" in w
                   for w in warnings), warnings


class TestRouteEntryPredicate:
    """`route_entry_matchers` 是產生器與 domain policy 共用的唯一判斷。"""

    def test_valid_entry(self):
        assert route_entry_matchers(_route(), 0, _T) == (
            ['severity="critical"'], [])

    def test_quote_escapes_newline(self):
        m, _ = route_entry_matchers(_route(match={"x": "a\nb"}), 0, _T)
        assert m == ['x="a\\nb"']

    def test_expand_uses_the_same_predicate(self):
        """反證：predicate 拒收的條目，產生器也不產出。"""
        entry = _route(match={"severity": 1})
        assert route_entry_matchers(entry, 0, _T)[0] is None
        subs, recvs, _ = expand_routing_routes(_T, {"routes": [entry]})
        assert subs == [] and recvs == []


# ============================================================
# 合併語意：淺合併、`_routing_defaults` 不接受 routes
# ============================================================
class TestMergeSemantics:

    def test_tenant_routes_replace_profile_routes_wholesale(self, tmp_path):
        d = _profiles_tree(tmp_path, {"routes": [
            _route(match={"team": "db"}, receiver=_HOOK)]})
        routes, _, _ = generate_routes(load_tenant_tree(str(d)).routing_configs)
        assert [c["matchers"] for c in routes[0]["routes"]] == [['team="db"']]

    def test_tenant_empty_routes_drops_profile_routes(self, tmp_path):
        d = _profiles_tree(tmp_path, {"routes": []})
        routes, _, _ = generate_routes(load_tenant_tree(str(d)).routing_configs)
        assert "routes" not in routes[0]

    def test_routing_defaults_routes_is_dropped_and_blocking(self, tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        _write(d, "_defaults.yaml", {"_routing_defaults": {
            "receiver": _MAIN, "routes": [_route()]}})
        _write(d, f"{_T}.yaml", {"tenants": {_T: {}}})
        tree = load_tenant_tree(str(d))
        assert "routes" not in tree.routing_configs[_T]
        hits = [w for w in tree.schema_warnings if "_routing_defaults" in w]
        assert len(hits) == 1 and "'routes' is not supported" in hits[0]
        assert "skipping" in hits[0]
        routes, _, _ = generate_routes(tree.routing_configs)
        assert "routes" not in routes[0]
        r = subprocess.run([sys.executable, str(_GAR), "--config-dir", str(d),
                            "--validate"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert r.returncode == 1, r.stdout + r.stderr


# ============================================================
# Domain policy 與 `--validate --strict`
# ============================================================
class TestDomainPolicyCoversRoutes:

    @staticmethod
    def _policy(**constraints):
        return {"sre": {"tenants": [_T], "constraints": constraints}}

    def test_lister_includes_routes_after_overrides(self):
        rc = _cfg(_route(match={"severity": "critical", "team": "db"}),
                  overrides=[{"alertname": "A", "receiver": _HOOK}],
                  repeat_interval="4h")[_T]
        listed = list_tenant_subroutes(rc)
        assert [(ref, match) for ref, match, _c, _i in listed] == [
            ("override[0]", "alertname=A"),
            ("routes[0]", "severity=critical,team=db")]
        _ref, _m, eff, inherited = listed[1]
        assert eff["repeat_interval"] == "4h"
        assert inherited == frozenset({"repeat_interval"})

    def test_lister_skips_entries_the_generator_skips(self):
        rc = _cfg({"match": {}, "receiver": _PD},
                  {"match": {"severity": "x"}, "receiver": _PD, "continue": True},
                  {"match": {"severity": "x"}})[_T]
        assert list_tenant_subroutes(rc) == []

    def test_forbidden_routes_receiver_strict_error_names_it(self):
        routing = _cfg(_route(receiver={"type": "slack",
                                        "api_url": "https://hooks.slack.com/x"}))
        routing[_T]["receiver"] = _PD
        msgs = check_domain_policies(
            routing, self._policy(forbidden_receiver_types=["slack"]),
            strict=True)
        assert msgs == [
            f"  ERROR: domain_policy 'sre', tenant '{_T}' routes[0] "
            "(severity=critical): receiver type 'slack' is forbidden — fix: "
            "domain forbids ['slack']; switch routes[0]'s receiver.type to a "
            "compliant type or amend the domain policy"]

    def test_undeclared_repeat_interval_checked_with_the_inherited_value(self):
        routing = _cfg(_route(), repeat_interval="24h")
        routing[_T]["receiver"] = _PD
        msgs = check_domain_policies(
            routing, self._policy(max_repeat_interval="1h"), strict=True)
        sub = [m for m in msgs if "routes[0]" in m]
        assert len(sub) == 1, msgs
        assert "repeat_interval '24h' (inherited from the tenant's main " \
               "route) exceeds max '1h'" in sub[0]

    def test_cli_validate_strict_blocks_profile_routes_violation(self, tmp_path):
        """驗收：`routes` 的 receiver 違反 domain policy → `--validate --strict`
        非 0；不加 --strict 仍為 0（WARN）。"""
        d = _profiles_tree(tmp_path)
        _write(d, "_domain_policy.yaml", {"domain_policies": {"sre": {
            "tenants": [_T],
            "constraints": {"forbidden_receiver_types": ["pagerduty"]}}}})
        strict = subprocess.run(
            [sys.executable, str(_GAR), "--config-dir", str(d), "--validate",
             "--strict"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert strict.returncode == 1, strict.stdout + strict.stderr
        assert (f"tenant '{_T}' routes[0] (severity=critical): receiver type "
                "'pagerduty' is forbidden") in strict.stderr
        lenient = subprocess.run(
            [sys.executable, str(_GAR), "--config-dir", str(d), "--validate"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert lenient.returncode == 0, lenient.stdout + lenient.stderr


# ============================================================
# Schema：一份定義、profile `$ref` 它
# ============================================================
def _load(name: str) -> dict:
    return json.loads((_SCHEMAS / name).read_text(encoding="utf-8"))


class TestSchemaConsolidation:

    def test_profile_schema_refs_the_tenant_routing_definition(self):
        prof = _load("routing-profiles.schema.json")
        rp = prof["properties"]["routing_profiles"]
        assert rp["additionalProperties"] == {
            "$ref": "tenant-config.schema.json#/definitions/routing"}
        assert "definitions" not in prof  # routingProfile / profileSubRoute 已刪

    def test_tenant_routing_definition_carries_routes(self):
        tenant = _load("tenant-config.schema.json")
        props = tenant["definitions"]["routing"]["properties"]
        assert props["routes"]["items"] == {"$ref": "#/definitions/routingRoute"}
        route = tenant["definitions"]["routingRoute"]
        assert set(route["properties"]) == {
            "match", "receiver", "group_by", "group_wait", "group_interval",
            "repeat_interval"}
        assert route["additionalProperties"] is False

    def test_schema_route_keys_match_the_generator(self):
        """schema 的欄位清單與產生器接受的鍵是同一組（反向漂移守門）。"""
        from _grar_validate import ROUTE_ENTRY_KEYS
        route = _load("tenant-config.schema.json")["definitions"]["routingRoute"]
        assert set(route["properties"]) == set(ROUTE_ENTRY_KEYS)

    def test_routing_defaults_definition_rejects_routes(self):
        rd = _load("tenant-config.schema.json")["definitions"]["routingDefaults"]
        assert "routes" not in rd["properties"]
        assert rd["additionalProperties"] is False

    @pytest.fixture
    def validate(self):
        jsonschema = pytest.importorskip("jsonschema")
        tenant = _load("tenant-config.schema.json")
        prof = _load("routing-profiles.schema.json")
        platform = _load("platform-defaults.schema.json")
        reg = ccs.checked_schema_registry(tenant, platform, prof)

        def _v(doc, schema=prof):
            try:
                jsonschema.validate(doc, schema, registry=reg)
            except jsonschema.ValidationError as exc:
                return exc.message
            return None
        _v.platform = platform
        return _v

    def test_example_profiles_file_is_valid(self, validate):
        doc = yaml.safe_load(_EXAMPLE_PROFILES.read_text(encoding="utf-8"))
        assert validate(doc) is None

    @pytest.mark.parametrize("entry", [
        {"match": {}, "receiver": _PD},
        {"match": {"severity": "critical"}, "receiver": _PD, "continue": True},
        {"match_re": {"severity": ".*"}, "receiver": _PD},
        {"match": {"severity": True}, "receiver": _PD},
        {"match": {"severity": ""}, "receiver": _PD},
        {"match": {"bad-label": "x"}, "receiver": _PD},
        {"match": {"severity": "critical"}},
    ])
    def test_invalid_route_entries_rejected(self, validate, entry):
        doc = {"routing_profiles": {"p": {"receiver": _MAIN, "routes": [entry]}}}
        assert validate(doc) is not None

    def test_routing_defaults_routes_rejected_by_platform_schema(self, validate):
        # #2386: `defaults` is required in every `_defaults*` file.
        doc = {"defaults": None,
               "_routing_defaults": {"receiver": _MAIN, "routes": [_route()]}}
        assert validate(doc, validate.platform) is not None
        del doc["_routing_defaults"]["routes"]
        assert validate(doc, validate.platform) is None


class TestConfdLintCoversProfiles:

    def _tree(self, tmp_path, text: str) -> Path:
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "_routing_profiles.yaml").write_text(text, encoding="utf-8")
        return d

    _YES = ("routing_profiles:\n  team-x:\n    receiver:\n      type: slack\n"
            "      api_url: \"https://hooks.slack.com/services/T/B/x\"\n"
            "      channel: yes\n")

    def test_channel_yes_in_profile_fails_check_confd_schema(self, tmp_path):
        """驗收：profile 裡 `channel: yes` 被 lint 擋下（#2164 引號檢查）。"""
        d = self._tree(tmp_path, self._YES)
        r = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "tools" / "lint"
                                 / "check_confd_schema.py"), "--config-dir", str(d)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert r.returncode == 1, r.stdout + r.stderr
        assert ("_routing_profiles.yaml:6: /routing_profiles/team-x/receiver/"
                "channel: unquoted 'yes'") in r.stderr

    def test_channel_yes_in_profile_fails_validate_config(self, tmp_path):
        import validate_config
        d = self._tree(tmp_path, self._YES)
        row = validate_config.check_yaml_quoting(str(d))
        assert row["status"] == validate_config.FAIL, row
        assert any("_routing_profiles.yaml:6" in x for x in row["details"]), row

    def test_quoted_channel_passes(self, tmp_path):
        d = self._tree(tmp_path, self._YES.replace("channel: yes",
                                                   'channel: "yes"'))
        jsonschema = pytest.importorskip("jsonschema")
        checked, viol, skipped = ccs.validate_dir(
            str(d), _load("tenant-config.schema.json"), jsonschema,
            _load("platform-defaults.schema.json"),
            _load("routing-profiles.schema.json"))
        assert (checked, viol, skipped) == (1, [], [])

    def test_without_profiles_schema_the_file_stays_skipped(self, tmp_path):
        """舊呼叫（不給 profiles schema）行為不變：列為略過。"""
        d = self._tree(tmp_path, self._YES)
        jsonschema = pytest.importorskip("jsonschema")
        _c, viol, skipped = ccs.validate_dir(
            str(d), _load("tenant-config.schema.json"), jsonschema,
            _load("platform-defaults.schema.json"))
        assert viol == [] and skipped == ["_routing_profiles.yaml"]

    def test_unresolvable_profiles_ref_is_a_caller_error(self):
        pytest.importorskip("jsonschema")
        tenant = _load("tenant-config.schema.json")
        tenant_no_id = {k: v for k, v in tenant.items() if k != "$id"}
        with pytest.raises(ccs.UnresolvableSchemaRef, match="routing_profiles"):
            ccs.checked_schema_registry(
                tenant_no_id, {"$id": "x", "type": "object"},
                _load("routing-profiles.schema.json"))


# ============================================================
# explain_route：列出實際產出的子路由
# ============================================================
class TestExplainRouteShowsEffectiveSubRoutes:

    def test_sub_routes_in_render_order_and_skips_listed(self, tmp_path):
        import explain_route
        from generate_alertmanager_routes import _parse_config_files
        d = _profiles_tree(tmp_path, {
            "overrides": [{"alertname": "DiskFull", "receiver": _HOOK}],
            "routes": [_route(), {"match": {}, "receiver": _PD}]})
        exp = explain_route.explain_tenant_routing(_parse_config_files(str(d)), _T)
        assert [(s["source"], s["matchers"], s["receiver_type"])
                for s in exp["sub_routes"]] == [
            ("overrides[0]", ['alertname="DiskFull"'], "webhook"),
            ("routes[0]", ['severity="critical"'], "pagerduty")]
        assert len(exp["skipped_sub_routes"]) == 1
        assert "routes[1]" in exp["skipped_sub_routes"][0]
        text = explain_route.format_explanation(exp)
        final_block = text.split("Final merged result:")[1].split("Effective")[0]
        assert "routes" not in final_block and "overrides" not in final_block
        assert "2. routes[0]: severity=\"critical\"" in text
        assert "Not in effect (skipped by the generator):" in text

    # #2293: --trace takes the delivery from `amtool config routes test`.
    @pytest.mark.usefixtures("amtool_required")
    def test_trace_critical_lands_on_the_routes_receiver(self, tmp_path):
        import explain_route
        from generate_alertmanager_routes import _parse_config_files
        parsed = _parse_config_files(str(_profiles_tree(tmp_path)))
        crit = explain_route.trace_alert_routing(parsed, _T, "X", "critical")
        assert crit["steps"][1]["matched_sub_route"] == "routes[0]"
        assert f"tenant-{_T}-route-0" in crit["final_receiver"]
        assert crit["timing"]["repeat_interval"] == "15m"
        warn = explain_route.trace_alert_routing(parsed, _T, "X", "warning")
        assert "matched_sub_route" not in warn["steps"][1]


# ============================================================
# 驗收：真的 amtool（PATH 上有才跑；CI 的 docker 版在
# test_alertmanager_routing_correctness.py）
# ============================================================
_AMTOOL = shutil.which("amtool")


@pytest.mark.usefixtures("amtool_required")  # VIBE_REQUIRE_AMTOOL=1 → fail, not skip
class TestAmtoolAcceptance:

    def test_configmap_passes_check_config_and_routes_critical(self, tmp_path):
        d = _profiles_tree(tmp_path, {"overrides": [
            {"alertname": "DiskFull", "receiver": _HOOK}]})
        out = tmp_path / "cm.yaml"
        r = subprocess.run(
            [sys.executable, str(_GAR), "--config-dir", str(d),
             "--output-configmap", "-o", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        am_yml = tmp_path / "alertmanager.yml"
        am_yml.write_text(
            yaml.safe_load(out.read_text(encoding="utf-8"))["data"]["alertmanager.yml"],
            encoding="utf-8")
        chk = subprocess.run([_AMTOOL, "check-config", str(am_yml)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert chk.returncode == 0, chk.stdout + chk.stderr
        for labels, expected in [
            ({"tenant": _T, "severity": "critical"}, f"tenant-{_T}-route-0"),
            ({"tenant": _T, "severity": "warning"}, f"tenant-{_T}"),
            ({"tenant": _T, "severity": "critical", "alertname": "DiskFull"},
             f"tenant-{_T}-override-0"),
        ]:
            t = subprocess.run(
                [_AMTOOL, "config", "routes", "test",
                 f"--config.file={am_yml}", f"--verify.receivers={expected}"]
                + [f"{k}={v}" for k, v in sorted(labels.items())],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
            assert t.returncode == 0, (labels, t.stdout + t.stderr)

    @pytest.mark.parametrize("label, expected", [
        ("metric_group=mg-conn", f"tenant-{_T}-override-0"),
        ("team=x", f"tenant-{_T}-route-0"),
    ])
    def test_trace_label_agrees_with_amtool(self, tmp_path, label, expected):
        """#2264：`explain_route --trace --label` 選到的 receiver 與 amtool
        對同一組 label 的判定一致（override 的 metric_group、routes 的 match key）。"""
        d = _profiles_tree(tmp_path, {
            "overrides": [{"metric_group": "mg-conn", "receiver": _HOOK}],
            "routes": [_route(match={"team": "x"})]})
        out = tmp_path / "cm.yaml"
        r = subprocess.run(
            [sys.executable, str(_GAR), "--config-dir", str(d),
             "--output-configmap", "-o", str(out)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
        am_yml = tmp_path / "alertmanager.yml"
        am_yml.write_text(
            yaml.safe_load(out.read_text(encoding="utf-8"))["data"]["alertmanager.yml"],
            encoding="utf-8")
        tr = subprocess.run(
            [sys.executable, str(_EXPLAIN), "--config-dir", str(d),
             "--tenant", _T, "--trace", "--alertname", "X",
             "--label", label, "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert tr.returncode == 0, tr.stdout + tr.stderr
        [trace] = json.loads(tr.stdout)
        assert f"{expected} " in trace["final_receiver"]
        am = subprocess.run(
            [_AMTOOL, "config", "routes", "test", f"--config.file={am_yml}",
             f"--verify.receivers={expected}"]
            + [f"{k}={v}" for k, v in sorted(trace["labels"].items())],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        assert am.returncode == 0, (trace["labels"], am.stdout + am.stderr)
