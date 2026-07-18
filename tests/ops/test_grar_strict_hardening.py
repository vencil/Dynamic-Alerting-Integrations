"""ADR-007 --strict 盲審收口測試（F1/F2/F3/F4）。

盲審抓到的六個 strict 繞過情境（0s / 1h30m / -1h / banana / group_by 非
list / 壞 YAML policy 檔）在 strict 下必須 fail-loud ERROR；非 strict 行為
byte-identical（零 policy 訊息、exit 0）。另鎖 POLICY_ERROR_PREFIX 唯一
來源（_policy_errors() 的 blocking 判定不被其他 ERROR 字串汙染）。
"""
import os
import subprocess
import sys
import tempfile

import pytest
import yaml

from factories import write_yaml

from generate_alertmanager_routes import (  # noqa: E402
    POLICY_ERROR_PREFIX,
    check_domain_policies,
    load_tenant_configs,
)
from _grar_validate import _parse_policy_duration  # noqa: E402

TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(TESTS_DIR)
_OPS_DIR = os.path.join(REPO_ROOT, "scripts", "tools", "ops")
_SCRIPT = os.path.join(_OPS_DIR, "generate_alertmanager_routes.py")


def _wy(tmpdir, filename, data):
    """write_yaml wrapper that accepts dict and auto-dumps to YAML string."""
    write_yaml(tmpdir, filename, yaml.dump(data, default_flow_style=False))


# ============================================================
# policy-side duration parser（F2）
# ============================================================
class TestParsePolicyDuration:
    """multi-unit / fractional 接受；負值 / 亂字串明確拒絕。"""

    @pytest.mark.parametrize("value,expected", [
        ("0", 0.0),
        ("0s", 0.0),
        ("30s", 30.0),
        ("1h", 3600.0),
        ("1h30m", 5400.0),          # multi-unit（舊 parser 拒收 → 靜默跳過）
        ("1.5h", 5400.0),           # fractional（schema example）
        ("1m30s", 90.0),
        ("500ms", 0.5),
        ("1d", 86400.0),
        (30, 30.0),                  # 裸數值＝秒（沿用舊 parser 語義）
        (0, 0.0),
    ])
    def test_accepted(self, value, expected):
        assert _parse_policy_duration(value) == pytest.approx(expected)

    @pytest.mark.parametrize("value", [
        "-1h",          # 負值明確拒絕
        "-30s",
        "banana",
        "1x",
        "h",
        "",
        "  ",
        "1h banana",
        None,
        -5,             # 負裸數值
        True,           # bool 不是 duration
        ["1h"],
    ])
    def test_rejected(self, value):
        assert _parse_policy_duration(value) is None


