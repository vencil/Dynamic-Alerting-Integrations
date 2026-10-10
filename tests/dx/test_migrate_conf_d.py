"""Tests for migrate_conf_d.py — Migrate flat conf.d/ to hierarchical layout."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_TOOLS_DIR = os.path.join(os.path.dirname(__file__), '..', '..', 'scripts', 'tools', 'dx')
sys.path.insert(0, _TOOLS_DIR)

import migrate_conf_d as mcd  # noqa: E402


class TestLoadYaml:
    """Tests for _load_yaml()."""

    def test_load_valid_yaml(self, tmp_path):
        p = tmp_path / "test.yaml"
        p.write_text("tenants:\n  tenant-a:\n    _metadata:\n      domain: finance\n")
        result = mcd._load_yaml(p)
        assert "tenants" in result

    def test_load_empty_yaml(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("")
        assert mcd._load_yaml(p) == {}


class TestExtractMetadata:
    """Tests for _extract_metadata()."""

    def test_extract_with_metadata(self):
        data = {
            "tenants": {
                "tenant-a": {
                    "_metadata": {"domain": "finance", "region": "us-east", "environment": "prod"}
                }
            }
        }
        meta = mcd._extract_metadata(data)
        assert meta == {"domain": "finance", "region": "us-east", "environment": "prod"}

    def test_extract_no_tenants(self):
        assert mcd._extract_metadata({}) is None

    def test_extract_tenants_not_dict(self):
        assert mcd._extract_metadata({"tenants": []}) is None

    def test_extract_no_metadata_in_tenant(self):
        data = {"tenants": {"tenant-a": {"config": "value"}}}
        assert mcd._extract_metadata(data) is None


class TestPlanMigration:
    """Tests for plan_migration()."""

    def test_plan_migration_with_metadata(self, tmp_path):
        """Plan migration for YAML with _metadata -> status ok, correct target path."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "tenant-a.yaml").write_text(yaml.dump({
            "tenants": {
                "tenant-a": {
                    "_metadata": {
                        "domain": "finance",
                        "region": "us-east",
                        "environment": "prod"
                    }
                }
            }
        }))

        actions = mcd.plan_migration(conf_d)
        assert len(actions) == 1
        assert actions[0]["status"] == "ok"
        assert actions[0]["source"] == "tenant-a.yaml"
        assert actions[0]["target"] == "finance/us-east/prod/tenant-a.yaml"
        assert actions[0]["tenant_id"] == "tenant-a"

    def test_plan_migration_no_metadata(self, tmp_path):
        """YAML without _metadata -> status skip_no_metadata."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "tenant-b.yaml").write_text(yaml.dump({
            "tenants": {"tenant-b": {"config": "value"}}
        }))

        actions = mcd.plan_migration(conf_d)
        assert actions[0]["status"] == "skip_no_metadata"
        assert actions[0]["source"] == "tenant-b.yaml"

    def test_plan_migration_system_file(self, tmp_path):
        """File starting with _ -> status skip_system_file."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "_metadata.yaml").write_text("# system\n")

        actions = mcd.plan_migration(conf_d)
        assert actions[0]["status"] == "skip_system_file"

    def test_plan_migration_nested_dir(self, tmp_path):
        """Directory in conf.d/ -> status skip_already_nested."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "finance").mkdir()

        actions = mcd.plan_migration(conf_d)
        assert actions[0]["status"] == "skip_already_nested"
        assert actions[0]["source"] == "finance/"

    def test_plan_migration_partial_metadata(self, tmp_path):
        """Handle domain + region without environment."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "tenant-a.yaml").write_text(yaml.dump({
            "tenants": {
                "tenant-a": {
                    "_metadata": {"domain": "finance", "region": "us-east"}
                }
            }
        }))

        actions = mcd.plan_migration(conf_d)
        assert actions[0]["status"] == "ok"
        assert actions[0]["target"] == "finance/us-east/tenant-a.yaml"

    def test_plan_migration_mixed_files(self, tmp_path):
        """Mixed file types -> all statuses present."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "tenant-a.yaml").write_text(yaml.dump({
            "tenants": {
                "tenant-a": {
                    "_metadata": {"domain": "finance"}
                }
            }
        }))
        (conf_d / "tenant-b.yaml").write_text(yaml.dump({"tenants": {"tenant-b": {}}}))
        (conf_d / "_system.yaml").write_text("# system\n")
        (conf_d / "existing").mkdir()

        actions = mcd.plan_migration(conf_d)
        statuses = {a["status"] for a in actions}
        assert statuses == {"ok", "skip_no_metadata", "skip_system_file", "skip_already_nested"}


class TestGenerateGitCommands:
    """Tests for generate_git_commands()."""

    def test_generate_git_commands(self, tmp_path):
        """Correct mkdir -p + git mv list format."""
        conf_d = tmp_path / "conf.d"
        actions = [{
            "source": "tenant-a.yaml",
            "target": "finance/us-east/prod/tenant-a.yaml",
            "tenant_id": "tenant-a",
            "metadata": {"domain": "finance", "region": "us-east", "environment": "prod"},
            "status": "ok"
        }]

        commands = mcd.generate_git_commands(actions, conf_d)
        assert len(commands) == 2
        assert commands[0][:2] == ["mkdir", "-p"]
        assert commands[1][:2] == ["git", "mv"]

    def test_generate_git_commands_empty(self, tmp_path):
        """No 'ok' actions -> empty commands."""
        conf_d = tmp_path / "conf.d"
        actions = [{
            "source": "tenant-a.yaml",
            "target": "tenant-a.yaml",
            "tenant_id": None,
            "metadata": None,
            "status": "skip_system_file"
        }]

        commands = mcd.generate_git_commands(actions, conf_d)
        assert commands == []

    def test_generate_git_commands_dedup_dirs(self, tmp_path):
        """Deduplicate target directories."""
        conf_d = tmp_path / "conf.d"
        actions = [
            {
                "source": "tenant-a.yaml",
                "target": "finance/us-east/prod/tenant-a.yaml",
                "tenant_id": "tenant-a",
                "metadata": {},
                "status": "ok"
            },
            {
                "source": "tenant-b.yaml",
                "target": "finance/us-east/prod/tenant-b.yaml",
                "tenant_id": "tenant-b",
                "metadata": {},
                "status": "ok"
            }
        ]

        commands = mcd.generate_git_commands(actions, conf_d)
        mkdir_count = sum(1 for c in commands if c[0] == "mkdir")
        mv_count = sum(1 for c in commands if c[0] == "git")
        assert mkdir_count == 1
        assert mv_count == 2


class TestCLIIntegration:
    """Tests for CLI behavior."""

    def test_cli_help(self):
        """--help exits 0."""
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, "-m", "migrate_conf_d", "--help"],
            cwd=_TOOLS_DIR,
            capture_output=True,
            text=True, encoding="utf-8",
        )
        assert result.returncode == 0

    def test_dry_run_cli(self, tmp_path):
        """--dry-run exit 0, shows commands but doesn't execute."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "tenant-a.yaml").write_text(yaml.dump({
            "tenants": {
                "tenant-a": {
                    "_metadata": {"domain": "finance"}
                }
            }
        }))

        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, "-m", "migrate_conf_d", "--conf-d", str(conf_d), "--dry-run"],
            cwd=_TOOLS_DIR,
            capture_output=True,
            text=True, encoding="utf-8",
        )
        assert result.returncode == 0
        assert "mkdir" in result.stdout or "Dry-run" in result.stdout

    def test_cli_missing_conf_d(self, tmp_path):
        """Missing conf.d -> error."""
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, "-m", "migrate_conf_d", "--conf-d", str(tmp_path / "nonexistent")],
            cwd=_TOOLS_DIR,
            capture_output=True,
            text=True, encoding="utf-8",
        )
        assert result.returncode != 0

    def test_cli_output_plan(self, tmp_path):
        """--output-plan writes JSON migration plan."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "tenant-a.yaml").write_text(yaml.dump({
            "tenants": {
                "tenant-a": {
                    "_metadata": {"domain": "finance"}
                }
            }
        }))

        plan_file = tmp_path / "plan.json"
        result = subprocess.run(  # subprocess-timeout: ignore
            [sys.executable, "-m", "migrate_conf_d",
             "--conf-d", str(conf_d),
             "--output-plan", str(plan_file)],
            cwd=_TOOLS_DIR,
            capture_output=True,
            text=True, encoding="utf-8",
        )
        assert result.returncode == 0
        assert plan_file.exists()
        plan = json.loads(plan_file.read_text())
        assert isinstance(plan, list) and len(plan) > 0


class TestUnplaceableMetadata:
    """#2830: a `_metadata` the exporter reads but this plan cannot place —
    the string form, or a non-string domain/region/environment — is listed
    for a manual move instead of crashing the whole plan."""

    @pytest.mark.parametrize("body, reason", [
        ('tenants:\n  t2:\n    _metadata: "domain: finance\\nenvironment: production\\n"\n',
         "_metadata is not a mapping (str)"),
        ("tenants:\n  t3:\n    _metadata:\n      domain: finance\n      environment: 123\n",
         "_metadata.environment is not a string (int)"),
        ("tenants:\n  t5:\n    _metadata:\n      domain: 7\n",
         "_metadata.domain is not a string (int)"),
    ])
    def test_listed_not_crashed(self, tmp_path, body, reason):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "t.yaml").write_text(body, encoding="utf-8")
        (conf_d / "t4.yaml").write_text(
            "tenants:\n  t4:\n    _metadata:\n      domain: finance\n", encoding="utf-8")
        by_source = {a["source"]: a for a in mcd.plan_migration(conf_d)}
        assert by_source["t.yaml"]["status"] == "skip_unplaceable_metadata"
        assert by_source["t.yaml"]["reason"] == reason
        assert by_source["t.yaml"]["target"] == "t.yaml"
        # The other file is still planned.
        assert by_source["t4.yaml"]["status"] == "ok"
        assert by_source["t4.yaml"]["target"] == "finance/t4.yaml"

    def test_cli_names_the_file(self, tmp_path):
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "t2.yaml").write_text(
            'tenants:\n  t2:\n    _metadata: "domain: finance\\n"\n', encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, os.path.join(_TOOLS_DIR, "migrate_conf_d.py"), "--conf-d", str(conf_d), "--dry-run"],
            capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert "Skip (unplaceable): 1" in proc.stdout
        assert "t2.yaml: _metadata is not a mapping (str) — move it by hand" in proc.stdout

    def test_an_empty_path_field_is_not_written(self, tmp_path):
        """`environment:` (YAML null) is left out of the path, as before
        #2830 — not reported as a non-string."""
        conf_d = tmp_path / "conf.d"
        conf_d.mkdir()
        (conf_d / "t1.yaml").write_text(
            "tenants:\n  t1:\n    _metadata:\n      domain: finance\n      region: us\n      environment:\n",
            encoding="utf-8")
        (action,) = mcd.plan_migration(conf_d)
        assert action["status"] == "ok"
        assert action["target"] == "finance/us/t1.yaml"
