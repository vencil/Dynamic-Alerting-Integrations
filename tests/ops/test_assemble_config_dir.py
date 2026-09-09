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
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "tools"))
import _lib_tenant_uniqueness as tu  # noqa: E402

from assemble_config_dir import (
    PLATFORM_FILES,
    _file_sha256,
    assemble,
    build_manifest,
    detect_conflicts,
    discover_yamls,
    has_errors,
    is_platform_file,
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

    @pytest.mark.parametrize("carrier", ["alpha.yml", "Alpha.YAML"])
    def test_sees_yml_and_upper_case_the_same_as_yaml(self, tmp_path, carrier):
        """#1603 — a `.yml` / `.YAML` shard file is discovered like `.yaml`.

        WHY: the exporter reads both spellings, so a shard file this tool
        does not discover is a tenant silently missing from the assembled
        config-dir. Measured before the fix: `alpha.yaml` discovered,
        `alpha.yml` / `Alpha.YAML` not — same answer as an empty shard.
        The `none` arm is the sensitivity control.
        """
        seen = {}
        for arm, name in (("yaml", "alpha.yaml"), ("other", carrier),
                          ("none", None)):
            d = tmp_path / arm
            d.mkdir()
            _write_file(d / "_defaults.yaml", "defaults: {}")
            if name:
                _write_file(d / name, "tenants:\n  alpha:\n    x: 1\n")
            seen[arm] = [p.name for p in discover_yamls(d)]
        assert seen["yaml"] != seen["none"], "fixture is vacuous"
        assert len(seen["other"]) == len(seen["yaml"]) and carrier in seen["other"], (
            f"{carrier} was not discovered: {seen['other']}")

    def test_yml_shard_is_merged_and_can_conflict(self, tmp_path):
        """Blast radius of #1603 here: a `.yml` shard now REACHES the merge.

        Before: `alpha.yml` in shard 2 was dropped from `file_map` without
        a word. After: it is merged, and two `.yml` shards with the same
        name and different bodies are a conflict like their `.yaml` twins.
        """
        s1, s2 = tmp_path / "s1", tmp_path / "s2"
        s1.mkdir()
        s2.mkdir()
        _write_file(s1 / "beta.yaml", "tenants:\n  beta:\n    x: 1\n")
        _write_file(s2 / "alpha.yml", "tenants:\n  alpha:\n    x: 1\n")
        conflicts, fmap = detect_conflicts([s1, s2])
        assert "alpha.yml" in fmap and conflicts == {}
        _write_file(s1 / "alpha.yml", "tenants:\n  alpha:\n    x: 2\n")
        conflicts, fmap = detect_conflicts([s1, s2])
        assert "alpha.yml" in conflicts and "alpha.yml" not in fmap


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

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="Windows file ACL semantics don't match Unix mode bits "
               "(group/other distinction collapses to 0o666). The "
               "production code's chmod call is a no-op on Windows; "
               "this test asserts Unix-specific semantics.",
    )
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

    @pytest.mark.parametrize("carrier", ["bad.yml", "BAD.YAML"])
    def test_validates_yml_and_upper_case_the_same_as_yaml(self, config_dir, carrier):
        """#1603 — the file `discover_yamls` now copies must also be the one
        validated; a broken `.yml` in the output is a parse error like a
        broken `.yaml`. Control: the same body under `.txt` is not a YAML
        carrier and produces no issue.
        """
        _write_file(os.path.join(config_dir, carrier), ":\n  - [invalid")
        _write_file(os.path.join(config_dir, "notes.txt"), ":\n  - [invalid")
        issues = validate_merged(Path(config_dir))
        assert [i for i in issues if carrier in i and "parse error" in i], issues
        assert not [i for i in issues if "notes.txt" in i], issues


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

    def test_check_mode_no_conflicts(self, config_dir, monkeypatch, cli_argv):
        """--check 模式無衝突回傳 0。"""
        sources = self._setup_sources(config_dir)
        cli_argv("assemble", "--sources", sources, "--check")
        assert main() == 0

    def test_check_mode_json(self, config_dir, monkeypatch, capsys, cli_argv):
        """--check --json 輸出 JSON 格式。"""
        sources = self._setup_sources(config_dir)
        cli_argv("assemble", "--sources", sources, "--check", "--json")
        assert main() == 0
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["file_count"] == 2

    def test_assemble_mode(self, config_dir, monkeypatch, cli_argv):
        """組裝模式正確複製檔案。"""
        sources = self._setup_sources(config_dir)
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", sources, "--output", str(out))
        assert main() == 0
        assert (out / "db-a.yaml").exists()
        assert (out / "db-b.yaml").exists()

    def test_assemble_with_manifest(self, config_dir, monkeypatch, cli_argv):
        """組裝模式產生 manifest JSON。"""
        sources = self._setup_sources(config_dir)
        out = Path(config_dir) / "output"
        manifest_path = Path(config_dir) / "manifest.json"
        cli_argv("assemble", "--sources", sources, "--output", str(out), "--manifest", str(manifest_path))
        assert main() == 0
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["file_count"] == 2

    def test_conflict_returns_1(self, config_dir, monkeypatch, cli_argv):
        """檔案衝突回傳 exit code 1。"""
        src_a = Path(config_dir) / "team-a"
        src_b = Path(config_dir) / "team-b"
        src_a.mkdir()
        src_b.mkdir()
        _write_file(src_a / "db-a.yaml", "a: 1")
        _write_file(src_b / "db-a.yaml", "a: 99")
        cli_argv("assemble", "--sources", f"{src_a},{src_b}", "--check")
        assert main() == 1

    def test_conflict_json_output(self, config_dir, monkeypatch, capsys, cli_argv):
        """衝突時 --json 輸出衝突詳情。"""
        src_a = Path(config_dir) / "team-a"
        src_b = Path(config_dir) / "team-b"
        src_a.mkdir()
        src_b.mkdir()
        _write_file(src_a / "db-a.yaml", "a: 1")
        _write_file(src_b / "db-a.yaml", "a: 99")
        cli_argv("assemble", "--sources", f"{src_a},{src_b}", "--check", "--json")
        assert main() == 1
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "conflict"
        assert "db-a.yaml" in output["conflicts"]

    def test_missing_source_returns_2(self, config_dir, monkeypatch, cli_argv):
        """不存在的 source 目錄回傳 exit code 2。"""
        cli_argv('assemble', '--sources', '/nonexistent/dir', '--check')
        assert main() == 2

    def test_assemble_json_output(self, config_dir, monkeypatch, capsys, cli_argv):
        """組裝模式 --json 輸出結構化結果。"""
        sources = self._setup_sources(config_dir)
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", sources, "--output", str(out), "--json")
        assert main() == 0
        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["file_count"] == 2

    def test_assemble_with_validate(self, config_dir, monkeypatch, cli_argv):
        """組裝模式含 --validate 檢查 YAML 有效性。"""
        sources = self._setup_sources(config_dir)
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", sources, "--output", str(out), "--validate")
        assert main() == 0


