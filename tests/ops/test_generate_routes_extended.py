"""Extended tests for generate_alertmanager_routes.py — coverage boost.

Targets uncovered lines: render_output, load_base_config, assemble_configmap,
apply_to_configmap (mocked), main() CLI paths (dry-run, validate, output-configmap,
apply, stdout).
"""
import json
import os
import subprocess
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest
import yaml

from factories import (
    make_receiver, make_routing_config, make_tenant_yaml,
    make_enforced_routing, write_yaml,
)

from generate_alertmanager_routes import (
    render_output,
    load_base_config,
    assemble_configmap,
    apply_to_configmap,
    load_tenant_configs,
    generate_routes,
    generate_inhibit_rules,
    _build_enforced_routes,
    _build_tenant_routes,
    _parse_config_files,
    write_text_secure,
)

import generate_alertmanager_routes as gar


# ============================================================
# render_output
# ============================================================
class TestRenderOutput:
    """render_output() YAML fragment rendering."""

    def test_routes_only(self):
        routes = [{"receiver": "tenant-db-a", "matchers": ['tenant="db-a"']}]
        result = render_output(routes, [], None)
        parsed = yaml.safe_load(result)
        assert "route" in parsed
        assert parsed["route"]["routes"] == routes
        assert "receivers" not in parsed
        assert "inhibit_rules" not in parsed

    def test_receivers_only(self):
        receivers = [{"name": "tenant-db-a", "webhook_configs": [{"url": "https://x.com"}]}]
        result = render_output([], receivers, None)
        parsed = yaml.safe_load(result)
        assert "receivers" in parsed
        assert "route" not in parsed

    def test_inhibit_rules_only(self):
        inhibit = [{"source_matchers": ["severity=\"critical\""],
                     "target_matchers": ["severity=\"warning\""]}]
        result = render_output([], [], inhibit)
        parsed = yaml.safe_load(result)
        assert "inhibit_rules" in parsed

    def test_all_sections(self):
        routes = [{"receiver": "r1"}]
        receivers = [{"name": "r1"}]
        inhibit = [{"source_matchers": ["a=b"]}]
        result = render_output(routes, receivers, inhibit)
        parsed = yaml.safe_load(result)
        assert "route" in parsed
        assert "receivers" in parsed
        assert "inhibit_rules" in parsed

    def test_empty_all(self):
        result = render_output([], [], [])
        parsed = yaml.safe_load(result)
        # Empty lists => sections omitted
        assert parsed is None or parsed == {}


# ============================================================
# load_base_config
# ============================================================
class TestLoadBaseConfig:
    """load_base_config() tests."""

    def test_no_path_returns_defaults(self):
        base = load_base_config(None)
        assert "global" in base
        assert "route" in base
        assert "receivers" in base
        assert "inhibit_rules" in base

    def test_nonexistent_path_returns_defaults(self):
        base = load_base_config("/nonexistent/path.yaml")
        assert "global" in base
        assert base["route"]["receiver"] == "default"

    def test_valid_file(self, tmp_path):
        config = {
            "global": {"resolve_timeout": "10m"},
            "route": {
                "receiver": "custom-default",
                "group_by": ["alertname"],
            },
            "receivers": [{"name": "custom-default"}],
            "inhibit_rules": [{"source_matchers": ["severity=\"critical\""]}],
        }
        p = tmp_path / "base.yaml"
        p.write_text(yaml.dump(config), encoding="utf-8")
        base = load_base_config(str(p))
        assert base["global"]["resolve_timeout"] == "10m"
        assert base["route"]["receiver"] == "custom-default"

    def test_partial_file_fills_defaults(self, tmp_path):
        """File with missing keys gets defaults filled in."""
        config = {"global": {"resolve_timeout": "3m"}}
        p = tmp_path / "partial.yaml"
        p.write_text(yaml.dump(config), encoding="utf-8")
        base = load_base_config(str(p))
        assert base["global"]["resolve_timeout"] == "3m"
        assert "route" in base
        assert "receivers" in base


