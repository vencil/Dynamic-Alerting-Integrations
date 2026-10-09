"""Tests for policy_opa_bridge.py — OPA tenant policy evaluation bridge.

Audit flagged 0% coverage. This is the OPA REST/binary bridge:
converts tenant YAML configs to OPA input JSON, calls OPA (via REST
or binary), converts violations back to PolicyResult/Violation. Tests
stub urlopen / subprocess / load_yaml_file so no real OPA is invoked.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.error import URLError

import pytest

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import policy_opa_bridge as pob  # noqa: E402
from _lib_exitcodes import EXIT_CALLER_ERROR  # noqa: E402


# ---------------------------------------------------------------------------
# Violation + PolicyResult dataclasses
# ---------------------------------------------------------------------------
class TestPolicyResult:
    def test_empty_passes(self):
        r = pob.PolicyResult()
        assert r.error_count == 0
        assert r.warning_count == 0
        assert r.passed is True

    def test_warning_only_still_passes(self):
        r = pob.PolicyResult(violations=[
            pob.Violation("db-a", "WARNING", "soft issue", "x"),
        ])
        assert r.warning_count == 1
        assert r.error_count == 0
        assert r.passed is True

    def test_error_fails(self):
        r = pob.PolicyResult(violations=[
            pob.Violation("db-a", "ERROR", "broken", "x"),
            pob.Violation("db-b", "WARNING", "soft", "y"),
        ])
        assert r.error_count == 1
        assert r.warning_count == 1
        assert r.passed is False


# ---------------------------------------------------------------------------
# load_tenant_inputs (#2115 0-B)
# ---------------------------------------------------------------------------
# 取代原本的 TestLoadTenantConfigs：它守的是舊契約（`_lib_io.load_tenant_configs`：
# 只讀根目錄租戶檔、平面檔以檔名當租戶、不存在的目錄回 {}）。現在 `tenants` 來自
# `da-guard effective`、`served` 來自 `da-guard served-values`；平面檔不是租戶（WARN），
# 子目錄的租戶在，值寫在哪一層都一樣（tests/shared/test_served_values_policy_readers_matrix.py）。
def _write_tree(root, files):
    for rel, body in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body, encoding="utf-8")
    return root


@pytest.mark.usefixtures("da_guard_env")
class TestLoadTenantInputs:
    def test_tenants_are_written_plus_inherited_and_served_is_the_metrics_numbers(self, tmp_path):
        d = _write_tree(tmp_path, {
            "_defaults.yaml": "defaults:\n  mysql_connections: 80\n  mysql_threads_running: 50\n",
            "tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_cpu: '500'\n    _silent_mode: disable\n",
            "team/tenant-b.yaml": "tenants:\n  tenant-b: {}\n",
        })
        tenants, served = pob.load_tenant_inputs(str(d))
        # tenants: 原形狀（寫法，舊拼法照寫），加上繼承；Go 自動補的不在。
        # #2720 之後 effective 是逐閾值 view：同一閾值跨層新舊拼法只留勝出那一層，
        # 所以租戶的 `mysql_cpu: '500'` 蓋掉根的 `mysql_threads_running: 50`，不再並列。
        assert tenants["tenant-a"] == {"mysql_cpu": "500", "_silent_mode": "disable",
                                       "mysql_connections": 80}
        assert tenants["tenant-b"] == {"mysql_connections": 80, "mysql_threads_running": 50}
        # served: /metrics 的數字，現行拼法
        assert served["tenant-a"] == {"mysql_connections": 80.0, "mysql_threads_running": 500.0}
        assert served["tenant-b"] == {"mysql_connections": 80.0, "mysql_threads_running": 50.0}

    def test_flat_file_is_not_a_tenant(self, tmp_path, capsys):
        d = _write_tree(tmp_path, {
            "_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
            "flat-a.yaml": "mysql_connections: '70'\n",
        })
        assert pob.load_tenant_inputs(str(d)) == ({}, {})
        assert "WARN: flat-a.yaml: declares no tenant" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# load_defaults
# ---------------------------------------------------------------------------
class TestLoadDefaults:
    def test_missing_file_returns_empty(self, tmp_path):
        assert pob.load_defaults(str(tmp_path)) == {}

    def test_loads_dict(self, tmp_path, monkeypatch):
        f = tmp_path / "_defaults.yaml"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(pob, "load_yaml_file_strict",
                            lambda p: {"mysql_connections": 80})
        assert pob.load_defaults(str(tmp_path)) == {"mysql_connections": 80}

    def test_reads_the_selected_carrier_not_a_hardcoded_name(self, tmp_path):
        """#1674: a root holding only `_defaults.yml` gave OPA no defaults,
        because the path was joined as `_defaults.yaml`; and with a pair the
        exporter reads the `.yaml`."""
        (tmp_path / "_defaults.yml").write_text(
            "defaults:\n  cpu_pct: 70\n", encoding="utf-8")
        assert pob.load_defaults(str(tmp_path)) == {"defaults": {"cpu_pct": 70}}
        (tmp_path / "_defaults.yaml").write_text(
            "defaults:\n  cpu_pct: 50\n", encoding="utf-8")
        assert pob.load_defaults(str(tmp_path)) == {"defaults": {"cpu_pct": 50}}

    def test_non_dict_returns_empty(self, tmp_path, monkeypatch):
        f = tmp_path / "_defaults.yaml"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(pob, "load_yaml_file_strict", lambda p: ["list"])
        assert pob.load_defaults(str(tmp_path)) == {}


# ---------------------------------------------------------------------------
# build_opa_input
# ---------------------------------------------------------------------------
class TestBuildOpaInput:
    def test_basic_shape(self):
        result = pob.build_opa_input(
            config_dir="/tmp",
            tenant_configs={"db-a": {"x": 1}},
            defaults={"mysql": 80, "_meta": "ignored"},
            rule_packs=["mariadb"],
            platform_version="v2.8.0",
        )
        assert result["tenants"] == {"db-a": {"x": 1}}
        assert result["defaults"] == {"mysql": 80}  # _meta excluded
        assert result["rule_packs"] == ["mariadb"]
        assert result["platform_version"] == "v2.8.0"

    def test_rule_packs_default_to_empty_list(self):
        result = pob.build_opa_input("/tmp", {}, {})
        assert result["rule_packs"] == []

    def test_underscore_keys_filtered_from_defaults(self):
        # _meta, _routing etc are NOT thresholds.
        result = pob.build_opa_input("/tmp", {}, {
            "x": 1, "_internal": "y", "_routing": {},
        })
        assert result["defaults"] == {"x": 1}


# ---------------------------------------------------------------------------
# call_opa_rest
# ---------------------------------------------------------------------------
class TestCallOpaRest:
    def _stub_urlopen(self, monkeypatch, body: bytes):
        class FakeResp:
            def read(self):
                return body
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
        monkeypatch.setattr(pob, "urlopen", lambda *a, **kw: FakeResp())

    def test_success_returns_violations(self, monkeypatch):
        body = json.dumps({"result": [
            {"msg": "bad", "severity": "error", "tenant": "db-a", "field": "x"},
        ]}).encode("utf-8")
        self._stub_urlopen(monkeypatch, body)
        out = pob.call_opa_rest(
            "http://localhost:8181", "dynamic_alerting.policy", {},
        )
        assert len(out) == 1
        assert out[0]["msg"] == "bad"

    # #2724: these used to return [] — "no violations" — so main reported
    # PASS rc 0. Each is now OpaEvalError (main: rc 2).
    def test_url_error_raises(self, monkeypatch):
        def boom(*a, **kw):
            raise URLError("connection refused")
        monkeypatch.setattr(pob, "urlopen", boom)
        with pytest.raises(pob.OpaEvalError, match="OPA API call failed"):
            pob.call_opa_rest("http://x", "p", {})

    def test_timeout_raises(self, monkeypatch):
        def boom(*a, **kw):
            raise TimeoutError("timed out")
        monkeypatch.setattr(pob, "urlopen", boom)
        with pytest.raises(pob.OpaEvalError, match="timed out"):
            pob.call_opa_rest("http://x", "p", {})

    def test_bad_url_raises(self):
        with pytest.raises(pob.OpaEvalError, match="OPA API call failed"):
            pob.call_opa_rest("not-a-url", "p", {})

    def test_invalid_json_raises(self, monkeypatch):
        self._stub_urlopen(monkeypatch, b"{not json")
        with pytest.raises(pob.OpaEvalError, match="is not JSON"):
            pob.call_opa_rest("http://x", "p", {})

    def test_result_not_a_list_raises(self, monkeypatch):
        body = json.dumps({"result": "string-not-list"}).encode("utf-8")
        self._stub_urlopen(monkeypatch, body)
        with pytest.raises(pob.OpaEvalError, match="not a set or array"):
            pob.call_opa_rest("http://x", "p", {})

    def test_undefined_raises(self, monkeypatch):
        """OPA answers `{}` for a path with no value: nothing was evaluated."""
        self._stub_urlopen(monkeypatch, b"{}")
        with pytest.raises(pob.OpaEvalError, match="undefined"):
            pob.call_opa_rest("http://x", "p", {})

    def test_defined_empty_set_is_no_violation(self, monkeypatch):
        self._stub_urlopen(monkeypatch, b'{"result": []}')
        assert pob.call_opa_rest("http://x", "p", {}) == []

    @pytest.mark.parametrize("package, path", [
        ("dynamic_alerting.policy", "/v1/data/dynamic_alerting/policy/violations"),
        ("dynamic_alerting/policy", "/v1/data/dynamic_alerting/policy/violations"),
        ("a.b.c", "/v1/data/a/b/c/violations"),
    ])
    def test_package_dots_become_slashes(self, monkeypatch, package, path):
        """#2724: OPA's data API takes the package with `/`; the default
        `dynamic_alerting.policy` sent as written is one key, undefined."""
        captured = {}

        class FakeResp:
            def read(self):
                return b'{"result": []}'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            return FakeResp()
        monkeypatch.setattr(pob, "urlopen", fake_urlopen)
        pob.call_opa_rest("http://localhost:8181", package, {})
        assert captured["url"] == "http://localhost:8181" + path

    def test_strips_trailing_slash_from_url(self, monkeypatch):
        captured = {}

        class FakeResp:
            def read(self):
                return b'{"result": []}'
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout):
            captured["url"] = req.full_url
            return FakeResp()
        monkeypatch.setattr(pob, "urlopen", fake_urlopen)
        pob.call_opa_rest("http://localhost:8181/", "p", {})
        # No double-slash.
        assert "//v1/" not in captured["url"]
        assert captured["url"].endswith("/v1/data/p/violations")


# ---------------------------------------------------------------------------
# call_opa_binary
# ---------------------------------------------------------------------------
class TestCallOpaBinary:
    def _stub_run(self, monkeypatch, returncode=0, stdout="", stderr=""):
        proc = subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=stdout, stderr=stderr,
        )
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return proc
        monkeypatch.setattr(pob.subprocess, "run", fake_run)
        return calls

    @staticmethod
    def _eval_doc(value):
        """`opa eval --format json`'s shape for a defined query."""
        return json.dumps({"result": [{"expressions": [
            {"value": value, "text": "data.pkg.violations",
             "location": {"row": 1, "col": 1}}]}]})

    def test_success_returns_violations(self, monkeypatch):
        self._stub_run(monkeypatch, 0, self._eval_doc(
            [{"msg": "x", "severity": "error", "tenant": "t", "field": "f"}]))
        out = pob.call_opa_binary("opa", "/p.rego", "pkg", {})
        assert out == [{"msg": "x", "severity": "error", "tenant": "t", "field": "f"}]

    def test_command_passes_input_on_stdin(self, monkeypatch):
        """#2724: `-I` takes no value; the old command put the input JSON after
        it, so OPA saw a second query and refused every run."""
        calls = self._stub_run(monkeypatch, 0, self._eval_doc([]))
        pob.call_opa_binary("opa", "/p.rego", "dynamic_alerting.policy", {"tenants": {}})
        (cmd, kw), = calls
        assert cmd == ["opa", "eval", "--format", "json", "-d", "/p.rego",
                       "--stdin-input", "data.dynamic_alerting.policy.violations"]
        assert json.loads(kw["input"]) == {"tenants": {}}

    @pytest.mark.parametrize("package, query", [
        ("dynamic_alerting.policy", "data.dynamic_alerting.policy.violations"),
        ("dynamic_alerting/policy", "data.dynamic_alerting.policy.violations"),
    ])
    def test_package_query(self, package, query):
        assert pob.package_query(package) == query

    # #2724: these used to return [] — PASS rc 0. Each is now OpaEvalError.
    def test_nonzero_returncode_raises(self, monkeypatch):
        self._stub_run(monkeypatch, 1, "", "policy parse error")
        with pytest.raises(pob.OpaEvalError, match="OPA eval failed.*policy parse error"):
            pob.call_opa_binary("opa", "/p.rego", "pkg", {})

    def test_eval_errors_on_stdout_are_named(self, monkeypatch):
        self._stub_run(monkeypatch, 2, json.dumps({"errors": [{
            "message": "unexpected eof token", "code": "rego_parse_error",
            "location": {"file": "/p.rego", "row": 3}}]}), "")
        with pytest.raises(pob.OpaEvalError,
                           match=r"rego_parse_error: unexpected eof token \(/p.rego:3\)"):
            pob.call_opa_binary("opa", "/p.rego", "pkg", {})

    def test_binary_not_found_raises(self, monkeypatch):
        def boom(*a, **kw):
            raise FileNotFoundError("opa not found")
        monkeypatch.setattr(pob.subprocess, "run", boom)
        with pytest.raises(pob.OpaEvalError, match="OPA binary not found"):
            pob.call_opa_binary("opa", "/p.rego", "pkg", {})

    def test_timeout_raises(self, monkeypatch):
        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="opa", timeout=10)
        monkeypatch.setattr(pob.subprocess, "run", boom)
        with pytest.raises(pob.OpaEvalError, match="timed out"):
            pob.call_opa_binary("opa", "/p.rego", "pkg", {})

    def test_invalid_json_output_raises(self, monkeypatch):
        self._stub_run(monkeypatch, 0, "{not json", "")
        with pytest.raises(pob.OpaEvalError, match="is not JSON"):
            pob.call_opa_binary("opa", "/p.rego", "pkg", {})

    def test_undefined_raises(self, monkeypatch):
        """`opa eval` prints `{}` rc 0 for an undefined query."""
        self._stub_run(monkeypatch, 0, "{}\n", "")
        with pytest.raises(pob.OpaEvalError, match="undefined"):
            pob.call_opa_binary("opa", "/p.rego", "pkg", {})

    def test_result_without_expressions_raises(self, monkeypatch):
        self._stub_run(monkeypatch, 0, json.dumps({"result": "scalar"}), "")
        with pytest.raises(pob.OpaEvalError, match="expressions"):
            pob.call_opa_binary("opa", "/p.rego", "pkg", {})

    def test_value_not_a_list_raises(self, monkeypatch):
        self._stub_run(monkeypatch, 0, self._eval_doc({"a": 1}), "")
        with pytest.raises(pob.OpaEvalError, match="not a set or array"):
            pob.call_opa_binary("opa", "/p.rego", "pkg", {})