# ============================================================
# strict fail-open 收口（unit 層）
# ============================================================
class TestStrictFailOpenClosures:
    """strict 一律 fail-loud ERROR；非 strict 行為不變（零 policy 訊息）。"""

    @staticmethod
    def _one_policy(constraints):
        return {"pol": {"tenants": ["tenant-x"], "constraints": constraints}}

    # ── F1: 邊界值 0s 被 truthiness 靜默跳過 ──
    def test_zero_group_wait_strict_violates(self):
        routing = {"tenant-x": {"group_wait": "0s"}}
        policies = self._one_policy({"min_group_wait": "30s"})
        strict = check_domain_policies(routing, policies, strict=True)
        assert any("below minimum" in m for m in strict), strict
        assert check_domain_policies(routing, policies) == []  # 非 strict 不變

    def test_bare_zero_group_wait_strict_violates(self):
        """裸 int 0 的 group_wait（falsy 但可 parse）不得被 truthiness 跳過。

        mutation pilot round 5 的存活 mutant（`is not None` → truthiness）
        揭露的缺口：既有測試只蓋 "0s"（truthy 字串），裸 0 走 falsy 分支。
        """
        routing = {"tenant-x": {"group_wait": 0}}
        policies = self._one_policy({"min_group_wait": "30s"})
        strict = check_domain_policies(routing, policies, strict=True)
        assert any("below minimum" in m for m in strict), strict
        assert check_domain_policies(routing, policies) == []  # 非 strict 不變

    def test_zero_repeat_interval_not_skipped(self):
        """0（裸 int）repeat_interval：parse 得出來、對 max 合規＝零訊息。"""
        routing = {"tenant-x": {"repeat_interval": 0}}
        policies = self._one_policy({"max_repeat_interval": "1h"})
        assert check_domain_policies(routing, policies, strict=True) == []
        assert check_domain_policies(routing, policies) == []

    # ── F2: multi-unit / 負值 / 亂字串 ──
    def test_multiunit_tenant_value_strict_enforced(self):
        routing = {"tenant-x": {"repeat_interval": "1h30m"}}
        policies = self._one_policy({"max_repeat_interval": "1h"})
        strict = check_domain_policies(routing, policies, strict=True)
        assert any("exceeds max" in m for m in strict), strict
        assert check_domain_policies(routing, policies) == []

    def test_multiunit_constraint_value_strict_enforced(self):
        routing = {"tenant-x": {"repeat_interval": "2h"}}
        policies = self._one_policy({"max_repeat_interval": "1h30m"})
        strict = check_domain_policies(routing, policies, strict=True)
        assert any("exceeds max" in m for m in strict), strict

    def test_negative_tenant_value_strict_fails_loud(self):
        routing = {"tenant-x": {"repeat_interval": "-1h"}}
        policies = self._one_policy({"max_repeat_interval": "1h"})
        strict = check_domain_policies(routing, policies, strict=True)
        assert any("not a valid duration" in m for m in strict), strict
        assert check_domain_policies(routing, policies) == []

    def test_garbage_constraint_value_strict_fails_loud(self):
        routing = {"tenant-x": {"repeat_interval": "4h"}}
        policies = self._one_policy({"max_repeat_interval": "banana"})
        strict = check_domain_policies(routing, policies, strict=True)
        assert any("'max_repeat_interval' value 'banana'" in m
                   and "not a valid duration" in m for m in strict), strict
        assert check_domain_policies(routing, policies) == []

    def test_garbage_min_group_wait_strict_fails_loud(self):
        routing = {"tenant-x": {"group_wait": "30s"}}
        policies = self._one_policy({"min_group_wait": "banana"})
        strict = check_domain_policies(routing, policies, strict=True)
        assert any("'min_group_wait' value 'banana'" in m for m in strict)
        assert check_domain_policies(routing, policies) == []

    # ── F3: 型別 fail-open ──
    def test_tenant_group_by_not_list_strict_fails_loud(self):
        routing = {"tenant-x": {"group_by": "tenant,alertname"}}
        policies = self._one_policy({"enforce_group_by": ["tenant"]})
        strict = check_domain_policies(routing, policies, strict=True)
        assert any("group_by must be a list" in m for m in strict), strict
        assert check_domain_policies(routing, policies) == []

    def test_policy_not_mapping_strict_fails_loud(self):
        routing = {"tenant-x": {}}
        policies = {"pol": "not-a-dict"}
        strict = check_domain_policies(routing, policies, strict=True)
        assert any("policy must be a mapping" in m for m in strict), strict
        assert check_domain_policies(routing, policies) == []

    def test_policy_null_is_inert_even_strict(self):
        """schema 允許 policy: null（inert）——strict 不誤殺。"""
        assert check_domain_policies({"t": {}}, {"pol": None},
                                     strict=True) == []

    def test_constraints_not_mapping_strict_fails_loud(self):
        routing = {"tenant-x": {}}
        policies = {"pol": {"tenants": ["tenant-x"],
                            "constraints": "not-a-dict"}}
        strict = check_domain_policies(routing, policies, strict=True)
        assert any("'constraints' must be a mapping" in m for m in strict)
        assert check_domain_policies(routing, policies) == []

    def test_constraints_null_is_inert_even_strict(self):
        policies = {"pol": {"tenants": ["tenant-x"], "constraints": None}}
        assert check_domain_policies({"tenant-x": {}}, policies,
                                     strict=True) == []

    def test_receiver_type_constraint_not_list_strict_fails_loud(self):
        """字串 forbidden_receiver_types 舊行為＝逐字元集合（靜默無效）。"""
        routing = {"tenant-x": {"receiver": {"type": "slack"}}}
        policies = self._one_policy({"forbidden_receiver_types": "slack"})
        strict = check_domain_policies(routing, policies, strict=True)
        assert any("'forbidden_receiver_types' must be a list" in m
                   for m in strict), strict
        assert check_domain_policies(routing, policies) == []

    def test_tenants_not_list_strict_becomes_error(self):
        """既有 WARN（'tenants' must be a list）在 strict 下升 ERROR。"""
        policies = {"pol": {"tenants": "tenant-x"}}
        strict = check_domain_policies({}, policies, strict=True)
        assert any(m.lstrip().startswith(POLICY_ERROR_PREFIX)
                   for m in strict)
        lenient = check_domain_policies({}, policies)
        assert lenient == [
            "  WARN: domain_policy 'pol': 'tenants' must be a list"]

    # ── F3: 檔案層 fail-open（load_tenant_configs 路徑） ──
    def test_broken_policy_yaml_strict_fails_loud(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_domain_policy.yaml",
                       "domain_policies:\n  fin: [unclosed\n")
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {"cpu": "90"}}})
            _, _, warnings, _, _ = load_tenant_configs(
                d, strict_policies=True)
            assert any("failed to parse" in w
                       and w.lstrip().startswith(POLICY_ERROR_PREFIX)
                       for w in warnings), warnings
            # 非 strict：warning stream 無 ERROR（僅 stderr print），照舊全綠
            _, _, lenient, _, _ = load_tenant_configs(d)
            assert not any(w.lstrip().startswith(POLICY_ERROR_PREFIX)
                           for w in lenient)

    def test_misplaced_domain_policies_strict_fails_loud(self):
        with tempfile.TemporaryDirectory() as d:
            _wy(d, "tenant-x.yaml", {
                "domain_policies": {"fin": {"tenants": ["tenant-x"]}},
                "tenants": {"tenant-x": {"cpu": "90"}},
            })
            _, _, warnings, _, _ = load_tenant_configs(
                d, strict_policies=True)
            assert any("only _domain_policy.yaml is loaded" in w
                       and w.lstrip().startswith(POLICY_ERROR_PREFIX)
                       for w in warnings), warnings
            _, _, lenient, _, _ = load_tenant_configs(d)
            assert not any(w.lstrip().startswith(POLICY_ERROR_PREFIX)
                           for w in lenient)


