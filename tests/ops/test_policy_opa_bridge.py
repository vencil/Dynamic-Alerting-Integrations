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
# load_tenant_configs
# ---------------------------------------------------------------------------
class TestLoadTenantConfigs:
    def test_missing_dir_returns_empty(self, tmp_path):
        ghost = tmp_path / "ghost"
        assert pob.load_tenant_configs(str(ghost)) == {}

    def test_flat_format_uses_filename_as_tenant(self, tmp_path, monkeypatch):
        # Stub load_yaml_file so we don't need PyYAML available.
        files = {
            str(tmp_path / "db-a.yaml"): {"mysql_connections": "70"},
            str(tmp_path / "db-b.yml"): {"redis_memory": "1024"},
        }
        for p in files:
            Path(p).write_text("x", encoding="utf-8")
        monkeypatch.setattr(pob, "load_yaml_file",
                            lambda p: files.get(p, None))
        configs = pob.load_tenant_configs(str(tmp_path))
        assert configs["db-a"] == {"mysql_connections": "70"}
        assert configs["db-b"] == {"redis_memory": "1024"}

    def test_multi_tenant_wrapper_format(self, tmp_path, monkeypatch):
        # File contains {tenants: {db-a: {...}, db-b: {...}}}.
        f = tmp_path / "all.yaml"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(pob, "load_yaml_file", lambda p: {
            "tenants": {
                "db-a": {"mysql_connections": "70"},
                "db-b": {"redis_memory": "1024"},
            },
        })
        configs = pob.load_tenant_configs(str(tmp_path))
        assert "db-a" in configs and "db-b" in configs

    def test_underscore_prefix_files_skipped(self, tmp_path, monkeypatch):
        (tmp_path / "_defaults.yaml").write_text("x", encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text("x", encoding="utf-8")
        monkeypatch.setattr(pob, "load_yaml_file", lambda p: {"k": "v"})
        configs = pob.load_tenant_configs(str(tmp_path))
        assert "db-a" in configs
        assert "_defaults" not in configs

    def test_non_yaml_extensions_ignored(self, tmp_path, monkeypatch):
        (tmp_path / "readme.md").write_text("x", encoding="utf-8")
        (tmp_path / "db-a.yaml").write_text("x", encoding="utf-8")
        monkeypatch.setattr(pob, "load_yaml_file", lambda p: {"k": "v"})
        configs = pob.load_tenant_configs(str(tmp_path))
        assert "readme" not in configs
        assert "db-a" in configs

    def test_non_dict_yaml_skipped(self, tmp_path, monkeypatch):
        (tmp_path / "weird.yaml").write_text("x", encoding="utf-8")
        monkeypatch.setattr(pob, "load_yaml_file", lambda p: ["a list", "not a dict"])
        configs = pob.load_tenant_configs(str(tmp_path))
        assert configs == {}

    def test_wrapper_with_non_dict_tenant_value_skipped(self, tmp_path, monkeypatch):
        (tmp_path / "x.yaml").write_text("x", encoding="utf-8")
        monkeypatch.setattr(pob, "load_yaml_file", lambda p: {
            "tenants": {
                "db-a": {"mysql_connections": "70"},
                "db-b": "not a dict",  # skipped
            },
        })
        configs = pob.load_tenant_configs(str(tmp_path))
        assert "db-a" in configs
        assert "db-b" not in configs


# ---------------------------------------------------------------------------
# load_defaults
# ---------------------------------------------------------------------------
class TestLoadDefaults:
    def test_missing_file_returns_empty(self, tmp_path):
        assert pob.load_defaults(str(tmp_path)) == {}

    def test_loads_dict(self, tmp_path, monkeypatch):
        f = tmp_path / "_defaults.yaml"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(pob, "load_yaml_file",
                            lambda p: {"mysql_connections": 80})
        assert pob.load_defaults(str(tmp_path)) == {"mysql_connections": 80}

    def test_non_dict_returns_empty(self, tmp_path, monkeypatch):
        f = tmp_path / "_defaults.yaml"
        f.write_text("x", encoding="utf-8")
        monkeypatch.setattr(pob, "load_yaml_file", lambda p: ["list"])
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

    def test_url_error_returns_empty_with_stderr(self, monkeypatch, capsys):
        def boom(*a, **kw):
            raise URLError("connection refused")
        monkeypatch.setattr(pob, "urlopen", boom)
        assert pob.call_opa_rest("http://x", "p", {}) == []
        assert "OPA API call failed" in capsys.readouterr().err

    def test_invalid_json_returns_empty(self, monkeypatch, capsys):
        self._stub_urlopen(monkeypatch, b"{not json")
        assert pob.call_opa_rest("http://x", "p", {}) == []
        assert "OPA response parsing failed" in capsys.readouterr().err

    def test_result_not_a_list_returns_empty(self, monkeypatch):
        body = json.dumps({"result": "string-not-list"}).encode("utf-8")
        self._stub_urlopen(monkeypatch, body)
        assert pob.call_opa_rest("http://x", "p", {}) == []

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
        monkeypatch.setattr(pob.subprocess, "run", lambda *a, **kw: proc)

    def test_success_returns_violations(self, monkeypatch):
        self._stub_run(monkeypatch, 0, json.dumps({
            "result": [{"msg": "x", "severity": "error", "tenant": "t", "field": "f"}],
        }))
        out = pob.call_opa_binary("opa", "/p.rego", "pkg", {})
        assert len(out) == 1

    def test_nonzero_returncode_returns_empty(self, monkeypatch, capsys):
        self._stub_run(monkeypatch, 1, "", "policy parse error")
        assert pob.call_opa_binary("opa", "/p.rego", "pkg", {}) == []
        assert "OPA eval failed" in capsys.readouterr().err

    def test_binary_not_found_returns_empty(self, monkeypatch, capsys):
        def boom(*a, **kw):
            raise FileNotFoundError("opa not found")
        monkeypatch.setattr(pob.subprocess, "run", boom)
        assert pob.call_opa_binary("opa", "/p.rego", "pkg", {}) == []
        assert "OPA binary not found" in capsys.readouterr().err

    def test_timeout_returns_empty(self, monkeypatch, capsys):
        def boom(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="opa", timeout=10)
        monkeypatch.setattr(pob.subprocess, "run", boom)
        assert pob.call_opa_binary("opa", "/p.rego", "pkg", {}) == []
        assert "OPA eval timeout" in capsys.readouterr().err

    def test_invalid_json_output_returns_empty(self, monkeypatch, capsys):
        self._stub_run(monkeypatch, 0, "{not json", "")
        assert pob.call_opa_binary("opa", "/p.rego", "pkg", {}) == []
        assert "OPA output parsing failed" in capsys.readouterr().err

    def test_result_not_a_list_returns_empty(self, monkeypatch):
        self._stub_run(monkeypatch, 0, json.dumps({"result": "scalar"}), "")
        assert pob.call_opa_binary("opa", "/p.rego", "pkg", {}) == []


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
            "tenant": "db-a", "field": "x",
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
class TestMain:
    def test_no_tenant_configs_returns_zero(self, monkeypatch, tmp_path, capsys):
        # Empty config-dir → no tenant configs → return 0 with informational msg.
        # #1112: the message is prose → stderr; stdout stays clean for the JSON
        # document (see the two envelope tests below).
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        rc = pob.main(["--config-dir", str(tmp_path)])
        assert rc == 0
        captured = capsys.readouterr()
        assert "No tenant configs found" in captured.err
        assert captured.out == ""

    def test_no_tenant_configs_json_envelope(self, monkeypatch, tmp_path, capsys):
        """#1112: --json + 空 config-dir → 一份歸零的 report（可被同一 consumer 消費）。"""
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
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        rc = pob.main(["--config-dir", str(tmp_path), "--dry-run", "--json"])
        assert rc == 0
        doc = json.loads(capsys.readouterr().out)
        assert doc["tenants"] == {}
        assert "platform_version" in doc

    def test_dry_run_prints_input_json(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        # Stub load_tenant_configs to return one tenant.
        monkeypatch.setattr(pob, "load_tenant_configs",
                            lambda d: {"db-a": {"x": 1}})
        monkeypatch.setattr(pob, "load_defaults", lambda d: {"y": 2})
        rc = pob.main(["--config-dir", str(tmp_path), "--dry-run"])
        assert rc == 0
        out = capsys.readouterr().out
        payload = json.loads(out)
        assert "tenants" in payload
        assert payload["tenants"]["db-a"] == {"x": 1}

    def test_no_url_no_path_returns_caller_error(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        monkeypatch.setattr(pob, "load_tenant_configs",
                            lambda d: {"db-a": {"x": 1}})
        monkeypatch.setattr(pob, "load_defaults", lambda d: {})
        rc = pob.main(["--config-dir", str(tmp_path)])
        assert rc == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "Must specify" in err

    def test_opa_url_path_evaluates_via_rest(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        monkeypatch.setattr(pob, "load_tenant_configs",
                            lambda d: {"db-a": {"x": 1}})
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
        monkeypatch.setattr(pob, "load_tenant_configs",
                            lambda d: {"db-a": {"x": 1}})
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
        monkeypatch.setattr(pob, "load_tenant_configs",
                            lambda d: {"db-a": {"x": 1}})
        monkeypatch.setattr(pob, "load_defaults", lambda d: {})
        monkeypatch.setattr(pob, "call_opa_rest", lambda *a, **kw: [{
            "msg": "bad", "severity": "error",
            "tenant": "db-a", "field": "x",
        }])
        rc = pob.main([
            "--config-dir", str(tmp_path),
            "--opa-url", "http://x",
            "--ci",
        ])
        assert rc == 1

    def test_ci_with_only_warnings_returns_zero(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        monkeypatch.setattr(pob, "load_tenant_configs",
                            lambda d: {"db-a": {"x": 1}})
        monkeypatch.setattr(pob, "load_defaults", lambda d: {})
        monkeypatch.setattr(pob, "call_opa_rest", lambda *a, **kw: [{
            "msg": "soft", "severity": "warning",
            "tenant": "db-a", "field": "x",
        }])
        rc = pob.main([
            "--config-dir", str(tmp_path),
            "--opa-url", "http://x",
            "--ci",
        ])
        assert rc == 0  # warnings don't fail in CI mode

    def test_json_output_emits_json(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "en")
        monkeypatch.setattr(pob, "load_tenant_configs",
                            lambda d: {"db-a": {"x": 1}})
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
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "zh")
        rc = pob.main(["--config-dir", str(tmp_path)])
        assert rc == 0
        assert "未找到 tenant 配置" in capsys.readouterr().err

    def test_zh_no_url_no_path_error_message(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(pob, "detect_cli_lang", lambda: "zh")
        monkeypatch.setattr(pob, "load_tenant_configs",
                            lambda d: {"db-a": {"x": 1}})
        monkeypatch.setattr(pob, "load_defaults", lambda d: {})
        rc = pob.main(["--config-dir", str(tmp_path)])
        assert rc == EXIT_CALLER_ERROR
        err = capsys.readouterr().err
        assert "必須指定" in err