# ============================================================
# #1794 — the question the filename key cannot ask
# ============================================================

_TENANT_DOC = "tenants:\n  {t}:\n    mysql_connections: {v}\n"


def _shard(root, name, *files):
    """Build a source dir; `files` are (filename, tenant_id, value) triples."""
    d = Path(root) / name
    d.mkdir(parents=True, exist_ok=True)
    for fn, tenant, value in files:
        _write_file(d / fn, _TENANT_DOC.format(t=tenant, v=value))
    return d


class TestDuplicateTenantRefusal:
    """⛔ The exporter rejects on "one tenant id, two files"; the assembler
    grouped by filename, so it could build that artifact and exit 0.

    `config_hierarchy.go` compares ids and never looks at a name, so the two
    arms below are the same defect wearing different filenames. Both were
    measured on `origin/main` before the fix: rc 0, both carriers written, and
    `validate_config --config-dir <output>` answering `tenant_uniqueness:
    fail` on the very same directory.
    """

    def test_two_spellings_of_one_stem_are_refused(self, config_dir, cli_argv):
        s = _shard(config_dir, "team-a",
                   ("db-a.yaml", "db-a", 50), ("db-a.yml", "db-a", 90))
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 1
        assert not out.exists(), "a rejected tree was written anyway"

    def test_two_stems_declaring_one_tenant_are_refused(self, config_dir, cli_argv):
        """The arm a stem-keyed grouping is structurally blind to.

        `db-a.yaml` + `legacy-db-a.yaml` share no stem — and share the same
        `.yaml` spelling, so the extension axis is not even in play — yet they
        reach the identical whole-directory rejection. A fix that grouped by
        stem would leave this exactly as it was.
        """
        s = _shard(config_dir, "team-a",
                   ("db-a.yaml", "db-a", 50), ("legacy-db-a.yaml", "db-a", 90))
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 1
        assert not out.exists()

    def test_one_tenant_split_across_two_sources_is_refused(self, config_dir, cli_argv):
        """The module's exit table has always read `1 conflict detected (same
        tenant in multiple sources)`. Until #1794 the filename key implemented
        it only where both carriers happen to share a name (measured on the
        parent commit: two sources, both `db-a.yaml`, rc 1). The moment they
        do not — this test — nothing asked the question."""
        a = _shard(config_dir, "team-a", ("db-a.yaml", "db-a", 50))
        b = _shard(config_dir, "team-b", ("db-a.yml", "db-a", 90))
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", f"{a},{b}", "--output", str(out))
        assert main() == 1

    def test_two_spellings_declaring_different_tenants_still_assemble(
            self, config_dir, cli_argv):
        """CONTROL — must stay quiet.

        The exporter accepts this tree (measured), so a refusal here would be
        a fix that simply rejects any directory holding both spellings. It
        also keeps the refusals above from being satisfiable by the extension
        axis, which is a different question (#1603).
        """
        s = _shard(config_dir, "team-a",
                   ("db-a.yaml", "db-a", 50), ("db-a.yml", "db-b", 90))
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 0
        assert (out / "db-a.yaml").exists() and (out / "db-a.yml").exists()

    def test_check_mode_asks_the_same_question(self, config_dir, cli_argv):
        """`sharded-check` is the dry run of `sharded-assemble`. If the check
        answers a narrower question than the write path, its green is the
        thing that gets trusted."""
        s = _shard(config_dir, "team-a",
                   ("db-a.yaml", "db-a", 50), ("db-a.yml", "db-a", 90))
        cli_argv("assemble", "--sources", str(s), "--check")
        assert main() == 1

    def test_json_refusal_is_one_document_and_names_the_check(
            self, config_dir, capsys, cli_argv):
        s = _shard(config_dir, "team-a",
                   ("db-a.yaml", "db-a", 50), ("db-a.yml", "db-a", 90))
        cli_argv("assemble", "--sources", str(s), "--check", "--json")
        assert main() == 1
        payload = json.loads(capsys.readouterr().out)   # one document or this raises
        # The wire value is pinned as a literal (consumers read it); the
        # check NAME comes from the helper, because a rename there has to
        # travel to whatever this producer prints.
        assert payload["status"] == "duplicate"
        assert payload["check"] == tu.CHECK_NAME
        assert tu.EXIT_FOR[tu.UNMEASURED] == 2, (
            "both producers map could-not-measure to caller error")
        assert any("db-a" in line for line in payload["details"])

    def test_cannot_measure_is_not_read_as_clean(self, config_dir, cli_argv,
                                                 monkeypatch):
        """A validator that cannot be parsed is not a validator that passed.

        Patches `subprocess.run` on the stdlib module — the object
        `_lib_tenant_uniqueness` calls — so the arm under test is the real
        classification path, not a stubbed verdict.
        """
        s = _shard(config_dir, "team-a", ("db-a.yaml", "db-a", 50))
        out = Path(config_dir) / "output"

        class _R:
            returncode = 0
            stdout = "not json at all"
            stderr = ""

        monkeypatch.setattr(subprocess, "run", lambda *a, **k: _R())
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 2
        assert not out.exists()


