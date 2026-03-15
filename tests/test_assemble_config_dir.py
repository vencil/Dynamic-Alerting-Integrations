#!/usr/bin/env python3
"""test_assemble_config_dir.py — assemble_config_dir.py 測試。

pytest style：使用 plain assert + conftest fixtures。

驗證:
  1. discover_yamls() — YAML 檔案探索
  2. _file_sha256() — 確定性 hash 計算
  3. detect_conflicts() — 衝突偵測（含 platform files）
  4. assemble() — 檔案組裝（含 dry-run）
  5. validate_merged() — YAML 驗證（含邊界情況）
  6. build_manifest() — 組裝清單產生
"""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from assemble_config_dir import (
    PLATFORM_FILES,
    _file_sha256,
    assemble,
    build_manifest,
    detect_conflicts,
    discover_yamls,
    main,
    validate_merged,
)


# ── Helpers ───────────────────────────────────────────────────

def _write_file(path, content="a: 1"):
    """寫入檔案並設定安全權限。"""
    p = Path(path)
    p.write_text(content, encoding="utf-8")
    os.chmod(p, 0o600)
    return p


# ============================================================
# discover_yamls
# ============================================================

class TestDiscoverYamls:
    """discover_yamls() YAML 檔案探索。"""

    def test_finds_yaml_files(self, config_dir):
        """正確發現 .yaml 檔案。"""
        _write_file(os.path.join(config_dir, "a.yaml"))
        _write_file(os.path.join(config_dir, "b.yaml"), "b: 2")
        _write_file(os.path.join(config_dir, "c.txt"), "not yaml")
        result = discover_yamls(Path(config_dir))
        assert len(result) == 2
        assert all(f.suffix == ".yaml" for f in result)

    def test_missing_dir_raises(self):
        """不存在的目錄 raise FileNotFoundError。"""
        with pytest.raises(FileNotFoundError):
            discover_yamls(Path("/nonexistent/dir"))

    def test_empty_dir(self, config_dir):
        """空目錄回傳空清單。"""
        assert discover_yamls(Path(config_dir)) == []

    def test_sorted_order(self, config_dir):
        """結果按字母排序。"""
        _write_file(os.path.join(config_dir, "c.yaml"))
        _write_file(os.path.join(config_dir, "a.yaml"))
        _write_file(os.path.join(config_dir, "b.yaml"))
        result = discover_yamls(Path(config_dir))
        names = [f.name for f in result]
        assert names == ["a.yaml", "b.yaml", "c.yaml"]


# ============================================================
# _file_sha256
# ============================================================

class TestFileSha256:
    """_file_sha256() 確定性 hash 計算。"""

    def test_deterministic(self, config_dir):
        """同一檔案多次計算 hash 結果相同。"""
        p = _write_file(os.path.join(config_dir, "test.yaml"), "test content")
        h1 = _file_sha256(p)
        h2 = _file_sha256(p)
        assert h1 == h2
        assert len(h1) == 64

    def test_different_content_different_hash(self, config_dir):
        """不同內容產生不同 hash。"""
        p1 = _write_file(os.path.join(config_dir, "a.yaml"), "a: 1")
        p2 = _write_file(os.path.join(config_dir, "b.yaml"), "b: 2")
        assert _file_sha256(p1) != _file_sha256(p2)


# ============================================================
# detect_conflicts
# ============================================================

