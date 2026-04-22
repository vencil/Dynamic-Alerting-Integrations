#!/usr/bin/env python3
"""test_check_path_metadata_consistency — A-9 柔性一致性警告測試。

覆蓋：
  1. 路徑推斷正確（domain = 第一層；environment = allowlist 命中）
  2. _metadata 與 path 不符時發出警告（exit 0）
  3. _metadata 缺欄位時不警告
  4. `_*.yaml` 檔案被略過
  5. 扁平配置（無階層路徑）不警告
  6. 大小寫比對（prod == PROD）
  7. CI 模式輸出格式
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import check_path_metadata_consistency as cpmc  # noqa: E402


# ── helpers ────────────────────────────────────────────────────────────


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _tenant_yaml(
    tenant_id: str,
    *,
    domain: str | None = None,
    region: str | None = None,
    environment: str | None = None,
) -> str:
    meta_lines: list[str] = []
    if domain is not None:
        meta_lines.append(f"      domain: {domain}")
    if region is not None:
        meta_lines.append(f"      region: {region}")
    if environment is not None:
        meta_lines.append(f"      environment: {environment}")
    meta_block = ""
    if meta_lines:
        meta_block = "    _metadata:\n" + "\n".join(meta_lines) + "\n"
    return (
        "tenants:\n"
        f"  {tenant_id}:\n"
        f"{meta_block}"
        "    threshold:\n"
        "      cpu: 80\n"
    )


def _run_cli(
    repo_root: Path,
    *args: str,
) -> subprocess.CompletedProcess:
    script = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "tools"
        / "lint"
        / "check_path_metadata_consistency.py"
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


# ── TestPathInferences ─────────────────────────────────────────────────


class TestPathInferences:
    def test_first_segment_is_domain(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        inferred = cpmc._path_inferences(f, config_dir)
        assert inferred["domain"] == "db"

    def test_environment_from_allowlisted_segment(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        inferred = cpmc._path_inferences(f, config_dir)
        assert inferred["environment"] == "prod"

    def test_no_environment_inference_for_non_allowlist(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "t.yaml"
        inferred = cpmc._path_inferences(f, config_dir)
        assert "environment" not in inferred

    def test_flat_file_has_no_inferences(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        config_dir.mkdir(parents=True)
        f = config_dir / "flat.yaml"
        inferred = cpmc._path_inferences(f, config_dir)
        assert inferred == {}


# ── TestScanFile ───────────────────────────────────────────────────────


class TestScanFile:
    def test_warns_on_environment_mismatch(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        _write(f, _tenant_yaml("t-prod", environment="staging"))
        mismatches = cpmc.scan_file(f, config_dir)
        assert len(mismatches) == 1
        m = mismatches[0]
        assert m.field == "environment"
        assert m.path_value == "prod"
        assert m.metadata_value == "staging"

    def test_warns_on_domain_mismatch(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        _write(f, _tenant_yaml("t", domain="web", environment="prod"))
        mismatches = cpmc.scan_file(f, config_dir)
        # environment matches (prod == prod) → only domain mismatch.
        assert len(mismatches) == 1
        assert mismatches[0].field == "domain"

    def test_no_warning_when_aligned(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        _write(f, _tenant_yaml("t", domain="db", environment="prod"))
        assert cpmc.scan_file(f, config_dir) == []

    def test_no_warning_when_metadata_missing(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        _write(f, _tenant_yaml("t"))  # no _metadata at all
        assert cpmc.scan_file(f, config_dir) == []

    def test_case_insensitive_match(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "mariadb" / "prod" / "t.yaml"
        _write(f, _tenant_yaml("t", domain="DB", environment="PROD"))
        assert cpmc.scan_file(f, config_dir) == []

    def test_malformed_yaml_is_silent(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        f = config_dir / "db" / "prod" / "bad.yaml"
        _write(f, "tenants:\n  - not-a-map\n    x: [unclosed")
        assert cpmc.scan_file(f, config_dir) == []


# ── TestScan (full-directory) ──────────────────────────────────────────


class TestScan:
    def test_ignores_underscore_files(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        _write(
            config_dir / "db" / "prod" / "_defaults.yaml",
            "defaults:\n  _metadata:\n    environment: staging\n",
        )
        # _defaults.yaml would mismatch (path=prod, meta=staging), but
        # underscore-prefixed files must be skipped.
        assert cpmc.scan(config_dir) == []

    def test_multiple_files_aggregate(self, tmp_path):
        config_dir = tmp_path / "conf.d"
        _write(
            config_dir / "db" / "prod" / "a.yaml",
            _tenant_yaml("a", environment="staging"),
        )
        _write(
            config_dir / "db" / "staging" / "b.yaml",
            _tenant_yaml("b", environment="prod"),
        )
        # Third file: correctly aligned.
        _write(
            config_dir / "db" / "prod" / "c.yaml",
            _tenant_yaml("c", environment="prod"),
        )
        mismatches = cpmc.scan(config_dir)
        assert len(mismatches) == 2
        tenants = {m.tenant for m in mismatches}
        assert tenants == {"a", "b"}


# ── TestCLI ────────────────────────────────────────────────────────────


class TestCLI:
    def test_clean_dir_exit_zero(self, tmp_path):
        (tmp_path / ".git").mkdir()
        config_dir = tmp_path / "conf.d"
        _write(
            config_dir / "db" / "prod" / "t.yaml",
            _tenant_yaml("t", domain="db", environment="prod"),
        )
        result = _run_cli(tmp_path, "--config-dir", str(config_dir))
        assert result.returncode == 0
        assert "0 mismatch(es)" in result.stdout

    def test_warning_still_exits_zero(self, tmp_path):
        (tmp_path / ".git").mkdir()
        config_dir = tmp_path / "conf.d"
        _write(
            config_dir / "db" / "prod" / "t.yaml",
            _tenant_yaml("t", environment="staging"),
        )
        result = _run_cli(tmp_path, "--config-dir", str(config_dir))
        assert result.returncode == 0
        # warning is printed; summary tail goes to stderr when mismatches
        # exist.
        assert "WARN path/metadata mismatch" in result.stdout
        assert "1 mismatch(es)" in result.stderr

    def test_ci_mode_single_line_format(self, tmp_path):
        (tmp_path / ".git").mkdir()
        config_dir = tmp_path / "conf.d"
        _write(
            config_dir / "db" / "prod" / "t.yaml",
            _tenant_yaml("t", environment="staging"),
        )
        result = _run_cli(
            tmp_path, "--config-dir", str(config_dir), "--ci"
        )
        assert result.returncode == 0
        # format: "<file>:0: warning: path/metadata mismatch tenant=..."
        assert ":0: warning: path/metadata mismatch" in result.stdout
        assert "tenant=t" in result.stdout
        assert "field=environment" in result.stdout

    def test_missing_config_dir_is_soft(self, tmp_path):
        (tmp_path / ".git").mkdir()
        result = _run_cli(
            tmp_path, "--config-dir", str(tmp_path / "does-not-exist")
        )
        assert result.returncode == 0
        assert "config dir not found" in result.stderr