class TestValidateCanFail:
    """`--validate` printed `ERROR: ...` and returned 0 for every input.

    Measured on `origin/main`: a malformed carrier and a non-mapping top level
    both printed their ERROR line under `Validation (1 issue(s))` and exited
    0, with a clean tree exiting 0 as well — the flag had no input that made
    it non-zero, while the module's exit table promised `2 validation error`.
    """

    def test_an_unparseable_carrier_refuses_even_without_validate(
            self, config_dir, cli_argv):
        """Deliberate, and a consequence of the duplicate-tenant gate rather
        than of `--validate`: a carrier the checker cannot open is where a
        second declaration hides, so `tenant_uniqueness` answers `warn` and
        this tool refuses instead of writing a tree it could not vet. The
        sibling producer's gate already behaves this way (#1783).
        """
        s = _shard(config_dir, "team-a", ("db-a.yaml", "db-a", 50))
        _write_file(s / "broken.yaml", "tenants:\n  - [unclosed\n")
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 2
        assert not out.exists()

    def test_validate_still_covers_what_the_gate_cannot_see(
            self, config_dir, cli_argv):
        """⛔ The two are NOT redundant, and this pair is the proof.

        A non-mapping top level parses fine, so `check_tenant_uniqueness`
        skips it and answers `pass` — measured: rc 0, carrier written. Only
        `--validate` reports it. Without this pair, deleting `--validate`
        would look free.
        """
        s = _shard(config_dir, "team-a", ("db-a.yaml", "db-a", 50))
        _write_file(s / "scalar.yaml", "just-a-string\n")
        out = Path(config_dir) / "output"

        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 0, "the gate is blind to this shape; that is the point"

        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--validate")
        assert main() == 2

    def test_a_warn_only_issue_does_not_fail(self, config_dir, cli_argv):
        """CONTROL — an empty carrier is a placeholder, not a broken one."""
        s = _shard(config_dir, "team-a", ("db-a.yaml", "db-a", 50))
        _write_file(s / "placeholder.yaml", "")
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--validate")
        assert main() == 0

    def test_has_errors_separates_the_two_severities(self):
        assert has_errors(["ERROR: x.yaml parse error: boom"])
        assert not has_errors(["WARN: x.yaml is empty"])
        assert not has_errors([])
        assert has_errors(["WARN: x.yaml is empty",
                           "ERROR: y.yaml top-level is not a mapping"])


