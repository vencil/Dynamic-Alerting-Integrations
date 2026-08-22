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


# ============================================================
# #1448 — every way a policy file falls out of the scan has to be recorded
# the same way, because they all have the same consequence.
# ============================================================
class TestUnusablePolicyFileIsAlwaysRecorded:
    """A ``_domain_policy.yaml`` the reader cannot use drops **every** domain
    policy, whatever stopped it being usable.

    ⛔ #1447 added a second way for a file to drop out of the scan (it parses
    but is not a mapping) and modelled it on the first (it does not parse)
    *except* for the bookkeeping ``--strict`` reads. Measured on
    ``generate-routes --validate --strict`` over an otherwise clean conf.d:

    ⛔ Measured against **this branch's base** (``cd4dbd05``), which is not
    the same tree an earlier draft of this docstring quoted::

    ==========================================  ====  ============
    ``_domain_policy.yaml``                      rc    policy ERROR
    ==========================================  ====  ============
    absent (control — proves rc=0 is reachable)  0     0
    valid, and the routing violates it           1     1
    syntax error (control — the loud sibling)    1     1
    a bare YAML list                             1     0  (traceback)
    a lone scalar                                1     0  (traceback)
    ``domain_policies:`` is a list or null       0     0  ← silent
    ==========================================  ====  ============

    Only the last row printed ``OK: all configs valid`` on base; the two
    whole-file shapes above it **crashed** there. An earlier version of this
    table reported those two as ``0 ← silent``, which was measured on an
    abandoned attempt that is not in this history — the same lineage
    mistake, in a second file. ``_grar_parse._drop_unusable_policy``'s
    docstring carries the authoritative version of this table.

    This class is parametrised over *why the file is unusable* rather than
    pinned to the shapes above, so a third way to drop a file has to answer
    it too.
    """

    UNUSABLE = [
        ("broken-yaml", "domain_policies:\n  fin: [unclosed\n"),
        ("bare-list", "- domain_policies:\n    fin:\n      tenants: [t]\n"),
        ("bare-scalar", "domain_policies\n"),
        ("list-of-scalars", "- a\n- b\n"),
    ]

    @pytest.mark.parametrize("label,body", UNUSABLE,
                             ids=[i for i, _ in UNUSABLE])
    def test_strict_reports_it(self, label, body):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_domain_policy.yaml", body)
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {"cpu": "90"}}})
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            blocking = [w for w in warnings
                        if w.lstrip().startswith(POLICY_ERROR_PREFIX)]
            assert blocking, (
                f"an unusable _domain_policy.yaml ({label}) dropped every "
                f"domain policy without --strict saying so: {warnings}")
            assert any("_domain_policy.yaml" in w for w in blocking), blocking

    @pytest.mark.parametrize("label,body", UNUSABLE,
                             ids=[i for i, _ in UNUSABLE])
    def test_the_message_states_this_files_own_cause_and_remedy(self,
                                                                label, body):
        """⛔ The remedy used to be one fixed sentence written by the
        consumer — "fix: repair the YAML syntax in _domain_policy.yaml" —
        which is the right instruction for exactly one of these rows and
        sends the reader of the others hunting for a syntax error in a file
        whose syntax is fine.
        """
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_domain_policy.yaml", body)
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {"cpu": "90"}}})
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            msg = " ".join(w for w in warnings
                           if w.lstrip().startswith(POLICY_ERROR_PREFIX))
            assert "— fix:" in msg, msg
            if label == "broken-yaml":
                assert "repair the YAML syntax" in msg, msg
            else:
                assert "must be a mapping" in msg, msg
                assert "repair the YAML syntax" not in msg, (
                    "this file parses — telling the reader to repair its "
                    f"syntax sends them looking for nothing: {msg}")

    def test_a_usable_policy_file_records_nothing(self):
        """Must-tolerate control.

        Without it, "record everything" passes every assertion above while
        turning a healthy conf.d red — and the parametrised rows could not
        tell a working recogniser from one that always fires.
        """
        with tempfile.TemporaryDirectory() as d:
            _wy(d, "_domain_policy.yaml", {
                "domain_policies": {"fin": {"tenants": ["tenant-x"]}}})
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {"cpu": "90"}}})
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            assert not [w for w in warnings
                        if "could not be used" in w], warnings

    def test_a_wrong_shaped_file_that_is_not_the_policy_file_is_not(self):
        """Second must-tolerate side: the bookkeeping is for policy files.

        A tenant file with the same wrong shape is skipped with a warning on
        stderr, exactly as before — it does not silently disable a
        cross-cutting control, so it does not get a blocking ERROR.
        """
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "tenant-x.yaml", "- not: a mapping\n")
            _wy(d, "_defaults.yaml", {"defaults": {"cpu": 80}})
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            assert not [w for w in warnings
                        if w.lstrip().startswith(POLICY_ERROR_PREFIX)], warnings