# ============================================================
# POLICY_ERROR_PREFIX 唯一來源鎖（F4）
# ============================================================
class TestPolicyErrorPrefixPin:
    """_policy_errors() 以此前綴判 blocking；若 _grar_* 家族其他模組把
    'ERROR:' 字面值寫進 validate 的 warning stream，會被誤判為
    domain-policy blocking。_grar_render 的 ERROR 字串只流入 --apply
    路徑（不進 validate 的 warning stream），故不在鎖定範圍。
    """

    def _src(self, name):
        with open(os.path.join(_OPS_DIR, name), encoding="utf-8") as f:
            return f.read()

    def test_routes_and_merge_have_no_error_prefix(self):
        for mod in ("_grar_routes.py", "_grar_merge.py"):
            assert "ERROR:" not in self._src(mod), \
                f"{mod} 出現 ERROR: 字面值——會汙染 --strict 的 blocking 判定"

    def test_parse_error_literal_only_fatal_print(self):
        """_grar_parse 的 'ERROR:' 字面值只允許 fatal print+exit 那一處。"""
        lines = [line for line in self._src("_grar_parse.py").splitlines()
                 if "ERROR:" in line and "POLICY_ERROR_PREFIX" not in line]
        assert len(lines) == 1 and "config directory not found" in lines[0], \
            f"非預期的 ERROR: 字面值: {lines}"

    def test_validate_error_literal_only_constant_definition(self):
        lines = [line for line in self._src("_grar_validate.py").splitlines()
                 if '"ERROR:"' in line]
        assert len(lines) == 1 and "POLICY_ERROR_PREFIX" in lines[0], \
            f'非預期的 "ERROR:" 字面值: {lines}'

    def test_facade_policy_errors_uses_constant(self):
        src = self._src("generate_alertmanager_routes.py")
        assert "POLICY_ERROR_PREFIX" in src
        assert 'startswith("ERROR:")' not in src