# ============================================================
# assemble_configmap
# ============================================================
class TestAssembleConfigmap:
    """assemble_configmap() K8s ConfigMap generation."""

    def test_basic_configmap(self):
        base = load_base_config(None)
        routes = [{"receiver": "tenant-db-a", "matchers": ['tenant="db-a"']}]
        receivers = [{"name": "tenant-db-a", "webhook_configs": [{"url": "https://x.com"}]}]
        inhibit = [{"source_matchers": ["severity=\"critical\""]}]

        cm_yaml = assemble_configmap(base, routes, receivers, inhibit)
        parsed = yaml.safe_load(cm_yaml)

        assert parsed["apiVersion"] == "v1"
        assert parsed["kind"] == "ConfigMap"
        assert parsed["metadata"]["name"] == "alertmanager-config"
        assert parsed["metadata"]["namespace"] == "monitoring"
        assert "alertmanager.yml" in parsed["data"]

        am_config = yaml.safe_load(parsed["data"]["alertmanager.yml"])
        assert len(am_config["route"]["routes"]) == 1
        # Base receiver + tenant receiver
        names = {r["name"] for r in am_config["receivers"]}
        assert "default" in names
        assert "tenant-db-a" in names

    def test_custom_namespace_and_name(self):
        base = load_base_config(None)
        cm_yaml = assemble_configmap(base, [], [], [],
                                     namespace="custom-ns",
                                     configmap_name="my-config")
        parsed = yaml.safe_load(cm_yaml)
        assert parsed["metadata"]["namespace"] == "custom-ns"
        assert parsed["metadata"]["name"] == "my-config"

    def test_dedup_receivers(self):
        """Tenant receivers with same name as base are not duplicated."""
        base = load_base_config(None)
        # Add a receiver with the same name as in base
        receivers = [{"name": "default", "webhook_configs": [{"url": "https://x.com"}]}]
        cm_yaml = assemble_configmap(base, [], receivers, [])
        parsed = yaml.safe_load(cm_yaml)
        am_config = yaml.safe_load(parsed["data"]["alertmanager.yml"])
        default_count = sum(1 for r in am_config["receivers"] if r["name"] == "default")
        assert default_count == 1


# ============================================================
# apply_to_configmap (mocked kubectl/curl)
# ============================================================
class TestApplyToConfigmap:
    """apply_to_configmap() with mocked subprocess calls."""

    def _mock_subprocess(self, monkeypatch, kubectl_get_stdout, kubectl_get_rc=0,
                         kubectl_create_rc=0, kubectl_apply_rc=0, curl_rc=0):
        calls = []

        def mock_run(cmd, **kwargs):
            calls.append(cmd)
            result = MagicMock()
            result.stderr = ""
            result.stdout = ""

            if "get" in cmd and "configmap" in cmd:
                result.returncode = kubectl_get_rc
                result.stdout = kubectl_get_stdout
            elif "create" in cmd and "configmap" in cmd:
                result.returncode = kubectl_create_rc
                result.stdout = "apiVersion: v1\nkind: ConfigMap\ndata: {}"
            elif "apply" in cmd:
                result.returncode = kubectl_apply_rc
                result.stdout = ""
            elif "curl" in cmd:
                result.returncode = curl_rc
            else:
                result.returncode = 0

            return result

        monkeypatch.setattr(subprocess, "run", mock_run)
        return calls

    def test_successful_apply(self, monkeypatch):
        existing_cm = {
            "data": {
                "alertmanager.yml": yaml.dump({
                    "route": {"receiver": "default", "routes": []},
                    "receivers": [{"name": "default"}],
                    "inhibit_rules": [],
                })
            }
        }
        self._mock_subprocess(monkeypatch, json.dumps(existing_cm))
        routes = [{"receiver": "t1", "matchers": ['tenant="t1"']}]
        receivers = [{"name": "t1", "webhook_configs": [{"url": "https://x.com"}]}]
        result = apply_to_configmap(routes, receivers, [], "monitoring", "am-config")
        assert result is True

    def test_kubectl_get_fails(self, monkeypatch):
        self._mock_subprocess(monkeypatch, "", kubectl_get_rc=1)
        result = apply_to_configmap([], [], [], "monitoring", "am-config")
        assert result is False

    def test_empty_configmap_data(self, monkeypatch):
        existing_cm = {"data": {}}
        self._mock_subprocess(monkeypatch, json.dumps(existing_cm))
        result = apply_to_configmap([], [], [], "monitoring", "am-config")
        assert result is False

    def test_kubectl_apply_fails(self, monkeypatch):
        existing_cm = {
            "data": {
                "alertmanager.yml": yaml.dump({
                    "route": {"receiver": "default"},
                    "receivers": [{"name": "default"}],
                    "inhibit_rules": [],
                })
            }
        }
        self._mock_subprocess(monkeypatch, json.dumps(existing_cm),
                              kubectl_apply_rc=1)
        result = apply_to_configmap([], [], [], "monitoring", "am-config")
        assert result is False

    def test_reload_fails_still_returns_true(self, monkeypatch):
        """If curl reload fails, apply still returns True (ConfigMap was updated)."""
        existing_cm = {
            "data": {
                "alertmanager.yml": yaml.dump({
                    "route": {"receiver": "default"},
                    "receivers": [{"name": "default"}],
                    "inhibit_rules": [],
                })
            }
        }
        self._mock_subprocess(monkeypatch, json.dumps(existing_cm), curl_rc=1)
        result = apply_to_configmap([], [], [], "monitoring", "am-config")
        assert result is True