class TestNullValuedKeysDoNotKillTheRun:
    """``key:`` with everything under it commented out parses to ``None``.

    ⛔ ``.get(k, {})`` returns that ``None`` — the default only fires when
    the key is *absent* — and the next call raises ``AttributeError``. That
    is #1448's root cause class, and the file it killed is an ordinary one:
    somebody commented out their tenants for an afternoon.
    """

    @pytest.mark.parametrize("body", [
        "tenants:\n",
        "tenants:\n  # db-a:\n  #   cpu: 90\n",
        "tenants: null\n",
    ], ids=["bare-key", "all-commented-out", "explicit-null"])
    def test_null_tenants_block(self, body):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "tenant-x.yaml", body)
            _wy(d, "_defaults.yaml", {"defaults": {"cpu": 80}})
            routing, _, _, _, _ = load_tenant_configs(d)
            assert routing == {}

    def test_null_defaults_block(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml", "defaults:\n")
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {"cpu": "90"}}})
            _, _, warnings, _, _ = load_tenant_configs(d)
            assert isinstance(warnings, list)

    def test_a_populated_tenants_block_is_still_read(self):
        """Must-succeed control: `or {}` must not swallow real content."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d, "_defaults.yaml", {"defaults": {"cpu": 80}})
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {
                "_routing": {"receiver": {
                    "type": "webhook",
                    "url": "https://hooks.example.com/a"}}}}})
            routing, _, _, _, _ = load_tenant_configs(d)
            assert "tenant-x" in routing


class TestEveryWayThePoliciesFallOnTheFloor:
    """#1448: the drop paths are recorded because they share a consequence.

    ⛔ Parametrised over *what makes the policies unreachable*, not over
    whole-file shapes — an earlier draft claimed the latter was "derived"
    while structurally being unable to reach the case where the file is a
    perfectly good mapping and only the ``domain_policies:`` value is wrong.
    Blind review found exactly that: measured on this branch's base, a
    ``domain_policies:`` holding a list, a null or a scalar printed
    ``OK: all configs valid`` and exited **0** while enforcing nothing.
    """

    UNUSABLE = [
        # the file cannot be read or parsed at all
        ("broken-yaml", b"domain_policies:\n  fin: [unclosed\n"),
        ("not-utf8",
         "domain_policies:\n  fin:\n    # 測試\n    tenants: [t]\n".encode("cp950")),
        # the file is fine, its top level is not a mapping
        ("file-is-a-list", b"- domain_policies:\n    fin:\n      tenants: [t]\n"),
        ("file-is-a-scalar", b"domain_policies\n"),
        # the file and its top level are fine, the block is not a mapping
        ("block-is-a-list", b"domain_policies:\n  - fin\n"),
        ("block-is-null", b"domain_policies:\n"),
        ("block-is-a-scalar", b"domain_policies: nope\n"),
    ]

    @staticmethod
    def _tree(d, policy: bytes):
        with open(os.path.join(d, "_domain_policy.yaml"), "wb") as f:
            f.write(policy)
        _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {"cpu": "90"}}})

    @pytest.mark.parametrize("label,body", UNUSABLE,
                             ids=[i for i, _ in UNUSABLE])
    def test_strict_refuses_to_stay_quiet(self, label, body):
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, body)
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            blocking = [w for w in warnings
                        if w.lstrip().startswith(POLICY_ERROR_PREFIX)]
            assert blocking, (
                f"{label}: every domain policy was dropped and --strict said "
                f"nothing: {warnings}")
            assert any("_domain_policy.yaml" in w for w in blocking), blocking

    @pytest.mark.parametrize("label,body", UNUSABLE,
                             ids=[i for i, _ in UNUSABLE])
    def test_the_message_states_this_cause_and_its_own_remedy(self, label,
                                                              body):
        """⛔ The remedy used to be one sentence written by the consumer —
        "fix: repair the YAML syntax in _domain_policy.yaml" — which is right
        for exactly one of these rows and sends every other reader hunting
        for a syntax error in a file whose syntax is fine."""
        with tempfile.TemporaryDirectory() as d:
            self._tree(d, body)
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            msg = " ".join(w for w in warnings
                           if w.lstrip().startswith(POLICY_ERROR_PREFIX))
            assert "— fix:" in msg, msg
            if label == "broken-yaml":
                assert "repair the YAML syntax" in msg, msg
            else:
                assert "repair the YAML syntax" not in msg, (
                    f"{label}: this file parses — telling the reader to "
                    f"repair its syntax sends them looking for nothing: {msg}")
            if label == "not-utf8":
                assert "UTF-8" in msg, msg
            if label.startswith("block-"):
                assert "'domain_policies:'" in msg, msg

    def test_a_usable_policy_file_records_nothing(self):
        """Must-tolerate control: without it, "record everything" passes
        every row above while turning a healthy conf.d red."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d, "_domain_policy.yaml", {
                "domain_policies": {"fin": {"tenants": ["tenant-x"]}}})
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {"cpu": "90"}}})
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            assert not [w for w in warnings
                        if "could not be used" in w], warnings

    def test_the_policies_are_actually_read_when_the_file_is_fine(self):
        """Must-succeed control: proves the fixture shape can produce a
        loaded policy at all, so "nothing recorded" above is not simply
        "nothing happened"."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d, "_defaults.yaml", {"defaults": {"cpu": 80}})
            _wy(d, "_domain_policy.yaml", {"domain_policies": {
                "fin": {"tenants": ["tenant-x"],
                        "constraints": {"forbidden_receiver_types": ["slack"]}}}})
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {
                "_routing": {"receiver": {
                    "type": "slack", "api_url": "https://h.example/a"}}}}})
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            assert any("forbidden" in w for w in warnings), warnings

    def test_a_non_policy_file_with_the_same_shape_is_not_escalated(self):
        """Second must-tolerate side: this bookkeeping exists because a
        policy file silently disables a cross-cutting control. A tenant file
        with a wrong top level is still just a skipped file."""
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "tenant-x.yaml", "- not: a mapping\n")
            _wy(d, "_defaults.yaml", {"defaults": {"cpu": 80}})
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            assert not [w for w in warnings
                        if w.lstrip().startswith(POLICY_ERROR_PREFIX)], warnings

    def test_measured_gap_a_policy_file_that_parses_to_nothing(self):
        """⚠️ Disclosure, not a contract — and deliberately phrased so it
        fails if someone closes the gap without updating this note.

        A policy file that parses to ``None`` (empty, or every entry
        commented out) is dropped by the ``if not data`` guard with no
        record, unchanged from before #1448. An intentionally empty policy
        file is legal today, so closing this is a behaviour change rather
        than a fail-open fix; it is tracked as a follow-up on #1448.
        """
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_domain_policy.yaml", "# all commented out\n")
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {"cpu": "90"}}})
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            assert not [w for w in warnings
                        if w.lstrip().startswith(POLICY_ERROR_PREFIX)], (
                "this gap is now closed — good; update the disclosure in "
                "_drop_unusable_policy() and this test's docstring")


class TestTheSharedReaderNamesWhatItCannotRead:
    """#1448's symptom must not survive on `generate-routes` / `explain-route`.

    ⛔ The first version of this branch listed two exception types in
    ``_parse_config_files`` while the sibling loop it was modelled on had
    just gone open-ended *because listing keeps losing*. Measured: a tenant
    file nested ~500 levels deep made ``yaml.safe_load`` raise
    (roughly half ``sys.getrecursionlimit()``, and ⚠️ **not a constant even
    on one host** — it shifts with how many frames are already on the stack.
    Measured three ways on this machine: flow-style 495/495/493, block-style
    493/492/491. Never assert on the number; assert on the behaviour.)
    ``RecursionError`` (not a ``YAMLError``), which escaped and killed
    ``generate-routes --validate --strict`` with a traceback and **zero
    bytes on stdout** — the original symptom, on the shared path.
    """

    def test_a_third_kind_of_unreadable_file_is_recorded_not_raised(self):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_domain_policy.yaml",
                       "".join("  " * i + "k%d:\n" % i for i in range(700)))
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {"cpu": "90"}}})
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            blocking = [w for w in warnings
                        if w.lstrip().startswith(POLICY_ERROR_PREFIX)]
            assert blocking, (
                "an unreadable policy file escaped the reader instead of "
                f"being recorded: {warnings}")
            assert "_domain_policy.yaml" in " ".join(blocking), blocking

    def test_a_declared_but_empty_tenants_block_says_so(self, capsys):
        """⛔ Not silent. Commenting a tenant out is a legitimate way to
        disable it, so this is not an error and the exit code is unchanged
        — but it is byte-identical to commenting out one threshold and
        taking the block with it, and the sibling branch (a wrong-shaped
        `tenants:`) already warns. Only the null spelling was quiet.
        """
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "tenant-x.yaml", "tenants:\n  # db-a:\n  #   cpu: 9\n")
            _wy(d, "_defaults.yaml", {"defaults": {"cpu": 80}})
            routing, _, _, _, _ = load_tenant_configs(d)
            assert routing == {}
            err = capsys.readouterr().err
            assert "'tenants:' is declared but empty" in err, err
            assert "tenant-x.yaml" in err, err

    def test_a_populated_tenants_block_stays_quiet(self, capsys):
        """Must-stay-quiet control: the warning must not fire on a normal
        file, or it becomes noise everyone learns to ignore."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d, "_defaults.yaml", {"defaults": {"cpu": 80}})
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {"cpu": "90"}}})
            load_tenant_configs(d)
            assert "declared but empty" not in capsys.readouterr().err