# ---------------------------------------------------------------------------
# convert_opa_violations
# ---------------------------------------------------------------------------
class TestConvertOpaViolations:
    def test_empty_returns_empty_result(self):
        result = pob.convert_opa_violations([], 5)
        assert result.tenants_evaluated == 5
        assert result.violations == []

    def test_normal_violation(self):
        result = pob.convert_opa_violations([{
            "msg": "bad", "severity": "error",
            "tenant": "db-a", "field": "x",
        }], 1)
        assert len(result.violations) == 1
        v = result.violations[0]
        assert v.tenant == "db-a"
        assert v.level == "ERROR"
        assert v.message == "bad"
        assert v.field == "x"

    def test_warning_severity_normalised(self):
        result = pob.convert_opa_violations([{
            "msg": "soft", "severity": "warning",
            "tenant": "tenant-a", "field": "x",
        }], 1)
        assert result.violations[0].level == "WARNING"

    def test_unknown_severity_falls_back_to_error(self):
        result = pob.convert_opa_violations([{
            "msg": "weird", "severity": "info",
            "tenant": "x", "field": "y",
        }], 1)
        assert result.violations[0].level == "ERROR"

    def test_missing_severity_defaults_error(self):
        result = pob.convert_opa_violations([{
            "msg": "no-sev", "tenant": "x", "field": "y",
        }], 1)
        assert result.violations[0].level == "ERROR"

    def test_missing_fields_get_defaults(self):
        result = pob.convert_opa_violations([{}], 1)
        v = result.violations[0]
        assert v.tenant == "unknown"
        assert v.message == "Policy violation"
        assert v.field == ""

    def test_non_dict_entries_skipped(self):
        result = pob.convert_opa_violations(
            ["string-not-dict", None, {"msg": "ok", "tenant": "t"}], 1,
        )
        # Only the dict survives.
        assert len(result.violations) == 1
        assert result.violations[0].message == "ok"