class TestPlatformFileSpelling:
    """`PLATFORM_FILES` was two literal `.yaml` names, so the other spellings
    of the same platform ROLE fell through to the tenant path — measured on
    `origin/main`: two sources each holding `_defaults.yml` printed
    `❌ 1 tenant conflict(s)` and exited 1, where the `.yaml` pair printed the
    first-source-wins warning and exited 0.
    """

    @pytest.mark.parametrize("name", [
        "_defaults.yaml", "_defaults.yml", "_defaults.YAML", "_DEFAULTS.yml",
        "_profiles.yaml", "_profiles.yml", "_Profiles.YAML",
    ])
    def test_every_spelling_of_a_platform_role_is_one(self, name):
        assert is_platform_file(name)

    @pytest.mark.parametrize("name", [
        "db-a.yaml", "db-a.yml",
        # ⛔ EXACT, not a prefix: the exporter merges neither of these into a
        # chain, and `_lib_confd.is_defaults_name` says so for the same reason.
        "_defaults-multidb.yaml", "_profiles-extra.yaml",
        # Other `_`-prefixed control files keep the tenant-path policy
        # (refuse on divergence) deliberately: first-source-wins would drop a
        # second shard's routing profiles without a word.
        "_routing_profiles.yaml", "_instance_mapping.yaml",
        "_defaults.json", "_defaults",
    ])
    def test_other_names_are_not_platform_files(self, name):
        assert not is_platform_file(name)

    def test_the_defaults_half_agrees_with_the_shared_oracle(self):
        """⛔ Two predicates, one fact — pin them together instead of hoping.

        `_lib_confd.is_defaults_name` answers to `config_hierarchy.go`. Every
        name it calls a defaults carrier has to be a platform file here too,
        or this module has quietly grown a second opinion about which file
        the exporter merges into a chain. Widening
        `_lib_confd.is_defaults_name` to a prefix match fails this and nothing
        else in the file. ⚠️ Dropping
        `_defaults` from `PLATFORM_FILES` also fails it, but takes the
        parametrized cases above down with it, so that mutation does not
        isolate this pin.
        """
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                               / "scripts" / "tools"))
        from _lib_confd import is_defaults_name

        names = ["_defaults.yaml", "_defaults.yml", "_DEFAULTS.YAML",
                 "_defaults-multidb.yaml", "db-a.yaml", "_profiles.yaml"]
        shared = {n for n in names if is_defaults_name(n)}
        assert shared, "fixture is vacuous — the oracle matched nothing"
        assert shared <= {n for n in names if is_platform_file(n)}
        assert PLATFORM_FILES, "the derived name set must not be empty"

    def test_a_yml_defaults_pair_takes_the_platform_policy(self, config_dir,
                                                           cli_argv, capsys):
        """First source wins with a warning — not `tenant conflict`, which is
        both the wrong name and the wrong policy for a platform carrier."""
        a = Path(config_dir) / "team-a"
        b = Path(config_dir) / "team-b"
        a.mkdir()
        b.mkdir()
        _write_file(a / "_defaults.yml", "defaults:\n  mysql_connections: 10\n")
        _write_file(b / "_defaults.yml", "defaults:\n  mysql_connections: 20\n")
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", f"{a},{b}", "--output", str(out))
        assert main() == 0
        printed = capsys.readouterr().out
        assert "Platform file duplicates" in printed
        assert "tenant conflict" not in printed
        assert (out / "_defaults.yml").read_text(
            encoding="utf-8") == "defaults:\n  mysql_connections: 10\n"


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("make") is None,
                    reason="needs GNU make + POSIX sh; the recipe runs on Linux/macOS")
class TestTheRefusalCanActuallyFailTheTarget:
    """⛔ The wire, not the two halves.

    No other case here goes through the Makefile — they call `main()` or the
    helpers in-process, and most never reach `main()` at all. None of
    them notices if
    `make sharded-assemble` stops being able to fail — prefix the recipe line
    with `-`, append `|| true`, and the target reports success on a tree the
    exporter refuses in full. The sibling producer's gate was measured going
    green under exactly that mutation (#1783).
    """

    def test_make_sharded_assemble_stops_before_the_artifact(self, tmp_path):
        REPO = Path(__file__).resolve().parents[2]
        s = tmp_path / "team-a"
        s.mkdir()
        for fn, val in (("db-a.yaml", 50), ("db-a.yml", 90)):
            (s / fn).write_text(
                f"tenants:\n  db-a:\n    mysql_connections: {val}\n",
                encoding="utf-8")

        artifact = REPO / ".build" / "config-dir"
        before = artifact.stat().st_mtime_ns if artifact.exists() else None

        r = subprocess.run(["make", "sharded-assemble", f"SOURCES={s}"],
                           cwd=REPO, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)

        assert r.returncode != 0, (
            "the recipe ran to completion on a tree the exporter would reject "
            f"in full\nstdout: {r.stdout}\nstderr: {r.stderr}")
        assert "tenant_uniqueness" in (r.stdout + r.stderr), r.stdout + r.stderr
        after = artifact.stat().st_mtime_ns if artifact.exists() else None
        assert after == before, "the target wrote its artifact anyway"


class TestTheExporterReadsTheOutputDirNotJustOurCarriers:
    """⛔ `assemble()` never clears `--output`; the exporter reads what is
    THERE, not what this run copied.

    Blind review reached #1794's own symptom by a second route: run the target,
    apply the remediation the tool suggests (one file owns the tenant), run it
    again — the previous carrier is still in the output dir and the exporter
    rejects the whole directory, while the tool prints `✅ Assembled 3 file(s)`
    and exits 0. Measured on the parent commit too, so the accumulation is
    older than #1794; what is new is that a gate claims to speak for the
    artifact, which means it has to look where the artifact actually is.
    """

    def _sources(self, config_dir, *files):
        s = Path(config_dir) / "team"
        s.mkdir(parents=True, exist_ok=True)
        _write_file(s / "_defaults.yaml", "defaults:\n  x: 1\n")
        for fn, tenant in files:
            _write_file(s / fn, _TENANT_DOC.format(t=tenant, v=1))
        return s

    def test_a_previous_runs_carrier_is_part_of_the_question(
            self, config_dir, cli_argv):
        s = self._sources(config_dir, ("legacy-db-a.yaml", "db-a"))
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 0

        # the remediation this tool's own message asks for
        (s / "legacy-db-a.yaml").rename(s / "db-a.yaml")
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 1, (
            "the tool reported success for a directory the exporter rejects "
            "in full")

    def test_a_leftover_that_does_not_duplicate_is_disclosed_not_blocked(
            self, config_dir, capsys, cli_argv):
        """CONTROL — refuse only where the exporter would, and say the rest.

        A leftover carrier declaring some OTHER tenant is a directory the
        exporter loads happily, so blocking it would be this tool inventing a
        policy. It is still not something this assembly produced and the
        manifest does not mention it, so it gets named.
        """
        s = self._sources(config_dir, ("db-a.yaml", "db-a"))
        out = Path(config_dir) / "output"
        out.mkdir()
        _write_file(out / "db-z.yaml", _TENANT_DOC.format(t="db-z", v=9))

        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 0
        assert "db-z.yaml" in capsys.readouterr().err

    def test_check_without_output_says_which_half_it_answered(
            self, config_dir, capsys, cli_argv):
        """⛔ `--check` takes no required `--output`, so it cannot see the
        other half. Saying so is the difference between a narrow answer and a
        wrong one."""
        s = self._sources(config_dir, ("db-a.yaml", "db-a"))
        cli_argv("assemble", "--sources", str(s), "--check")
        assert main() == 0
        assert "these sources only" in capsys.readouterr().out

    def test_check_with_output_does_see_the_other_half(self, config_dir,
                                                       cli_argv):
        s = self._sources(config_dir, ("db-a.yaml", "db-a"))
        out = Path(config_dir) / "output"
        out.mkdir()
        _write_file(out / "legacy-db-a.yaml", _TENANT_DOC.format(t="db-a", v=9))
        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--check")
        assert main() == 1

    def test_an_overwritten_leftover_is_neither_blocked_nor_reported(
            self, config_dir, capsys, cli_argv):
        """CONTROL — a leftover this run OVERWRITES is not a leftover.

        The exit code alone cannot see this: the question is a dict keyed by
        carrier name, so re-adding an overwritten name is a no-op there
        (measured — the mutation survives an rc-only assertion). What the
        filter actually governs is the message, and without it a plain re-run
        tells the operator that the files it just wrote were not produced by
        this assembly.
        """
        s = self._sources(config_dir, ("db-a.yaml", "db-a"))
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 0
        capsys.readouterr()

        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 0, "a re-run with unchanged sources must stay green"
        assert "not produced by this assembly" not in capsys.readouterr().err


