"""Pipeline integration test: init → scaffold → validate-config → diagnose.

Tests the end-to-end tool pipeline using temporary directories and mocked
Prometheus responses where needed.
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TOOLS_DIR = Path(__file__).parent.parent.parent / "scripts" / "tools"


def _utf8_env():
    """Force UTF-8 stderr/stdout in the child process.

    Without this, Python on Windows defaults stderr to the console code
    page (CP950 / CP932 / CP1252 depending on locale). When the child
    emits Chinese strings (which scaffold/validate do for human-readable
    diagnostics), the bytes can't be decoded as UTF-8 in the test, causing
    UnicodeDecodeError. Setting PYTHONIOENCODING is the canonical fix.
    """
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


@pytest.fixture
def work_dir(tmp_path):
    """Create a temporary working directory."""
    return tmp_path


class TestPipelineIntegration:
    """Integration test for the core tool pipeline."""

    def test_scaffold_produces_valid_yaml(self, work_dir):
        """scaffold → creates valid tenant YAML in conf.d/."""
        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "ops" / "scaffold_tenant.py"),
             "--tenant", "test-db", "--db", "mariadb",
             "--non-interactive", "--output-dir", str(work_dir)],
            capture_output=True, timeout=30, cwd=str(work_dir),
            env=_utf8_env(), text=True, encoding="utf-8", errors="replace",
        )
        # scaffold may exit 0 or print to stdout
        assert result.returncode == 0, (
            f"scaffold failed with exit code {result.returncode}\n"
            f"stderr: {result.stderr[:300]}"
        )
        # Check that a YAML file was created
        yaml_files = list(work_dir.glob("**/*.yaml")) + list(work_dir.glob("**/*.yml"))
        assert len(yaml_files) > 0, (
            f"scaffold produced no YAML files. Files in {work_dir}: "
            f"{list(work_dir.glob('*'))}"
        )

    def test_validate_config_on_sample(self, work_dir):
        """validate-config → should handle empty/sample configs."""
        # Create a minimal conf.d
        conf_dir = work_dir / "conf.d"
        conf_dir.mkdir()
        defaults = conf_dir / "_defaults.yaml"
        defaults.write_text("defaults:\n  mysql_connections: 80\n", encoding="utf-8")
        tenant = conf_dir / "test-db.yaml"
        tenant.write_text("tenants:\n  test-db:\n    mysql_connections: '70'\n", encoding="utf-8")

        result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "ops" / "validate_config.py"),
             "--config-dir", str(conf_dir)],
            capture_output=True, timeout=30,
            env=_utf8_env(), text=True, encoding="utf-8", errors="replace",
        )
        # validate-config should succeed on valid minimal config
        assert result.returncode == 0, (
            f"validate-config failed with exit code {result.returncode}\n"
            f"stderr: {result.stderr[:300]}"
        )

    def test_scaffold_then_validate_pipeline(self, work_dir):
        """End-to-end: scaffold → validate-config."""
        # Step 1: Scaffold
        scaffold_result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "ops" / "scaffold_tenant.py"),
             "--tenant", "pipeline-test-db", "--db", "mariadb",
             "--non-interactive", "--output-dir", str(work_dir)],
            capture_output=True, timeout=30, cwd=str(work_dir),
            env=_utf8_env(), text=True, encoding="utf-8", errors="replace",
        )
        assert scaffold_result.returncode == 0, (
            f"scaffold failed: {scaffold_result.stderr[:300]}"
        )

        # Find the generated config directory
        yaml_files = list(work_dir.glob("**/*.yaml")) + list(work_dir.glob("**/*.yml"))
        assert len(yaml_files) > 0, "scaffold produced no YAML files"

        # Step 2: Validate the scaffolded config
        config_dir = yaml_files[0].parent
        validate_result = subprocess.run(
            [sys.executable, str(TOOLS_DIR / "ops" / "validate_config.py"),
             "--config-dir", str(config_dir)],
            capture_output=True, timeout=30,
            env=_utf8_env(), text=True, encoding="utf-8", errors="replace",
        )
        assert validate_result.returncode == 0, (
            f"validate-config failed on scaffolded config: "
            f"{validate_result.stderr[:300]}"
        )