class TestTenantsBlockShapeGuard:
    """⛔ The half that was load-bearing was the half nobody tested.

    A mutation sweep deleted ``if not isinstance(tenants, dict)`` from
    ``_parse_config_files`` and the entire suite stayed green — while a
    ``conf.d`` holding ``tenants:`` as a LIST went from a clean warning to
    ``AttributeError: 'list' object has no attribute 'items'``, i.e. the
    exact crash class #1448 exists to remove, on the shared
    ``generate-routes`` / ``explain-route`` path.

    Its sibling (``tenants:`` is null) *was* tested — and the null branch's
    own comment justifies itself by pointing at this one ("the sibling
    branch below already warns for a wrong-shaped `tenants:`"). The comment
    was leaning on an unguarded branch.

    ⚠️ ``test_tenants_not_list_strict_becomes_error`` above is not this: it
    exercises ``domain_policies['pol']['tenants']``, a different key.
    """

    @pytest.mark.parametrize("body", [
        "tenants:\n  - db-a\n  - db-b\n",
        "tenants: not-a-mapping\n",
        "tenants: 7\n",
    ], ids=["list", "scalar-str", "scalar-int"])
    def test_a_wrong_shaped_tenants_block_warns_instead_of_crashing(
            self, body, capsys):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "tenant-x.yaml", body)
            _wy(d, "_defaults.yaml", {"defaults": {"cpu": 80}})
            routing, _, _, _, _ = load_tenant_configs(d)
            assert routing == {}
            err = capsys.readouterr().err
            assert "'tenants' must be a mapping" in err, err
            assert "tenant-x.yaml" in err, err

    def test_a_normal_tenants_block_is_still_loaded(self, capsys):
        """Must-succeed control: the guard must not swallow real tenants."""
        with tempfile.TemporaryDirectory() as d:
            _wy(d, "_defaults.yaml", {"defaults": {"cpu": 80}})
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {
                "_routing": {"receiver": {
                    "type": "webhook",
                    "url": "https://hooks.example.com/a"}}}}})
            routing, _, _, _, _ = load_tenant_configs(d)
            assert "tenant-x" in routing
            assert "must be a mapping" not in capsys.readouterr().err

    def test_a_falsy_non_mapping_document_is_not_waved_through(self, capsys):
        """⛔ `if not data: continue` used to run BEFORE the shape check.

        So `[]`, `0` and `false` skipped out untouched: measured, a
        `_domain_policy.yaml` whose whole body was `[]` left `--strict` at
        exit 0 with zero policies enforced, while `validate-config` called
        the same file a non-mapping and exited 1. Two readers, one file,
        two verdicts.
        """
        for body in ("[]\n", "0\n", "false\n"):
            with tempfile.TemporaryDirectory() as d:
                write_yaml(d, "_domain_policy.yaml", body)
                _wy(d, "tenant-x.yaml",
                    {"tenants": {"tenant-x": {"cpu": "90"}}})
                _, _, warnings, _, _ = load_tenant_configs(
                    d, strict_policies=True)
                blocking = [w for w in warnings
                            if w.lstrip().startswith(POLICY_ERROR_PREFIX)]
                assert blocking, f"body={body!r} slipped through: {warnings}"

    def test_a_genuinely_empty_document_stays_exempt(self, capsys):
        """Must-tolerate side: `None` and `{}` really are "nothing", and an
        intentionally empty policy file is legal today. Without this the
        fix above could be bought by rejecting every empty file."""
        for body in ("# all commented out\n", "{}\n"):
            with tempfile.TemporaryDirectory() as d:
                write_yaml(d, "_domain_policy.yaml", body)
                _wy(d, "tenant-x.yaml",
                    {"tenants": {"tenant-x": {"cpu": "90"}}})
                _, _, warnings, _, _ = load_tenant_configs(
                    d, strict_policies=True)
                assert not [w for w in warnings
                            if w.lstrip().startswith(POLICY_ERROR_PREFIX)], body

    def test_a_multi_line_parser_message_stays_on_one_line(self):
        """⛔ YAML parser messages are multi-line; the record is one line.

        Unflattening survived the sweep. The recorded detail is rendered
        into a `--strict` ERROR line that CI greps and humans skim; a
        newline in the middle of it splits one finding into two fragments.
        """
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_domain_policy.yaml",
                       "domain_policies:\n  fin: [unclosed\n")
            _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {"cpu": "90"}}})
            _, _, warnings, _, _ = load_tenant_configs(d,
                                                       strict_policies=True)
            blocking = [w for w in warnings
                        if w.lstrip().startswith(POLICY_ERROR_PREFIX)]
            assert blocking
            for w in blocking:
                assert "\n" not in w, repr(w)