# ---------------------------------------------------------------------------
# generate_text_report
# ---------------------------------------------------------------------------
class TestGenerateTextReport:
    def test_clean_report_en(self):
        out = pob.generate_text_report(pob.PolicyResult(tenants_evaluated=3), "en")
        assert "OPA Policy Evaluation Report" in out
        assert "Tenants: 3" in out
        assert "All policies passed" in out

    def test_clean_report_zh(self):
        out = pob.generate_text_report(pob.PolicyResult(tenants_evaluated=3), "zh")
        assert "OPA 策略評估報告" in out
        assert "租戶數: 3" in out
        assert "所有策略均通過" in out

    def test_with_violations_groups_by_tenant(self):
        result = pob.PolicyResult(
            tenants_evaluated=2,
            violations=[
                pob.Violation("db-b", "ERROR", "second tenant first violation", "f1"),
                pob.Violation("db-a", "ERROR", "first tenant", "f2"),
                pob.Violation("db-a", "WARNING", "first tenant warn", "f3"),
            ],
        )
        out = pob.generate_text_report(result, "en")
        # Tenants sorted alphabetically.
        assert out.index("[db-a]") < out.index("[db-b]")
        # Both icons present.
        assert "✗" in out
        assert "⚠" in out
        assert "FAIL" in out

    def test_passed_status_line_when_only_warnings(self):
        result = pob.PolicyResult(
            tenants_evaluated=1,
            violations=[pob.Violation("db-a", "WARNING", "soft", "x")],
        )
        out = pob.generate_text_report(result, "en")
        assert "PASS" in out