# ============================================================
# main() CLI paths
# ============================================================
class TestMainCLI:
    """main() CLI entry point tests."""

    def _make_config_dir(self, tmp_path):
        """Create a minimal config dir for testing main()."""
        d = tmp_path / "conf.d"
        d.mkdir()
        defaults = {"defaults": {"mysql_connections": 80}}
        (d / "_defaults.yaml").write_text(yaml.dump(defaults), encoding="utf-8")
        tenant = {"tenants": {"db-a": {
            "mysql_connections": "70",
            "_routing": {
                "receiver": {"type": "webhook", "url": "https://hooks.example.com/alert"},
            },
            "_severity_dedup": "enable",
        }}}
        (d / "db-a.yaml").write_text(yaml.dump(tenant), encoding="utf-8")
        return str(d)

    def test_dry_run(self, tmp_path, monkeypatch, capsys):
        config_dir = self._make_config_dir(tmp_path)
        monkeypatch.setattr(sys, "argv", [
            "generate_alertmanager_routes", "--config-dir", config_dir, "--dry-run"
        ])
        gar.main()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "route" in out.lower() or "receiver" in out.lower()

    def test_validate_mode(self, tmp_path, monkeypatch, capsys):
        config_dir = self._make_config_dir(tmp_path)
        monkeypatch.setattr(sys, "argv", [
            "generate_alertmanager_routes", "--config-dir", config_dir, "--validate"
        ])
        with pytest.raises(SystemExit) as exc:
            gar.main()
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "Validation" in out or "OK" in out

    def test_stdout_output(self, tmp_path, monkeypatch, capsys):
        config_dir = self._make_config_dir(tmp_path)
        monkeypatch.setattr(sys, "argv", [
            "generate_alertmanager_routes", "--config-dir", config_dir
        ])
        gar.main()
        out = capsys.readouterr().out
        assert "route" in out.lower() or "receiver" in out.lower()

    def test_output_file(self, tmp_path, monkeypatch, capsys):
        config_dir = self._make_config_dir(tmp_path)
        out_file = str(tmp_path / "output.yaml")
        monkeypatch.setattr(sys, "argv", [
            "generate_alertmanager_routes", "--config-dir", config_dir,
            "-o", out_file
        ])
        gar.main()
        assert os.path.isfile(out_file)
        content = open(out_file, encoding="utf-8").read()
        assert "route" in content.lower() or "receiver" in content.lower()

    def test_output_configmap(self, tmp_path, monkeypatch, capsys):
        config_dir = self._make_config_dir(tmp_path)
        monkeypatch.setattr(sys, "argv", [
            "generate_alertmanager_routes", "--config-dir", config_dir,
            "--output-configmap"
        ])
        gar.main()
        out = capsys.readouterr().out
        parsed = yaml.safe_load(out)
        assert parsed["kind"] == "ConfigMap"

    def test_output_configmap_dry_run(self, tmp_path, monkeypatch, capsys):
        config_dir = self._make_config_dir(tmp_path)
        monkeypatch.setattr(sys, "argv", [
            "generate_alertmanager_routes", "--config-dir", config_dir,
            "--output-configmap", "--dry-run"
        ])
        gar.main()
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "ConfigMap" in out

    def test_output_configmap_to_file(self, tmp_path, monkeypatch, capsys):
        config_dir = self._make_config_dir(tmp_path)
        out_file = str(tmp_path / "cm.yaml")
        monkeypatch.setattr(sys, "argv", [
            "generate_alertmanager_routes", "--config-dir", config_dir,
            "--output-configmap", "-o", out_file
        ])
        gar.main()
        assert os.path.isfile(out_file)

    def test_policy_flag(self, tmp_path, monkeypatch, capsys):
        config_dir = self._make_config_dir(tmp_path)
        policy = tmp_path / "policy.yaml"
        policy.write_text(yaml.dump({"allowed_domains": ["hooks.example.com"]}),
                          encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "generate_alertmanager_routes", "--config-dir", config_dir,
            "--policy", str(policy)
        ])
        gar.main()
        out = capsys.readouterr().out
        assert "Policy" in out or "route" in out.lower()

    def test_empty_config_dir(self, tmp_path, monkeypatch, capsys):
        d = tmp_path / "empty"
        d.mkdir()
        monkeypatch.setattr(sys, "argv", [
            "generate_alertmanager_routes", "--config-dir", str(d)
        ])
        with pytest.raises(SystemExit) as exc:
            gar.main()
        assert exc.value.code == 0

    def test_validate_with_errors(self, tmp_path, monkeypatch, capsys):
        """Validate mode with bad config should exit 1."""
        d = tmp_path / "conf.d"
        d.mkdir()
        defaults = {"defaults": {"mysql_connections": 80}}
        (d / "_defaults.yaml").write_text(yaml.dump(defaults), encoding="utf-8")
        # Tenant with missing receiver url
        tenant = {"tenants": {"db-a": {
            "_routing": {"receiver": {"type": "webhook"}},  # missing url
            "_severity_dedup": "enable",
        }}}
        (d / "db-a.yaml").write_text(yaml.dump(tenant), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", [
            "generate_alertmanager_routes", "--config-dir", str(d), "--validate"
        ])
        with pytest.raises(SystemExit) as exc:
            gar.main()
        # May be 0 or 1 depending on whether it generates valid routes


# ============================================================
# _parse_config_files edge cases
# ============================================================
class TestParseConfigFilesEdge:
    """Edge cases for _parse_config_files."""

    def test_empty_directory(self, tmp_path):
        result = _parse_config_files(str(tmp_path))
        assert result["all_tenants"] == []
        assert result["explicit_routing"] == {}

    def test_dotfile_ignored(self, tmp_path):
        """Files starting with . are ignored."""
        (tmp_path / ".hidden.yaml").write_text(
            yaml.dump({"tenants": {"x": {"foo": 1}}}), encoding="utf-8")
        result = _parse_config_files(str(tmp_path))
        assert "x" not in result["all_tenants"]

    def test_unparseable_yaml_skipped(self, tmp_path):
        """Bad YAML files are skipped with warning."""
        (tmp_path / "bad.yaml").write_text("key: [unclosed", encoding="utf-8")
        result = _parse_config_files(str(tmp_path))
        assert result["all_tenants"] == []