class TestTheSuccessLineIsTheThingThisPRTalksAbout:
    """`OK: all configs valid` had no assertion anywhere.

    ⛔ A coverage inventory (list the artifact's properties FIRST, only then
    read the guards) found this: deleting the line that prints it leaves the
    whole suite at 211 passed. That is the exact string
    ``_drop_unusable_policy``'s docstring cites as the symptom of the
    fail-open it exists to close — "printed `OK: all configs valid` while
    zero policies were enforced". The narrative leaned on a line nothing
    pinned.

    The lenient-path assertions that already exist check `returncode == 0`
    and `"ERROR" not in stderr`; neither notices whether the tool said
    anything at all, and a silent success is exactly what a fail-open looks
    like from the outside.

    ⚠️ Scope: `generate_alertmanager_routes.py` is not otherwise touched by
    this change. This is one assertion protecting the sentence the change is
    about, not an attempt to cover that module.
    """

    @staticmethod
    def _clean_tree(d):
        _wy(d, "_defaults.yaml", {
            "defaults": {"mysql_threads_running": 80},
            "_routing_defaults": {
                "group_by": ["tenant", "alertname"],
                "group_wait": "30s",
                "repeat_interval": "4h",
                "receiver": {"type": "slack",
                             "api_url": "https://hooks.example/a"}}})
        _wy(d, "tenant-x.yaml", {"tenants": {"tenant-x": {
            "mysql_threads_running": 90}}})

    def _run(self, d, *extra):
        p = subprocess.run(
            [sys.executable, _SCRIPT, "--config-dir", d, "--validate", *extra],
            capture_output=True, timeout=300)
        return (p.returncode,
                p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))

    def test_a_clean_run_says_so_out_loud(self):
        with tempfile.TemporaryDirectory() as d:
            self._clean_tree(d)
            rc, out, err = self._run(d)
            assert rc == 0, out + err
            assert "OK: all configs valid" in out, (
                "a clean run printed no success line — silence and success "
                f"are now indistinguishable from the outside:\n{out!r}")

    def test_a_failing_run_does_not_say_it(self):
        """Must-not-fire control. Without it, hardcoding the success line
        unconditionally would satisfy the assertion above — and that is
        precisely the fail-open this branch exists to close."""
        with tempfile.TemporaryDirectory() as d:
            self._clean_tree(d)
            write_yaml(d, "_domain_policy.yaml",
                       "domain_policies:\n  fin: [unclosed\n")
            rc, out, err = self._run(d, "--strict")
            assert rc == 1, out + err
            assert "OK: all configs valid" not in out, out