class TestANonRegularCarrierIsAnInputFactNotAFailedMeasurement:
    """A shard's broken symlink or a directory named `subteam.yaml`.

    The exporter serves this tree (`config_hierarchy.go` logs a WARN and moves
    on) and the sibling producer passes it. Measured regression from the first
    cut of #1794: staging the entry raised, the copy failure was classified as
    `could not measure`, and `sharded-check` answered rc 2 where the parent
    commit answered rc 0.
    """

    def _tree(self, config_dir):
        s = Path(config_dir) / "team"
        s.mkdir(parents=True, exist_ok=True)
        _write_file(s / "_defaults.yaml", "defaults:\n  x: 1\n")
        _write_file(s / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=1))
        return s

    @pytest.mark.skipif(sys.platform == "win32",
                        reason="needs POSIX symlink creation")
    def test_a_broken_symlink_does_not_become_could_not_measure(
            self, config_dir, capsys, cli_argv):
        s = self._tree(config_dir)
        (s / "dangling.yaml").symlink_to("../gone/dangling.yaml")
        cli_argv("assemble", "--sources", str(s), "--check")
        assert main() == 0
        assert "dangling.yaml" in capsys.readouterr().err, (
            "dropped without naming it — that is the silent loss this family "
            "is about")

    def test_a_directory_named_like_a_carrier_does_not_become_an_error(
            self, config_dir, capsys, cli_argv):
        s = self._tree(config_dir)
        (s / "subteam.yaml").mkdir()
        cli_argv("assemble", "--sources", str(s), "--check")
        assert main() == 0
        assert "subteam.yaml" in capsys.readouterr().err

    def test_a_real_unreadable_stage_is_still_could_not_measure(
            self, config_dir, cli_argv, monkeypatch):
        """CONTROL — the UNMEASURED arm must survive the filter above.

        Dropping unreadable ENTRIES must not turn into swallowing a staging
        failure: if the copy fails for a carrier we did accept, that is still
        `could not measure`.
        """
        s = self._tree(config_dir)
        out = Path(config_dir) / "output"

        real_copy = shutil.copy2

        def _boom(src, dst, *a, **k):
            if Path(src).name == "db-a.yaml":
                raise OSError(5, "simulated I/O error")
            return real_copy(src, dst, *a, **k)

        monkeypatch.setattr(tu.shutil, "copy2", _boom)
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 2
class TestZeroCarriersIsAMisPointedSources:
    """The sibling producer refuses a `--config-dir` holding no carrier and
    writes down why: that is a mis-pointed path far more often than an
    intentional empty deploy. This side had no such arm — `--sources ","`, an
    empty directory, and (once unreadable entries stopped being fatal) a tree
    whose only config-named entry is unreadable all produced an empty
    config-dir and exited 0, which the exporter reads as a platform with no
    tenants.

    ⛔ Not a tenant-count policy: a source holding only `_defaults.yaml` is one
    carrier and stays allowed, exactly as the sibling allows it.
    """

    def test_a_degenerate_sources_value_is_refused(self, config_dir, cli_argv):
        cli_argv("assemble", "--sources", ",", "--output",
                 str(Path(config_dir) / "out"))
        assert main() == 1

    def test_a_source_with_no_carrier_is_refused(self, config_dir, cli_argv):
        s = Path(config_dir) / "team"
        s.mkdir()
        _write_file(s / "notes.txt", "not yaml")
        cli_argv("assemble", "--sources", str(s), "--check")
        assert main() == 1

    def test_an_unreadable_only_source_is_refused_not_assembled_empty(
            self, config_dir, cli_argv):
        """The shape PR #1814's pin exercises, and the reason it still passes.

        Its fixture is a source whose only config-named entry is a DIRECTORY
        called `x.yaml`. Dropping unreadable entries (blind review) made that
        tree assemble zero files and exit 0; this arm is what keeps it loud.
        """
        s = Path(config_dir) / "team"
        s.mkdir()
        (s / "x.yaml").mkdir()
        out = Path(config_dir) / "out"
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 1

    def test_a_defaults_only_source_is_still_allowed(self, config_dir,
                                                     cli_argv):
        """CONTROL — the boundary the sibling states in words."""
        s = Path(config_dir) / "team"
        s.mkdir()
        _write_file(s / "_defaults.yaml", "defaults:\n  x: 1\n")
        out = Path(config_dir) / "out"
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 0
        assert (out / "_defaults.yaml").exists()