# ---------------------------------------------------------------------------
# generate_json_report
# ---------------------------------------------------------------------------
class TestGenerateJsonReport:
    def test_shape(self):
        result = pob.PolicyResult(
            tenants_evaluated=2,
            violations=[
                pob.Violation("db-a", "ERROR", "msg", "field"),
            ],
        )
        report = pob.generate_json_report(result)
        assert report["tenants_evaluated"] == 2
        assert report["error_count"] == 1
        assert report["warning_count"] == 0
        assert report["passed"] is False
        assert len(report["violations"]) == 1
        assert report["violations"][0]["tenant"] == "db-a"


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------
class TestBuildParser:
    def test_en_parser_required_config_dir(self):
        parser = pob.build_parser("en")
        with pytest.raises(SystemExit):
            parser.parse_args([])  # --config-dir missing
        # Valid call.
        args = parser.parse_args(["--config-dir", "/tmp"])
        assert args.config_dir == "/tmp"

    def test_zh_parser_required_config_dir(self):
        parser = pob.build_parser("zh")
        with pytest.raises(SystemExit):
            parser.parse_args([])

    def test_default_values(self):
        parser = pob.build_parser("en")
        args = parser.parse_args(["--config-dir", "/tmp"])
        assert args.opa_binary == "opa"
        assert args.policy_package == "dynamic_alerting.policy"
        assert args.dry_run is False
        assert args.json_output is False
        assert args.ci is False