class TestTheGoAsymmetryWarningStaysTrue:
    """⛔ A cross-language claim printed at customers, pinned on both sides.

    The warning for a wrong-shaped ``tenants:`` tells the reader that the Go
    exporter drops the **whole file** on the same bytes — which is why this
    reader only warning is safe to ship. That sentence is an assertion about
    a file in another language that nothing here compiles, so it is exactly
    the kind that rots silently: `flat_scanner.go` changes, the Python
    warning keeps telling customers something that stopped being true.

    Two halves, both cheap:
      * the warning still says it (so a reword cannot drop it), and
      * the Go declaration it rests on still says what it says.
    """


    def test_the_warning_carries_the_go_consequence(self, capsys):
        with tempfile.TemporaryDirectory() as d:
            write_yaml(d, "_defaults.yaml",
                       "defaults:\n  cpu: 80\ntenants:\n  - alpha\n")
            load_tenant_configs(d)
            err = capsys.readouterr().err
            assert "'tenants' must be a mapping" in err, err
            assert "Go exporter drops the WHOLE file" in err, (
                "the warning stopped telling the reader why a mere warning "
                f"here is not the whole story:\n{err}")

    # ⛔ There is deliberately NO test here asserting on the Go source.
    #
    # A previous revision grepped `pkg/config/types.go` and
    # `flat_scanner.go` to keep the sentence above honest. Blind review
    # produced three legitimate Go edits that turned it red, each with a
    # failure message stating the opposite of the truth:
    #   * renaming the local `partial` to `cfg` → "no longer drops the file"
    #     (it still does);
    #   * `Tenants TenantThresholds` with `type TenantThresholds = map[...]`
    #     → "the field is no longer a map" (an alias; it is);
    #   * adding an unrelated `TenantsDigest` field above it → the `startswith`
    #     scan matched the wrong line.
    # The cheapest way back to green in the first case was to undo a correct
    # Go refactor for the sake of a Python test. Pinning another language's
    # source with a grep has no safe shape at this granularity, and a guard
    # whose message contradicts reality is worse than no guard.
    #
    # What remains is the disclosure. The warning's claim about Go was
    # verified by reading three places, and it is worth naming which one
    # actually carries the "including its `defaults:`" half:
    #   * `pkg/config/types.go` — the `tenants` field decodes into a map, so
    #     a YAML sequence there is a decode error;
    #   * `flat_scanner.go` — `parsePartialConfig` returns `ok=false` on it.
    #     ⚠️ This alone does NOT establish the consequence: yaml.v3 fills in
    #     the fields it could decode before returning a `*yaml.TypeError`, so
    #     the returned `partial` still carries `defaults:`;
    #   * `config.go` — the callers are what drop it, at `:446-449`
    #     (`delete(newConfigs, name); continue`) and `:629-631` (`continue`,
    #     so the file never reaches `fileConfigs`). Change either caller to
    #     keep `partial` and the customer-facing sentence goes false.
    # Nothing here will tell you when that happens. A real pin belongs in a
    # Go test, next to the code it is about.