class TestDetectConflicts:
    """detect_conflicts() 衝突偵測。"""

    def test_no_conflicts(self, config_dir):
        """不同檔名無衝突。"""
        src_a = Path(config_dir) / "team-a"
        src_b = Path(config_dir) / "team-b"
        src_a.mkdir()
        src_b.mkdir()
        _write_file(src_a / "db-a.yaml")
        _write_file(src_b / "db-b.yaml", "b: 2")
        conflicts, file_map = detect_conflicts([src_a, src_b])
        assert len(conflicts) == 0
        assert len(file_map) == 2

    def test_real_conflict(self, config_dir):
        """同名不同內容檔案為真實衝突。"""
        src_a = Path(config_dir) / "team-a"
        src_b = Path(config_dir) / "team-b"
        src_a.mkdir()
        src_b.mkdir()
        _write_file(src_a / "db-a.yaml", "a: 1")
        _write_file(src_b / "db-a.yaml", "a: 99")
        conflicts, file_map = detect_conflicts([src_a, src_b])
        assert "db-a.yaml" in conflicts
        assert "db-a.yaml" not in file_map

    def test_identical_not_conflict(self, config_dir):
        """同名同內容檔案非衝突。"""
        src_a = Path(config_dir) / "team-a"
        src_b = Path(config_dir) / "team-b"
        src_a.mkdir()
        src_b.mkdir()
        content = "tenants:\n  db-a:\n    x: '1'"
        _write_file(src_a / "db-a.yaml", content)
        _write_file(src_b / "db-a.yaml", content)
        conflicts, file_map = detect_conflicts([src_a, src_b])
        assert len(conflicts) == 0
        assert "db-a.yaml" in file_map

    def test_platform_file_first_wins(self, config_dir):
        """Platform 檔案重複時第一個 source 優先。"""
        src_a = Path(config_dir) / "team-a"
        src_b = Path(config_dir) / "team-b"
        src_a.mkdir()
        src_b.mkdir()
        _write_file(src_a / "_defaults.yaml", "d: 1")
        _write_file(src_b / "_defaults.yaml", "d: 2")
        conflicts, file_map = detect_conflicts([src_a, src_b])
        assert "_defaults.yaml" in conflicts
        assert "_defaults.yaml" in file_map
        assert file_map["_defaults.yaml"] == src_a / "_defaults.yaml"

    def test_empty_sources(self):
        """空 sources 清單回傳空結果。"""
        conflicts, file_map = detect_conflicts([])
        assert conflicts == {}
        assert file_map == {}

    def test_three_way_conflict(self, config_dir):
        """三方衝突全部報告。"""
        dirs = []
        for name in ["team-a", "team-b", "team-c"]:
            d = Path(config_dir) / name
            d.mkdir()
            _write_file(d / "db-x.yaml", f"val: {name}")
            dirs.append(d)
        conflicts, file_map = detect_conflicts(dirs)
        assert "db-x.yaml" in conflicts
        assert len(conflicts["db-x.yaml"]) == 3


# ============================================================
# assemble
# ============================================================

class TestAssemble:
    """assemble() 檔案組裝。"""

    def test_copies_files(self, config_dir):
        """正確複製檔案到輸出目錄。"""
        src = Path(config_dir) / "src"
        out = Path(config_dir) / "out"
        src.mkdir()
        _write_file(src / "a.yaml")
        file_map = {"a.yaml": src / "a.yaml"}
        count = assemble(file_map, out)
        assert count == 1
        assert (out / "a.yaml").exists()
        assert (out / "a.yaml").read_text(encoding="utf-8") == "a: 1"

    def test_dry_run_does_not_write(self, config_dir):
        """dry-run 模式不寫入檔案。"""
        src = Path(config_dir) / "src"
        src.mkdir()
        _write_file(src / "a.yaml")
        out = Path(config_dir) / "nonexistent-output"
        file_map = {"a.yaml": src / "a.yaml"}
        count = assemble(file_map, out, dry_run=True)
        assert count == 1
        assert not out.exists()

    def test_empty_file_map(self, config_dir):
        """空 file_map 不複製任何檔案。"""
        out = Path(config_dir) / "out"
        count = assemble({}, out)
        assert count == 0

    def test_file_permissions(self, config_dir):
        """組裝後檔案權限正確（owner rw, group r, other r）。"""
        src = Path(config_dir) / "src"
        out = Path(config_dir) / "out"
        src.mkdir()
        _write_file(src / "a.yaml")
        assemble({"a.yaml": src / "a.yaml"}, out)
        mode = os.stat(out / "a.yaml").st_mode & 0o777
        expected = stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH
        assert mode == expected


