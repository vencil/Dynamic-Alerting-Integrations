"""Tests for offboard_tenant.py — Safe Tenant offboarding tool."""
from __future__ import annotations

import os
import sys

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', 'scripts', 'tools', 'ops')
sys.path.insert(0, _TOOLS_DIR)

import offboard_tenant as ot  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _make_config_dir(tmp_path, configs):
    """Create a conf.d directory with YAML config files.

    configs: dict of filename -> yaml_content_dict
    """
    d = tmp_path / "conf.d"
    d.mkdir()
    for name, data in configs.items():
        (d / name).write_text(yaml.dump(data), encoding="utf-8")
    return str(d)


# ---------------------------------------------------------------------------
# find_config_file
# ---------------------------------------------------------------------------
class TestFindConfigFile:
    def test_yaml_extension(self, tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "db-a.yaml").write_text("tenants:\n  db-a:\n    mysql_connections: '80'\n")
        assert ot.find_config_file("db-a", str(d)) == str(d / "db-a.yaml")

    def test_yml_extension(self, tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "db-a.yml").write_text("tenants:\n  db-a:\n    mysql_connections: '80'\n")
        assert ot.find_config_file("db-a", str(d)) == str(d / "db-a.yml")

    def test_not_found(self, tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        assert ot.find_config_file("db-x", str(d)) is None


# ---------------------------------------------------------------------------
# load_all_configs
# ---------------------------------------------------------------------------
class TestLoadAllConfigs:
    def test_loads_multiple_files(self, tmp_path):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": "80"}}},
            "db-b.yaml": {"tenants": {"db-b": {"pg_connections": "100"}}},
        }
        d = _make_config_dir(tmp_path, configs)
        result = ot.load_all_configs(d)
        assert len(result) == 2
        assert "db-a.yaml" in result
        assert "db-b.yaml" in result

    def test_skips_dotfiles(self, tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / ".hidden.yaml").write_text("hidden: true\n")
        (d / "db-a.yaml").write_text("tenants:\n  db-a:\n    x: '1'\n")
        result = ot.load_all_configs(str(d))
        assert len(result) == 1
        assert ".hidden.yaml" not in result

    def test_empty_directory(self, tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        result = ot.load_all_configs(str(d))
        assert len(result) == 0

    def test_invalid_yaml_handled(self, tmp_path):
        d = tmp_path / "conf.d"
        d.mkdir()
        (d / "bad.yaml").write_text("this: is: not: valid: yaml: {{{\n")
        (d / "good.yaml").write_text("tenants:\n  db-a:\n    x: '1'\n")
        result = ot.load_all_configs(str(d))
        # bad.yaml may fail to load, but good.yaml should succeed
        assert "good.yaml" in result


# ---------------------------------------------------------------------------
# check_cross_references
# ---------------------------------------------------------------------------
class TestCheckCrossReferences:
    def test_no_references(self, tmp_path):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": "80"}}},
            "db-b.yaml": {"tenants": {"db-b": {"pg_connections": "100"}}},
        }
        d = _make_config_dir(tmp_path, configs)
        all_configs = ot.load_all_configs(d)
        refs = ot.check_cross_references("db-a", all_configs)
        assert refs == []

    def test_found_reference(self, tmp_path):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": "80"}}},
            "_defaults.yaml": {"inherit_from": "db-a"},
        }
        d = _make_config_dir(tmp_path, configs)
        all_configs = ot.load_all_configs(d)
        refs = ot.check_cross_references("db-a", all_configs)
        assert "_defaults.yaml" in refs

    def test_skips_own_file(self, tmp_path):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"note": "db-a config"}}},
        }
        d = _make_config_dir(tmp_path, configs)
        all_configs = ot.load_all_configs(d)
        refs = ot.check_cross_references("db-a", all_configs)
        assert refs == []