# ---------------------------------------------------------------------------
# main — CLI orchestrator
# ---------------------------------------------------------------------------
def _no_tenant_tree(root):
    """A tree the exporter loads with no tenant in it (#2115 0-B: a directory
    with no config file at all is not a tree — da-guard exits 2 on it, see
    `test_dir_without_config_files_is_caller_error`)."""
    (root / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n", encoding="utf-8")
    return root


@pytest.mark.usefixtures("da_guard_env")
class TestMain:
    def test_dir_without_config_files_is_caller_error(self, monkeypatch, tmp_path, capsys):
        """#2115 0-B 行為變更：沒有任何設定檔的目錄（原本 rc 0「No tenant configs」）
        現在照 da-guard 的判定 rc 2、一行 ERROR、沒有 traceback。"""
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        with pytest.raises(SystemExit) as exc:
            pob.main(["--config-dir", str(tmp_path), "--dry-run"])
        assert exc.value.code == EXIT_CALLER_ERROR
        captured = capsys.readouterr()
        assert captured.err.startswith("ERROR: da-guard served-values exited 2"), captured.err
        assert captured.out == ""

    def test_no_tenant_configs_returns_zero(self, monkeypatch, tmp_path, capsys):
        # Empty config-dir → no tenant configs → return 0 with informational msg.
        # #1112: the message is prose → stderr; stdout stays clean for the JSON
        # document (see the two envelope tests below).
        _no_tenant_tree(tmp_path)
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        rc = pob.main(["--config-dir", str(tmp_path)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "No tenant configs found" in captured.err
        assert captured.out == ""

    def test_no_tenant_configs_json_envelope(self, monkeypatch, tmp_path, capsys):
        """#1112: --json + 空 config-dir → 一份歸零的 report（可被同一 consumer 消費）。"""
        _no_tenant_tree(tmp_path)
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        rc = pob.main(["--config-dir", str(tmp_path), "--json"])
        assert rc == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["status"] == "no_tenant_configs"
        assert doc["tenants_evaluated"] == 0
        assert doc["violations"] == []

    def test_no_tenant_configs_dry_run_emits_opa_input(self, monkeypatch, tmp_path,
                                                       capsys):
        """#1112: --dry-run 的 stdout 契約是「OPA input 文件」，零租戶就是空 tenants。

        故此路徑吐的是 opa_input（與有租戶時同 schema），不是 report envelope —
        dry-run 的輸出是要餵給 `opa eval` 的，不是給人讀的報告。
        """
        _no_tenant_tree(tmp_path)
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        rc = pob.main(["--config-dir", str(tmp_path), "--dry-run", "--json"])
        assert rc == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["tenants"] == {}
        assert doc["served"] == {}
        assert "platform_version" in doc

    def test_dry_run_prints_input_json(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        # Stub load_tenant_configs to return one tenant.
        monkeypatch.setattr(pob, "load_tenant_inputs",
                            lambda d: ({"tenant-a": {"x": 1}}, {}))
        monkeypatch.setattr(pob, "load_defaults", lambda d: {"y": 2})
        rc = pob.main(["--config-dir", str(tmp_path), "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "tenants" in payload
        assert payload["tenants"]["tenant-a"] == {"x": 1}
        assert payload["served"] == {}

    def test_no_url_no_path_returns_caller_error(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        monkeypatch.setattr(pob, "load_tenant_inputs",
                            lambda d: ({"tenant-a": {"x": 1}}, {}))
        monkeypatch.setattr(pob, "load_defaults", lambda d: {})
        rc = pob.main(["--config-dir", str(tmp_path)])
        assert rc == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "Must specify" in err

    def test_opa_url_path_evaluates_via_rest(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        monkeypatch.setattr(pob, "load_tenant_inputs",
                            lambda d: ({"tenant-a": {"x": 1}}, {}))
        monkeypatch.setattr(pob, "load_defaults", lambda d: {})

        called = {}

        def fake_rest(url, package, input_data):
            called["url"] = url
            called["package"] = package
            return []

        monkeypatch.setattr(pob, "call_opa_rest", fake_rest)
        # Should NOT call binary path.
        monkeypatch.setattr(
            pob, "call_opa_binary",
            lambda *a, **kw: pytest.fail("binary should not be called"),
        )
        rc = pob.main([
            "--config-dir", str(tmp_path),
            "--opa-url", "http://localhost:8181",
        ])
        assert rc == 0
        assert called["url"] == "http://localhost:8181"

    def test_policy_path_evaluates_via_binary(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        monkeypatch.setattr(pob, "load_tenant_inputs",
                            lambda d: ({"tenant-a": {"x": 1}}, {}))
        monkeypatch.setattr(pob, "load_defaults", lambda d: {})

        called = {}

        def fake_binary(binary, policy_path, package, input_data):
            called["policy_path"] = policy_path
            return []

        monkeypatch.setattr(pob, "call_opa_binary", fake_binary)
        monkeypatch.setattr(
            pob, "call_opa_rest",
            lambda *a, **kw: pytest.fail("rest should not be called"),
        )
        rc = pob.main([
            "--config-dir", str(tmp_path),
            "--policy-path", "/path/to/policy.rego",
        ])
        assert rc == 0
        assert called["policy_path"] == "/path/to/policy.rego"

    def test_ci_with_errors_returns_one(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        monkeypatch.setattr(pob, "load_tenant_inputs",
                            lambda d: ({"tenant-a": {"x": 1}}, {}))
        monkeypatch.setattr(pob, "load_defaults", lambda d: {})
        monkeypatch.setattr(pob, "call_opa_rest", lambda *a, **kw: [{
            "msg": "bad", "severity": "error",
            "tenant": "tenant-a", "field": "x",
        }])
        rc = pob.main([
            "--config-dir", str(tmp_path),
            "--opa-url", "http://x",
            "--ci",
        ])
        assert rc == 1

    def test_ci_with_only_warnings_returns_zero(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        monkeypatch.setattr(pob, "load_tenant_inputs",
                            lambda d: ({"tenant-a": {"x": 1}}, {}))
        monkeypatch.setattr(pob, "load_defaults", lambda d: {})
        monkeypatch.setattr(pob, "call_opa_rest", lambda *a, **kw: [{
            "msg": "soft", "severity": "warning",
            "tenant": "tenant-a", "field": "x",
        }])
        rc = pob.main([
            "--config-dir", str(tmp_path),
            "--opa-url", "http://x",
            "--ci",
        ])
        assert rc == 0  # warnings don't fail in CI mode

    def test_json_output_emits_json(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        monkeypatch.setattr(pob, "load_tenant_inputs",
                            lambda d: ({"tenant-a": {"x": 1}}, {}))
        monkeypatch.setattr(pob, "load_defaults", lambda d: {})
        monkeypatch.setattr(pob, "call_opa_rest", lambda *a, **kw: [])
        rc = pob.main([
            "--config-dir", str(tmp_path),
            "--opa-url", "http://x",
            "--json",
        ])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["tenants_evaluated"] == 1
        assert payload["passed"] is True

    def test_zh_no_tenants_message(self, monkeypatch, tmp_path, capsys):
        # #1112: prose → stderr (see TestMain::test_no_tenant_configs_returns_zero).
        _no_tenant_tree(tmp_path)
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "zh")
        rc = pob.main(["--config-dir", str(tmp_path)])
        assert rc == 0
        assert "未找到 tenant 配置" in capsys.readouterr().err

    def test_zh_no_url_no_path_error_message(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "zh")
        monkeypatch.setattr(pob, "load_tenant_inputs",
                            lambda d: ({"tenant-a": {"x": 1}}, {}))
        monkeypatch.setattr(pob, "load_defaults", lambda d: {})
        rc = pob.main(["--config-dir", str(tmp_path)])
        assert rc == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "必須指定" in err


# ---------------------------------------------------------------------------
# #2724: OPA that did not evaluate is exit 2, not "All policies passed."
# ---------------------------------------------------------------------------
# Before #2724 every one of these shapes printed `✓ All policies passed.` and
# exited 0, `--ci` included. These drive `main` end to end (real da-guard, a
# real tree); only OPA's side is a fake, so no `opa` binary is needed.
_TENANT_VIOLATES = {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
                    "team/tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_connections: "95"\n'}
_TENANT_CLEAN = {"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
                 "team/tenant-a.yaml": 'tenants:\n  tenant-a:\n    mysql_connections: "70"\n'}


def _fake_opa(tmp_path, body: str):
    """An executable `opa` stand-in (POSIX shell)."""
    if os.name == "nt":
        pytest.skip("fake `opa` is a POSIX shell script")
    f = tmp_path / "fake-opa"
    f.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    f.chmod(0o755)
    return str(f)


def _closed_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Server:
    """A local HTTP server answering every POST with a fixed status + body."""

    def __init__(self, status: int, body: bytes):
        import http.server
        import threading

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.mark.usefixtures("da_guard_env")
class TestOpaNotEvaluatedIsCallerError:
    @pytest.fixture
    def tree(self, tmp_path):
        return str(_write_tree(tmp_path / "conf.d", _TENANT_VIOLATES))

    def _run(self, monkeypatch, capsys, argv):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        rc = pob.main(argv)
        cap = capsys.readouterr()
        return rc, cap.out, cap.err

    def _assert_caller_error(self, rc, out, err, needle):
        assert rc == EXIT_CALLER_ERROR, (rc, out, err)
        assert "All policies passed" not in out
        assert needle in err, err
        assert "Traceback" not in err

    def test_unreachable_opa_url(self, monkeypatch, capsys, tree):
        url = f"http://127.0.0.1:{_closed_port()}"
        rc, out, err = self._run(monkeypatch, capsys,
                                 ["--config-dir", tree, "--opa-url", url, "--ci"])
        self._assert_caller_error(rc, out, err, "OPA API call failed")

    def test_unreachable_opa_url_json_leaves_stdout_empty(self, monkeypatch, capsys, tree):
        """This tool's caller-error convention: stderr only, no envelope
        (`test_yaml_file_error::test_decorated_tools_leave_stdout_empty_under_json`)."""
        url = f"http://127.0.0.1:{_closed_port()}"
        rc, out, err = self._run(monkeypatch, capsys,
                                 ["--config-dir", tree, "--opa-url", url, "--json"])
        self._assert_caller_error(rc, out, err, "OPA API call failed")
        assert out == ""

    @pytest.mark.parametrize("status, body, needle", [
        (200, b"<html>not json", "is not JSON"),
        (200, b"{}", "undefined"),
        (200, b'{"result": {"a": 1}}', "not a set or array"),
        (500, b'{"code": "internal_error"}', "HTTP 500"),
    ], ids=["garbage", "undefined", "not-a-list", "http-500"])
    def test_rest_answer_that_is_not_an_evaluation(self, monkeypatch, capsys, tree,
                                                   status, body, needle):
        srv = _Server(status, body)
        try:
            rc, out, err = self._run(monkeypatch, capsys,
                                     ["--config-dir", tree, "--opa-url", srv.url, "--ci"])
        finally:
            srv.close()
        self._assert_caller_error(rc, out, err, needle)

    def test_rest_defined_empty_set_still_passes(self, monkeypatch, capsys, tree):
        srv = _Server(200, b'{"result": []}')
        try:
            rc, out, _ = self._run(monkeypatch, capsys,
                                   ["--config-dir", tree, "--opa-url", srv.url, "--ci"])
        finally:
            srv.close()
        assert rc == 0 and "All policies passed" in out

    @pytest.mark.parametrize("script, needle", [
        ("echo 'not json'\n", "is not JSON"),
        ("echo '{}'\n", "undefined"),
        ("echo 'rego_parse_error' >&2\nexit 1\n", "OPA eval failed (rc 1): rego_parse_error"),
    ], ids=["garbage", "undefined", "eval-fails"])
    def test_binary_that_does_not_evaluate(self, monkeypatch, capsys, tmp_path, tree,
                                           script, needle):
        opa = _fake_opa(tmp_path, script)
        rc, out, err = self._run(monkeypatch, capsys, [
            "--config-dir", tree, "--opa-binary", opa,
            "--policy-path", str(tmp_path / "p.rego"), "--ci"])
        self._assert_caller_error(rc, out, err, needle)

    def test_binary_missing(self, monkeypatch, capsys, tmp_path, tree):
        rc, out, err = self._run(monkeypatch, capsys, [
            "--config-dir", tree, "--opa-binary", str(tmp_path / "no-such-opa"),
            "--policy-path", str(tmp_path / "p.rego"), "--ci"])
        self._assert_caller_error(rc, out, err, "OPA binary not found")

    def test_binary_timeout(self, monkeypatch, capsys, tmp_path, tree):
        monkeypatch.setattr(pob, "OPA_TIMEOUT_SECONDS", 0.5)
        opa = _fake_opa(tmp_path, "exec sleep 5\n")
        rc, out, err = self._run(monkeypatch, capsys, [
            "--config-dir", tree, "--opa-binary", opa,
            "--policy-path", str(tmp_path / "p.rego"), "--ci"])
        self._assert_caller_error(rc, out, err, "timed out")

    def test_binary_receives_input_on_stdin(self, monkeypatch, capsys, tmp_path, tree):
        """The fake echoes a violation built from what it read on stdin: a
        tenant reaches the report only if the input arrived there (#2724: the
        old `-I <json>` made OPA refuse every run)."""
        opa = _fake_opa(tmp_path, (
            'python3 -c "import json,sys; d=json.load(sys.stdin); '
            't=sorted(d[\'served\'])[0]; '
            'print(json.dumps({\'result\':[{\'expressions\':[{\'value\':'
            '[{\'msg\':\'m\',\'severity\':\'error\',\'tenant\':t,\'field\':\'f\'}]}]}]}))"\n'))
        rc, out, err = self._run(monkeypatch, capsys, [
            "--config-dir", tree, "--opa-binary", opa,
            "--policy-path", str(tmp_path / "p.rego"), "--ci"])
        assert rc == 1, (out, err)
        assert "[tenant-a]" in out


# ---------------------------------------------------------------------------
# #2724: against a real `opa` (binary and REST server)
# ---------------------------------------------------------------------------
# Every test above fakes OPA's side; these run the real thing, so the command
# line, the query, the REST path and the response shapes are OPA's own. The
# binary comes from $OPA_BINARY, else `opa` on PATH; without one the class
# SKIPS and says so — it is not on CI's runners today.
def _opa_binary():
    import shutil
    path = os.environ.get("OPA_BINARY") or shutil.which("opa")
    if not path or not os.path.isfile(path):
        pytest.skip("NOT MEASURED: no real `opa` — set OPA_BINARY or put `opa` on PATH "
                    "to run policy_opa_bridge against real OPA (#2724)")
    return path


_REGO = """package dynamic_alerting.policy
import rego.v1
violations contains v if {
  some t
  input.served[t].mysql_connections > 90
  v := {"msg": sprintf("%s too high", [t]), "severity": "error",
        "tenant": t, "field": "mysql_connections"}
}
"""


@pytest.fixture(scope="module")
def real_opa():
    return _opa_binary()


@pytest.fixture(scope="module")
def rego_file(tmp_path_factory):
    d = tmp_path_factory.mktemp("rego")
    (d / "thr.rego").write_text(_REGO, encoding="utf-8")
    (d / "broken.rego").write_text("package dynamic_alerting.policy\nviolations contains v if {\n",
                                   encoding="utf-8")
    return d


@pytest.fixture(scope="module")
def opa_server(real_opa, rego_file):
    import socket
    import time
    port = _closed_port()
    proc = subprocess.Popen([real_opa, "run", "--server", "--addr", f"127.0.0.1:{port}",
                             str(rego_file / "thr.rego")],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                socket.create_connection(("127.0.0.1", port), 0.2).close()
                break
            except OSError:
                time.sleep(0.1)
        else:
            pytest.fail("opa server did not start")
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(10)


@pytest.mark.usefixtures("da_guard_env")
class TestRealOpa:
    def _run(self, monkeypatch, capsys, argv):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        rc = pob.main(argv)
        cap = capsys.readouterr()
        return rc, cap.out, cap.err

    @pytest.mark.parametrize("files, rc_want", [(_TENANT_VIOLATES, 1), (_TENANT_CLEAN, 0)],
                             ids=["violates", "clean"])
    def test_binary_default_package(self, monkeypatch, capsys, tmp_path, real_opa,
                                    rego_file, files, rc_want):
        tree = str(_write_tree(tmp_path / "conf.d", files))
        rc, out, err = self._run(monkeypatch, capsys, [
            "--config-dir", tree, "--opa-binary", real_opa,
            "--policy-path", str(rego_file / "thr.rego"), "--ci"])
        assert rc == rc_want, (out, err)
        if rc_want:
            assert "tenant-a too high" in out

    @pytest.mark.parametrize("files, rc_want", [(_TENANT_VIOLATES, 1), (_TENANT_CLEAN, 0)],
                             ids=["violates", "clean"])
    @pytest.mark.parametrize("package", ["dynamic_alerting.policy", "dynamic_alerting/policy"])
    def test_rest(self, monkeypatch, capsys, tmp_path, opa_server, files, rc_want, package):
        tree = str(_write_tree(tmp_path / "conf.d", files))
        rc, out, err = self._run(monkeypatch, capsys, [
            "--config-dir", tree, "--opa-url", opa_server,
            "--policy-package", package, "--ci"])
        assert rc == rc_want, (out, err)

    def test_binary_undefined_package(self, monkeypatch, capsys, tmp_path, real_opa, rego_file):
        tree = str(_write_tree(tmp_path / "conf.d", _TENANT_VIOLATES))
        rc, out, err = self._run(monkeypatch, capsys, [
            "--config-dir", tree, "--opa-binary", real_opa,
            "--policy-path", str(rego_file / "thr.rego"),
            "--policy-package", "no.such.pkg", "--ci"])
        assert rc == EXIT_CALLER_ERROR and "undefined" in err, (out, err)

    def test_rest_undefined_package(self, monkeypatch, capsys, tmp_path, opa_server):
        tree = str(_write_tree(tmp_path / "conf.d", _TENANT_VIOLATES))
        rc, out, err = self._run(monkeypatch, capsys, [
            "--config-dir", tree, "--opa-url", opa_server,
            "--policy-package", "no.such.pkg", "--ci"])
        assert rc == EXIT_CALLER_ERROR and "undefined" in err, (out, err)

    def test_binary_broken_rego(self, monkeypatch, capsys, tmp_path, real_opa, rego_file):
        tree = str(_write_tree(tmp_path / "conf.d", _TENANT_VIOLATES))
        rc, out, err = self._run(monkeypatch, capsys, [
            "--config-dir", tree, "--opa-binary", real_opa,
            "--policy-path", str(rego_file / "broken.rego"), "--ci"])
        assert rc == EXIT_CALLER_ERROR and "rego_parse_error" in err, (out, err)

    def test_defaults_shape_as_documented(self, monkeypatch, capsys, tmp_path, real_opa):
        """The module docstring: `input.defaults` is the carrier's top-level
        keys, not unwrapped — a rego reads `input.defaults.defaults.<key>`."""
        d = tmp_path / "rego"
        d.mkdir()
        (d / "d.rego").write_text(
            "package dynamic_alerting.policy\nimport rego.v1\n"
            "violations contains v if {\n"
            "  input.defaults.defaults.mysql_connections == 80\n"
            '  v := {"msg": "seen", "severity": "error", "tenant": "-", "field": "d"}\n}\n',
            encoding="utf-8")
        tree = str(_write_tree(tmp_path / "conf.d", _TENANT_CLEAN))
        rc, out, err = self._run(monkeypatch, capsys, [
            "--config-dir", tree, "--opa-binary", real_opa,
            "--policy-path", str(d / "d.rego"), "--ci"])
        assert rc == 1 and "seen" in out, (out, err)