# ============================================================
# validate_merged
# ============================================================

class TestValidateMerged:
    """validate_merged() YAML 驗證。"""

    def test_valid_yaml(self, config_dir):
        """有效 YAML 無 issue。"""
        _write_file(os.path.join(config_dir, "a.yaml"),
                     "tenants:\n  db-a:\n    x: '1'")
        issues = validate_merged(Path(config_dir))
        assert issues == []

    def test_invalid_yaml(self, config_dir):
        """無效 YAML 報告 parse error。"""
        _write_file(os.path.join(config_dir, "bad.yaml"),
                     ":\n  - [invalid")
        issues = validate_merged(Path(config_dir))
        assert any("parse error" in i for i in issues)

    def test_empty_yaml(self, config_dir):
        """空 YAML 報告 empty 警告。"""
        _write_file(os.path.join(config_dir, "empty.yaml"), "")
        issues = validate_merged(Path(config_dir))
        assert any("empty" in i for i in issues)

    def test_non_dict_yaml(self, config_dir):
        """頂層非 dict 的 YAML 報告 ERROR。"""
        _write_file(os.path.join(config_dir, "list.yaml"), "- item1\n- item2\n")
        issues = validate_merged(Path(config_dir))
        assert any("not a mapping" in i for i in issues)

    def test_no_yaml_files(self, config_dir):
        """無 YAML 檔案回傳空清單。"""
        issues = validate_merged(Path(config_dir))
        assert issues == []


# ============================================================
# build_manifest
# ============================================================

class TestBuildManifest:
    """build_manifest() 組裝清單產生。"""

    def test_basic_manifest(self, config_dir):
        """基本清單結構正確。"""
        p = _write_file(os.path.join(config_dir, "a.yaml"))
        file_map = {"a.yaml": p}
        manifest = build_manifest([Path(config_dir)], file_map, {})
        assert manifest["file_count"] == 1
        assert "a.yaml" in manifest["files"]
        assert len(manifest["files"]["a.yaml"]["sha256"]) == 64

    def test_manifest_with_conflicts(self, config_dir):
        """清單正確記錄衝突資訊。"""
        p = _write_file(os.path.join(config_dir, "a.yaml"))
        file_map = {"a.yaml": p}
        conflicts = {
            "_defaults.yaml": [
                ("team-a", Path("/tmp/team-a/_defaults.yaml")),
                ("team-b", Path("/tmp/team-b/_defaults.yaml")),
            ]
        }
        manifest = build_manifest([Path(config_dir)], file_map, conflicts)
        assert "_defaults.yaml" in manifest["conflicts"]
        assert len(manifest["conflicts"]["_defaults.yaml"]) == 2

    def test_manifest_empty_file_map(self, config_dir):
        """空 file_map 清單 file_count=0。"""
        manifest = build_manifest([Path(config_dir)], {}, {})
        assert manifest["file_count"] == 0
        assert manifest["files"] == {}

    def test_manifest_sources_list(self, config_dir):
        """清單正確記錄 sources 路徑。"""
        src_a = Path(config_dir) / "a"
        src_b = Path(config_dir) / "b"
        manifest = build_manifest([src_a, src_b], {}, {})
        assert len(manifest["sources"]) == 2


# ============================================================
# main() CLI integration
# ============================================================