# ============================================================
# 盲審六情境 CLI exit-code 契約
# ============================================================
class TestStrictBypassScenariosCLI:
    """六個實跑繞過情境：strict exit 1 + ERROR；非 strict exit 0 無 ERROR。"""

    _BASE_DEFAULTS = {
        "receiver": {"type": "email", "to": ["oncall@example.com"],
                     "from": "alerting@example.com",
                     "smarthost": "smtp.example.com:587"},
        "group_by": ["tenant", "alertname"],
        "group_wait": "30s",
        "repeat_interval": "30m",
    }

    @classmethod
    def _write(cls, d, *, defaults_patch, constraints, raw_policy):
        defaults = dict(cls._BASE_DEFAULTS)
        defaults.update(defaults_patch)
        _wy(d, "_defaults.yaml", {
            "defaults": {"cpu": 80},
            "_routing_defaults": defaults,
        })
        if raw_policy is not None:
            write_yaml(d, "_domain_policy.yaml", raw_policy)
        else:
            _wy(d, "_domain_policy.yaml", {
                "domain_policies": {
                    "fin": {"tenants": ["tenant-fin"],
                            "constraints": constraints},
                }
            })
        _wy(d, "tenant-fin.yaml", {"tenants": {"tenant-fin": {"cpu": "90"}}})

    def _run(self, config_dir, *flags):
        return subprocess.run(
            [sys.executable, _SCRIPT, "--config-dir", config_dir,
             "--dry-run", "--validate", *flags],
            capture_output=True, text=True, encoding="utf-8", timeout=60)

    @pytest.mark.parametrize("defaults_patch,constraints,raw_policy,expect", [
        ({"group_wait": "0s"}, {"min_group_wait": "30s"}, None,
         "below minimum"),
        ({"repeat_interval": "1h30m"}, {"max_repeat_interval": "1h"}, None,
         "exceeds max"),
        ({"repeat_interval": "-1h"}, {"max_repeat_interval": "1h"}, None,
         "not a valid duration"),
        ({}, {"max_repeat_interval": "banana"}, None,
         "not a valid duration"),
        ({"group_by": "tenant,alertname"}, {"enforce_group_by": ["tenant"]},
         None, "group_by must be a list"),
        ({}, None, "domain_policies:\n  fin: [unclosed\n",
         "failed to parse"),
    ], ids=["zero-group-wait", "multiunit-repeat", "negative-repeat",
            "garbage-constraint", "group-by-not-list", "broken-policy-yaml"])
    def test_bypass_blocked_in_strict_open_in_lenient(
            self, defaults_patch, constraints, raw_policy, expect):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, defaults_patch=defaults_patch,
                        constraints=constraints, raw_policy=raw_policy)
            strict = self._run(d, "--strict")
            assert strict.returncode == 1, strict.stdout + strict.stderr
            assert expect in strict.stderr
            # 非 strict：同一 config 照舊通過（向後相容）
            lenient = self._run(d)
            assert lenient.returncode == 0, lenient.stdout + lenient.stderr
            assert "ERROR" not in lenient.stderr


# ============================================================
# validate_config.py --strict（F4b）
# ============================================================
class TestValidateConfigStrictCLI:
    """一站式 validate_config 的 --strict：與 CI 的 generate-routes 對齊。"""

    _VC_SCRIPT = os.path.join(_OPS_DIR, "validate_config.py")

    def _run(self, config_dir, *flags):
        return subprocess.run(
            [sys.executable, self._VC_SCRIPT, "--config-dir", config_dir,
             *flags],
            capture_output=True, text=True, encoding="utf-8", timeout=120)

    def _violating_dir(self, d):
        _wy(d, "_defaults.yaml", {
            "defaults": {"cpu": 80},
            "_routing_defaults": {
                "receiver": {"type": "slack",
                             "api_url": "https://hooks.slack.com/services/T/B/X"},
                "group_by": ["tenant", "alertname"],
                "group_wait": "30s",
                "repeat_interval": "30m",
            },
        })
        _wy(d, "_domain_policy.yaml", {
            "domain_policies": {
                "fin": {"tenants": ["tenant-fin"],
                        "constraints": {"forbidden_receiver_types": ["slack"]}},
            }
        })
        _wy(d, "tenant-fin.yaml", {"tenants": {"tenant-fin": {"cpu": "90"}}})

    def test_strict_violation_exits_nonzero(self):
        with tempfile.TemporaryDirectory() as d:
            self._violating_dir(d)
            result = self._run(d, "--strict")
            assert result.returncode == 1, result.stdout + result.stderr
            assert "ERROR" in result.stdout

    def test_lenient_violation_still_passes(self):
        with tempfile.TemporaryDirectory() as d:
            self._violating_dir(d)
            result = self._run(d)
            assert result.returncode == 0, result.stdout + result.stderr