class TestPlatformPackLocationIsLayoutIndependent:
    """#1494 — pack 的定位不得靠「數上去幾層」，且退化必須出聲。

    這一組釘的是本 PR 的承重宣稱：客戶在**映像**裡拿到的是完整探針集，
    不是降級版。原本的 `parents[3]` 在映像佈局（3 個祖先）是 IndexError，
    而修法之後真正該問的是「它到底找不找得到、找不到會不會講」。
    """

    @staticmethod
    def _load_isolated(tmpdir):
        """把 _grar_validate 從 *tmpdir* 載入成獨立模組實例。

        ⛔ 不能重用 already-imported 的那份：模組層的 `__file__` 決定解析
        起點，而快取的那份指向 repo 樹。每次給一個新名字，也讓
        `_PLATFORM_IDENTITY_CACHE` / warn-once 旗標不互相污染。
        """
        import importlib.util
        import uuid
        name = "grar_probe_" + uuid.uuid4().hex[:8]
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(tmpdir, "_grar_validate.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    @staticmethod
    def _stage(tmpdir, with_pack: bool):
        """複製 module + 其 import 相依到 *tmpdir*（模擬映像的扁平佈局）。"""
        import shutil
        for fname in ("_grar_validate.py", "_lib_python.py"):
            src = (os.path.join(_OPS_DIR, fname) if fname.startswith("_grar")
                   else os.path.join(REPO_ROOT, "scripts", "tools", fname))
            shutil.copy2(src, os.path.join(tmpdir, fname))
        if with_pack:
            shutil.copy2(
                os.path.join(REPO_ROOT, "k8s", "03-monitoring",
                             "configmap-rules-platform.yaml"),
                os.path.join(tmpdir, "configmap-rules-platform.yaml"))
        sys.path.insert(0, tmpdir)

    def test_flat_layout_with_the_pack_beside_it_gets_the_full_set(
        self, tmp_path, capsys
    ):
        """出貨組態：pack 與模組同目錄 ⇒ 完整探針集、零警告。"""
        d = str(tmp_path)
        self._stage(d, with_pack=True)
        try:
            mod = self._load_isolated(d)
            found = mod._find_platform_rules_configmap()
            assert found is not None
            assert found.parent == tmp_path, (
                f"應該解析到同目錄那份，實際 {found}")
            full = mod.platform_alert_identities()
            assert len(full) > len(mod.PLATFORM_ALERT_IDENTITY_LABELS), (
                "同目錄有 pack 卻仍拿到 fallback 常數 —— 出貨的檢查是降級版")
            assert "WARN" not in capsys.readouterr().err
        finally:
            sys.path.remove(d)

    def test_without_the_pack_it_degrades_and_says_so(self, tmp_path, capsys):
        """對照組：拿掉 pack ⇒ 退回常數，而且**出聲**。

        ⛔ 沒有這一格，上面那格證明不了「41 是因為 pack 在那裡」——
        它可能來自任何別的來源。
        """
        d = str(tmp_path)
        self._stage(d, with_pack=False)
        try:
            mod = self._load_isolated(d)
            assert mod._find_platform_rules_configmap() is None
            degraded = mod.platform_alert_identities()
            assert degraded == mod.PLATFORM_ALERT_IDENTITY_LABELS
            err = capsys.readouterr().err
            assert "WARN" in err and "degraded" in err, err
            assert "configmap-rules-platform.yaml" in err, err
        finally:
            sys.path.remove(d)

    def test_the_warning_is_emitted_once_per_process(self, tmp_path, capsys):
        """降級警告不得每次呼叫都刷一次（會淹掉客戶的真實輸出）。"""
        d = str(tmp_path)
        self._stage(d, with_pack=False)
        try:
            mod = self._load_isolated(d)
            mod.platform_alert_identities()
            capsys.readouterr()
            mod._PLATFORM_IDENTITY_CACHE = None  # 強迫再走一次解析
            mod.platform_alert_identities()
            assert "WARN" not in capsys.readouterr().err
        finally:
            sys.path.remove(d)

    def test_repo_layout_still_resolves_the_real_pack(self):
        """repo 佈局（開發者與 CI 走的那條）行為未變。"""
        import _grar_validate as g
        found = g._find_platform_rules_configmap()
        assert found is not None and found.is_file()
        assert found.parts[-3:] == (
            "k8s", "03-monitoring", "configmap-rules-platform.yaml")