class TestMainCLI:
    """main() CLI 整合測試。"""

    def _setup_sources(self, config_dir):
        """建立兩個 source 目錄，各含一個 tenant YAML。"""
        src_a = Path(config_dir) / "team-a"
        src_b = Path(config_dir) / "team-b"
        src_a.mkdir()
        src_b.mkdir()
        _write_file(src_a / "db-a.yaml", "tenants:\n  db-a:\n    x: '1'")
        _write_file(src_b / "db-b.yaml", "tenants:\n  db-b:\n    y: '2'")
        return f"{src_a},{src_b}"

    def test_check_mode_no_conflicts(self, config_dir, monkeypatch):
        """--check 模式無衝突回傳 0。"""
        sources = self._setup_sources(config_dir)
        monkeypatch.setattr(sys, "argv",
                            ["assemble", "--sources", sources, "--check"])
        assert main() == 0

    def test_check_mode_json(self, config_dir, monkeypatch, capsys):
        """--check --json 輸出 JSON 格式。"""
        sources = self._setup_sources(config_dir)
        monkeypatch.setattr(sys, "argv",
                            ["assemble", "--sources", sources, "--check", "--json"])
        assert main() == 0
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["file_count"] == 2

    def test_assemble_mode(self, config_dir, monkeypatch):
        """組裝模式正確複製檔案。"""
        sources = self._setup_sources(config_dir)
        out = Path(config_dir) / "output"
        monkeypatch.setattr(sys, "argv",
                            ["assemble", "--sources", sources, "--output", str(out)])
        assert main() == 0
        assert (out / "db-a.yaml").exists()
        assert (out / "db-b.yaml").exists()

    def test_assemble_with_manifest(self, config_dir, monkeypatch):
        """組裝模式產生 manifest JSON。"""
        sources = self._setup_sources(config_dir)
        out = Path(config_dir) / "output"
        manifest_path = Path(config_dir) / "manifest.json"
        monkeypatch.setattr(sys, "argv",
                            ["assemble", "--sources", sources,
                             "--output", str(out), "--manifest", str(manifest_path)])
        assert main() == 0
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["file_count"] == 2

    def test_conflict_returns_1(self, config_dir, monkeypatch):
        """檔案衝突回傳 exit code 1。"""
        src_a = Path(config_dir) / "team-a"
        src_b = Path(config_dir) / "team-b"
        src_a.mkdir()
        src_b.mkdir()
        _write_file(src_a / "db-a.yaml", "a: 1")
        _write_file(src_b / "db-a.yaml", "a: 99")
        monkeypatch.setattr(sys, "argv",
                            ["assemble", "--sources", f"{src_a},{src_b}", "--check"])
        assert main() == 1

    def test_conflict_json_output(self, config_dir, monkeypatch, capsys):
        """衝突時 --json 輸出衝突詳情。"""
        src_a = Path(config_dir) / "team-a"
        src_b = Path(config_dir) / "team-b"
        src_a.mkdir()
        src_b.mkdir()
        _write_file(src_a / "db-a.yaml", "a: 1")
        _write_file(src_b / "db-a.yaml", "a: 99")
        monkeypatch.setattr(sys, "argv",
                            ["assemble", "--sources", f"{src_a},{src_b}",
                             "--check", "--json"])
        assert main() == 1
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "conflict"
        assert "db-a.yaml" in output["conflicts"]

    def test_missing_source_returns_2(self, config_dir, monkeypatch):
        """不存在的 source 目錄回傳 exit code 2。"""
        monkeypatch.setattr(sys, "argv",
                            ["assemble", "--sources", "/nonexistent/dir", "--check"])
        assert main() == 2

    def test_assemble_json_output(self, config_dir, monkeypatch, capsys):
        """組裝模式 --json 輸出結構化結果。"""
        sources = self._setup_sources(config_dir)
        out = Path(config_dir) / "output"
        monkeypatch.setattr(sys, "argv",
                            ["assemble", "--sources", sources,
                             "--output", str(out), "--json"])
        assert main() == 0
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["file_count"] == 2

    def test_assemble_with_validate(self, config_dir, monkeypatch):
        """組裝模式含 --validate 檢查 YAML 有效性。"""
        sources = self._setup_sources(config_dir)
        out = Path(config_dir) / "output"
        monkeypatch.setattr(sys, "argv",
                            ["assemble", "--sources", sources,
                             "--output", str(out), "--validate"])
        assert main() == 0