# ---------------------------------------------------------------------------
# get_tenant_metrics
# ---------------------------------------------------------------------------
class TestGetTenantMetrics:
    def test_returns_metrics(self, tmp_path):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": "80", "mysql_cpu": "75"}}},
        }
        d = _make_config_dir(tmp_path, configs)
        all_configs = ot.load_all_configs(d)
        metrics = ot.get_tenant_metrics("db-a", all_configs)
        assert metrics == {"mysql_connections": "80", "mysql_cpu": "75"}

    def test_returns_empty_for_missing_tenant(self, tmp_path):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": "80"}}},
        }
        d = _make_config_dir(tmp_path, configs)
        all_configs = ot.load_all_configs(d)
        metrics = ot.get_tenant_metrics("db-x", all_configs)
        assert metrics == {}

    def test_returns_empty_for_no_tenants_key(self, tmp_path):
        configs = {
            "_defaults.yaml": {"some_key": "value"},
        }
        d = _make_config_dir(tmp_path, configs)
        all_configs = ot.load_all_configs(d)
        metrics = ot.get_tenant_metrics("db-a", all_configs)
        assert metrics == {}


# ---------------------------------------------------------------------------
# run_precheck
# ---------------------------------------------------------------------------
class TestRunPrecheck:
    def test_pass_clean_tenant(self, tmp_path):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": "80"}}},
            "db-b.yaml": {"tenants": {"db-b": {"pg_connections": "100"}}},
        }
        d = _make_config_dir(tmp_path, configs)
        can_proceed, report = ot.run_precheck("db-a", d)
        assert can_proceed is True
        report_text = "\n".join(report)
        assert "Pre-check 通過" in report_text or "可安全下架" in report_text

    def test_fail_missing_file(self, tmp_path):
        d = _make_config_dir(tmp_path, {
            "db-b.yaml": {"tenants": {"db-b": {"pg_connections": "100"}}},
        })
        can_proceed, report = ot.run_precheck("db-a", d)
        report_text = "\n".join(report)
        assert "找不到設定檔案" in report_text

    def test_warn_cross_reference(self, tmp_path):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": "80"}}},
            "_defaults.yaml": {"ref": "db-a"},
        }
        d = _make_config_dir(tmp_path, configs)
        can_proceed, report = ot.run_precheck("db-a", d)
        assert can_proceed is True  # cross-ref is a warning, not a blocker
        report_text = "\n".join(report)
        assert "跨檔案引用" in report_text

    def test_lists_metrics(self, tmp_path):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": "80", "mysql_cpu": "75"}}},
        }
        d = _make_config_dir(tmp_path, configs)
        _, report = ot.run_precheck("db-a", d)
        report_text = "\n".join(report)
        assert "mysql_connections" in report_text
        assert "2 個" in report_text


# ---------------------------------------------------------------------------
# execute_offboard
# ---------------------------------------------------------------------------
class TestExecuteOffboard:
    def test_deletes_file(self, tmp_path):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": "80"}}},
        }
        d = _make_config_dir(tmp_path, configs)
        result = ot.execute_offboard("db-a", d)
        assert result is True
        assert not os.path.exists(os.path.join(d, "db-a.yaml"))

    def test_missing_file(self, tmp_path):
        d = _make_config_dir(tmp_path, {})
        result = ot.execute_offboard("db-x", d)
        assert result is False


# ---------------------------------------------------------------------------
# main CLI
# ---------------------------------------------------------------------------
class TestMainCLI:
    def test_precheck_mode(self, tmp_path, capsys):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": "80"}}},
        }
        d = _make_config_dir(tmp_path, configs)

        with pytest.raises(SystemExit) if False else __import__("contextlib").nullcontext():
            import sys as _sys
            _sys.argv = ["offboard_tenant.py", "db-a", "--config-dir", d]
            ot.main()

        captured = capsys.readouterr()
        assert "Pre-check" in captured.out

    def test_execute_mode(self, tmp_path, capsys):
        configs = {
            "db-a.yaml": {"tenants": {"db-a": {"mysql_connections": "80"}}},
        }
        d = _make_config_dir(tmp_path, configs)

        import sys as _sys
        _sys.argv = ["offboard_tenant.py", "db-a", "--config-dir", d, "--execute"]
        ot.main()

        captured = capsys.readouterr()
        assert "已刪除" in captured.out
        assert not os.path.exists(os.path.join(d, "db-a.yaml"))

    def test_execute_fails_on_precheck_failure(self, tmp_path):
        """If config file doesn't exist, precheck fails and execute should not proceed."""
        d = _make_config_dir(tmp_path, {})

        import sys as _sys
        _sys.argv = ["offboard_tenant.py", "db-nonexistent", "--config-dir", d, "--execute"]
        with pytest.raises(SystemExit) as exc_info:
            ot.main()
        assert exc_info.value.code == 1