class TestTheOutputScanFollowsTheExporterIntoSubdirectories:
    """The exporter WALKS the config-dir, so a flat scan of it under-reports.

    Caught by the repo's own `test_confd_enumeration_contract` on the first
    cut of the output-dir question, and it is not a formality: a carrier the
    exporter loads out of a subdirectory is a second declaration the gate
    would never have seen.
    """

    def test_a_nested_leftover_still_counts(self, config_dir, cli_argv):
        s = Path(config_dir) / "team"
        s.mkdir()
        _write_file(s / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=1))
        out = Path(config_dir) / "output"
        (out / "archive").mkdir(parents=True)
        _write_file(out / "archive" / "db-a.yaml",
                    _TENANT_DOC.format(t="db-a", v=9))

        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 1, (
            "a nested carrier declaring the same tenant is a file the "
            "exporter loads, so the directory is rejected in full")

    def test_same_basename_in_two_subdirectories_is_two_files(
            self, config_dir, cli_argv):
        """⛔ Staging keys on the relative PATH, not the basename.

        Flattening these two would let one overwrite the other in the staged
        tree and take a real duplicate back out of the question — a fail-open
        produced by the measurement, not by the config.
        """
        s = Path(config_dir) / "team"
        s.mkdir()
        _write_file(s / "_defaults.yaml", "defaults:\n  x: 1\n")
        out = Path(config_dir) / "output"
        for sub in ("a", "b"):
            (out / sub).mkdir(parents=True)
            _write_file(out / sub / "db-a.yaml",
                        _TENANT_DOC.format(t="db-a", v=1))

        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 1

    def test_a_nested_carrier_for_another_tenant_is_not_a_refusal(
            self, config_dir, cli_argv):
        """CONTROL — recursion must not turn into refusing every nested tree."""
        s = Path(config_dir) / "team"
        s.mkdir()
        _write_file(s / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=1))
        out = Path(config_dir) / "output"
        (out / "archive").mkdir(parents=True)
        _write_file(out / "archive" / "db-z.yaml",
                    _TENANT_DOC.format(t="db-z", v=1))

        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 0
class TestEveryFaceDescribesTheArtifact:
    """`✅ Assembled N`, `--json`, and the manifest all counted what this run
    COPIED, while the exporter reads the directory.

    Measured: retire a shard at source, re-run, and all three say 1 file while
    the artifact holds 2 — the retired tenant keeps alerting and no
    machine-readable face mentions it. `--json` was worse than the text mode,
    because the disclosure was printed only when `--json` was absent, and the
    JSON document is the face a CI parses.
    """

    def _run_twice_then_retire(self, config_dir, cli_argv, extra=()):
        s = Path(config_dir) / "team"
        s.mkdir(parents=True, exist_ok=True)
        _write_file(s / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=1))
        _write_file(s / "app-x.yaml", _TENANT_DOC.format(t="app-x", v=1))
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 0
        (s / "app-x.yaml").unlink()          # the shard is retired at source
        cli_argv("assemble", "--sources", str(s), "--output", str(out), *extra)
        return s, out

    def test_the_success_line_names_the_total_the_exporter_will_read(
            self, config_dir, capsys, cli_argv):
        _, out = self._run_twice_then_retire(config_dir, cli_argv)
        capsys.readouterr()          # drop the first run's output
        assert main() == 0
        printed = capsys.readouterr()
        assert sorted(p.name for p in out.iterdir()) == ["app-x.yaml", "db-a.yaml"]
        assert "holds 2 carrier(s) the exporter will read" in printed.out, printed.out

    def test_json_carries_the_residue_and_the_artifact_count(
            self, config_dir, capsys, cli_argv):
        """⛔ The CI-readable face. Silence here was the whole defect."""
        _, out = self._run_twice_then_retire(config_dir, cli_argv, ("--json",))
        capsys.readouterr()          # the first run printed text, not JSON
        assert main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["file_count"] == 1
        assert payload["artifact_file_count"] == 2
        assert payload["residue"] == ["app-x.yaml"]

    def test_the_manifest_records_it_too(self, config_dir, cli_argv):
        s = Path(config_dir) / "team"
        s.mkdir()
        _write_file(s / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=1))
        _write_file(s / "app-x.yaml", _TENANT_DOC.format(t="app-x", v=1))
        out = Path(config_dir) / "output"
        mani = Path(config_dir) / "m.json"
        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--manifest", str(mani))
        assert main() == 0
        (s / "app-x.yaml").unlink()
        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--manifest", str(mani))
        assert main() == 0
        assert json.loads(mani.read_text(encoding="utf-8"))["residue"] == [
            "app-x.yaml"]

    def test_a_clean_run_says_nothing_about_residue(self, config_dir, capsys,
                                                    cli_argv):
        """CONTROL — no residue, no extra noise on any face."""
        s = Path(config_dir) / "team"
        s.mkdir()
        _write_file(s / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=1))
        out = Path(config_dir) / "output"
        mani = Path(config_dir) / "m.json"
        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--manifest", str(mani), "--json")
        assert main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert "residue" not in payload
        assert "artifact_file_count" not in payload
        assert "residue" not in json.loads(mani.read_text(encoding="utf-8"))


class TestTheRefusalDoesNotSendTheOperatorSomewhereWorse:
    """⛔ The forwarded advice was measured end to end and it made things worse.

    `tenant_uniqueness` says "decide which single file owns each tenant and
    remove the other". When one of the two files is a LEFTOVER in `--output`,
    the operator cannot edit it — it is in no source. Following the sentence
    verbatim ended at rc 0 with that leftover still shipping and the operator's
    rename silently undone: strictly worse than the refusal they started from.
    """

    def _stale_duplicate(self, config_dir, cli_argv):
        s = Path(config_dir) / "team"
        s.mkdir(parents=True, exist_ok=True)
        _write_file(s / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=1))
        out = Path(config_dir) / "output"
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        assert main() == 0
        (s / "db-a.yaml").rename(s / "dba-primary.yaml")   # renamed at source
        cli_argv("assemble", "--sources", str(s), "--output", str(out))
        return s, out

    def test_it_names_the_leftover_and_the_directory(self, config_dir, capsys,
                                                     cli_argv):
        self._stale_duplicate(config_dir, cli_argv)
        assert main() == 1
        err = capsys.readouterr().err
        assert "db-a.yaml" in err
        assert "left over from an earlier run" in err, err
        assert "no source produces it" in err, err

    def test_it_does_not_forward_the_edit_your_sources_advice(
            self, config_dir, capsys, cli_argv):
        """The sentence that sends them the wrong way must be absent HERE.

        Not deleted globally — it is the right advice when both carriers are
        in the sources, which the control below keeps green.
        """
        self._stale_duplicate(config_dir, cli_argv)
        assert main() == 1
        assert "Decide which single file owns" not in capsys.readouterr().err

    def test_a_source_only_duplicate_still_gets_the_checks_own_advice(
            self, config_dir, capsys, cli_argv):
        """CONTROL — both carriers are the operator's to edit, so the
        check's own wording is the correct one and must survive."""
        s = Path(config_dir) / "team"
        s.mkdir()
        _write_file(s / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=1))
        _write_file(s / "legacy-db-a.yaml", _TENANT_DOC.format(t="db-a", v=9))
        cli_argv("assemble", "--sources", str(s), "--output",
                 str(Path(config_dir) / "out"))
        assert main() == 1
        err = capsys.readouterr().err
        assert "Decide which single file owns" in err, err
        assert "left over from an earlier run" not in err


class TestBothShardedTargetsAskAboutTheSameDirectory:
    """`sharded-check` is the dry run of `sharded-assemble`. It could not pass
    `--output`, so the hint printed by the tool ("pass --output …") named an
    action that entry point cannot perform, and the two targets returned
    opposite verdicts for one tree.

    Read out of the Makefile rather than retyped: a copy here would let the
    recipes drift apart again with nothing noticing.
    """

    def _recipe(self, name: str) -> str:
        text = (Path(__file__).resolve().parents[2] / "Makefile").read_text(
            encoding="utf-8")
        start = text.index(f"\n{name}:")
        end = text.index("\n\n", start)
        return text[start:end]

    def test_check_and_assemble_target_the_same_output(self):
        check = self._recipe("sharded-check")
        assemble_ = self._recipe("sharded-assemble")
        assert "--output $(SHARDED_OUTDIR)" in check, check
        assert "--output $(SHARDED_OUTDIR)" in assemble_, assemble_

    def test_the_dry_run_does_not_create_the_directory(self, config_dir,
                                                       cli_argv):
        """⛔ Passing `--output` to a dry run must stay a dry run."""
        s = Path(config_dir) / "team"
        s.mkdir()
        _write_file(s / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=1))
        out = Path(config_dir) / "never-created"
        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--check")
        assert main() == 0
        assert not out.exists()
class TestBlameLandsOnTheRightFile:
    """⛔ The leftover-specific advice is chosen by matching residue names
    against the check's detail lines. A substring test blames the wrong file.

    `db-a.yaml` is a substring of `legacy-db-a.yaml`, and both are names this
    repo actually uses. Measured on the substring version: a duplicate living
    entirely between two SOURCE files blamed an unrelated leftover, and the
    `elif` then swallowed the only advice that applied — worse than forwarding
    unconditionally, which is what the parent commit did.
    """

    def _tree(self, config_dir, leftover_name):
        s = Path(config_dir) / "src"
        out = Path(config_dir) / "out"
        s.mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        # the duplicate is entirely between two SOURCE files
        _write_file(s / "legacy-db-a.yaml", _TENANT_DOC.format(t="db-x", v=1))
        _write_file(s / "other.yaml", _TENANT_DOC.format(t="db-x", v=2))
        _write_file(out / leftover_name, _TENANT_DOC.format(t="db-a", v=1))
        return s, out

    def test_a_leftover_whose_name_is_a_substring_is_not_blamed(
            self, config_dir, capsys, cli_argv):
        s, out = self._tree(config_dir, "db-a.yaml")
        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--check")
        assert main() == 1
        err = capsys.readouterr().err
        assert "Decide which single file owns" in err, (
            "the advice that actually applies was swallowed:\n" + err)
        assert "left over from an earlier run" not in err, err

    def test_a_leftover_that_really_is_a_participant_is_still_blamed(
            self, config_dir, capsys, cli_argv):
        """CONTROL — the leftover-specific advice must survive for the case it
        was written for."""
        s = Path(config_dir) / "src"
        out = Path(config_dir) / "out"
        s.mkdir()
        out.mkdir()
        _write_file(s / "dba-primary.yaml", _TENANT_DOC.format(t="db-a", v=1))
        _write_file(out / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=9))
        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--check")
        assert main() == 1
        err = capsys.readouterr().err
        assert "left over from an earlier run" in err, err
        assert "Decide which single file owns" not in err


class TestAnUnreadableSubtreeUnderOutputIsNotClean:
    """⛔ `iter_config_files` walks with `onerror=None`: a subdirectory it
    cannot enumerate vanishes, taking any declaration inside it with it.

    Measured as a non-root user (as root the mode bits are ignored, so the
    first probe wrongly showed nothing wrong): with the subtree unreadable the
    gate answered rc 0 and printed nothing, while the identical tree readable
    was correctly refused. A subtree we could not read is "could not measure",
    which is the single thing this gate must not round down to clean.
    """

    def _tree(self, config_dir):
        s = Path(config_dir) / "src"
        out = Path(config_dir) / "out"
        s.mkdir(parents=True, exist_ok=True)
        (out / "locked").mkdir(parents=True, exist_ok=True)
        _write_file(s / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=1))
        _write_file(out / "locked" / "dup.yaml", _TENANT_DOC.format(t="db-a", v=9))
        return s, out

    @pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                        reason="mode bits are advisory for root and on Windows; "
                               "the arm under test is a real permission denial")
    def test_it_refuses_instead_of_reporting_clean(self, config_dir, capsys,
                                                   cli_argv):
        s, out = self._tree(config_dir)
        os.chmod(out / "locked", 0o000)
        try:
            cli_argv("assemble", "--sources", str(s), "--output", str(out),
                     "--check")
            assert main() == 2
            err = capsys.readouterr().err
            assert "could not measure" in err.lower(), err
            assert "locked" in err, err
        finally:
            os.chmod(out / "locked", 0o755)

    def test_the_same_tree_readable_is_refused_for_the_real_reason(
            self, config_dir, capsys, cli_argv):
        """CONTROL — the duplicate must be found when we CAN see it, or the
        arm above could be satisfied by a check that refuses everything."""
        s, out = self._tree(config_dir)
        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--check")
        assert main() == 1
        assert "tenant_uniqueness" in capsys.readouterr().err

    def test_a_clean_output_tree_stays_green(self, config_dir, cli_argv):
        """CONTROL — no unreadable subtree, no refusal."""
        s = Path(config_dir) / "src"
        out = Path(config_dir) / "out"
        s.mkdir()
        (out / "sub").mkdir(parents=True)
        _write_file(s / "db-a.yaml", _TENANT_DOC.format(t="db-a", v=1))
        _write_file(out / "sub" / "db-z.yaml", _TENANT_DOC.format(t="db-z", v=1))
        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--check")
        assert main() == 0


class TestTheArtifactTotalUsesOnePredicate:
    """`count + len(residue)` added two different carrier predicates: `count`
    is what was copied (hidden entries included, because `discover_yamls` does
    not skip them — #1827), while `residue` mirrors the exporter, which does
    skip them. Measured: the JSON claimed 3 where the exporter reads 2.
    """

    def test_a_hidden_carrier_is_not_counted_as_readable(self, config_dir,
                                                         capsys, cli_argv):
        s = Path(config_dir) / "src"
        out = Path(config_dir) / "out"
        s.mkdir()
        out.mkdir()
        _write_file(s / "a.yaml", _TENANT_DOC.format(t="a", v=1))
        _write_file(s / ".hidden.yaml", _TENANT_DOC.format(t="h", v=1))
        _write_file(out / "old.yaml", _TENANT_DOC.format(t="old", v=1))
        cli_argv("assemble", "--sources", str(s), "--output", str(out),
                 "--json")
        assert main() == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["file_count"] == 2          # what was copied
        assert payload["residue"] == ["old.yaml"]
        # a.yaml + old.yaml — the hidden one is not read by the exporter
        assert payload["artifact_file_count"] == 2


class TestManifestOnlyNamesTheMissingFlag:
    """argparse accepts `--manifest` alone (there is no load path — #1829), so
    the zero-carrier arm is reachable with no sources at all. Blaming the
    carriers there points at the wrong thing."""

    def test_it_says_sources_is_required(self, config_dir, capsys, cli_argv):
        cli_argv("assemble", "--manifest", str(Path(config_dir) / "m.json"),
                 "--output", str(Path(config_dir) / "out"))
        assert main() == 1
        assert "--sources is required" in capsys.readouterr().err

    def test_a_real_empty_source_still_blames_the_source(self, config_dir,
                                                         capsys, cli_argv):
        """CONTROL — when sources WERE given, name them."""
        s = Path(config_dir) / "src"
        s.mkdir()
        _write_file(s / "notes.txt", "not yaml")
        cli_argv("assemble", "--sources", str(s), "--check")
        assert main() == 1
        err = capsys.readouterr().err
        assert "no config carrier found in" in err
        assert str(s) in err
