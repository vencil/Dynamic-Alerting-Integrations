#!/usr/bin/env python3
"""Tests for assemble_config_dir.py — Sharded GitOps assembly tool."""

import json
import os
import tempfile
import unittest
from pathlib import Path

# Make scripts/tools importable

from assemble_config_dir import (  # noqa: E402
    PLATFORM_FILES,
    _file_sha256,
    assemble,
    build_manifest,
    detect_conflicts,
    discover_yamls,
    validate_merged,
)


class TestDiscoverYamls(unittest.TestCase):
    def test_finds_yaml_files(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.yaml").write_text("a: 1", encoding="utf-8")
            (Path(d) / "b.yaml").write_text("b: 2", encoding="utf-8")
            (Path(d) / "c.txt").write_text("not yaml", encoding="utf-8")
            result = discover_yamls(Path(d))
            self.assertEqual(len(result), 2)
            self.assertTrue(all(f.suffix == ".yaml" for f in result))

    def test_missing_dir_raises(self):
        with self.assertRaises(FileNotFoundError):
            discover_yamls(Path("/nonexistent/dir"))

    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as d:
            result = discover_yamls(Path(d))
            self.assertEqual(result, [])


class TestDetectConflicts(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.src_a = Path(self.tmpdir) / "team-a"
        self.src_b = Path(self.tmpdir) / "team-b"
        self.src_a.mkdir()
        self.src_b.mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def test_no_conflicts(self):
        (self.src_a / "db-a.yaml").write_text("a: 1", encoding="utf-8")
        (self.src_b / "db-b.yaml").write_text("b: 2", encoding="utf-8")
        conflicts, file_map = detect_conflicts([self.src_a, self.src_b])
        self.assertEqual(len(conflicts), 0)
        self.assertEqual(len(file_map), 2)

    def test_real_conflict(self):
        (self.src_a / "db-a.yaml").write_text("a: 1", encoding="utf-8")
        (self.src_b / "db-a.yaml").write_text("a: 99", encoding="utf-8")
        conflicts, file_map = detect_conflicts([self.src_a, self.src_b])
        self.assertIn("db-a.yaml", conflicts)
        self.assertNotIn("db-a.yaml", file_map)

    def test_identical_not_conflict(self):
        content = "tenants:\n  db-a:\n    x: '1'"
        (self.src_a / "db-a.yaml").write_text(content, encoding="utf-8")
        (self.src_b / "db-a.yaml").write_text(content, encoding="utf-8")
        conflicts, file_map = detect_conflicts([self.src_a, self.src_b])
        self.assertEqual(len(conflicts), 0)
        self.assertIn("db-a.yaml", file_map)

    def test_platform_file_first_wins(self):
        (self.src_a / "_defaults.yaml").write_text("d: 1", encoding="utf-8")
        (self.src_b / "_defaults.yaml").write_text("d: 2", encoding="utf-8")
        conflicts, file_map = detect_conflicts([self.src_a, self.src_b])
        # Platform file dups are reported as conflicts but still in file_map
        self.assertIn("_defaults.yaml", conflicts)
        self.assertIn("_defaults.yaml", file_map)
        # First source wins
        self.assertEqual(file_map["_defaults.yaml"],
                         self.src_a / "_defaults.yaml")


class TestAssemble(unittest.TestCase):
    def test_copies_files(self):
        with tempfile.TemporaryDirectory() as src, \
             tempfile.TemporaryDirectory() as out:
            (Path(src) / "a.yaml").write_text("a: 1", encoding="utf-8")
            file_map = {"a.yaml": Path(src) / "a.yaml"}
            count = assemble(file_map, Path(out))
            self.assertEqual(count, 1)
            self.assertTrue((Path(out) / "a.yaml").exists())
            self.assertEqual(
                (Path(out) / "a.yaml").read_text(encoding="utf-8"), "a: 1")

    def test_dry_run_does_not_write(self):
        with tempfile.TemporaryDirectory() as src:
            (Path(src) / "a.yaml").write_text("a: 1", encoding="utf-8")
            out = Path(src) / "nonexistent-output"
            file_map = {"a.yaml": Path(src) / "a.yaml"}
            count = assemble(file_map, out, dry_run=True)
            self.assertEqual(count, 1)
            self.assertFalse(out.exists())


class TestValidateMerged(unittest.TestCase):
    def test_valid_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.yaml").write_text(
                "tenants:\n  db-a:\n    x: '1'", encoding="utf-8")
            issues = validate_merged(Path(d))
            self.assertEqual(issues, [])

    def test_invalid_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "bad.yaml").write_text(
                ":\n  - [invalid", encoding="utf-8")
            issues = validate_merged(Path(d))
            self.assertTrue(any("parse error" in i for i in issues))

    def test_empty_yaml(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "empty.yaml").write_text("", encoding="utf-8")
            issues = validate_merged(Path(d))
            self.assertTrue(any("empty" in i for i in issues))


class TestManifest(unittest.TestCase):
    def test_build_manifest(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.yaml"
            p.write_text("a: 1", encoding="utf-8")
            file_map = {"a.yaml": p}
            manifest = build_manifest([Path(d)], file_map, {})
            self.assertEqual(manifest["file_count"], 1)
            self.assertIn("a.yaml", manifest["files"])
            self.assertEqual(len(manifest["files"]["a.yaml"]["sha256"]), 64)


class TestFileSha256(unittest.TestCase):
    def test_deterministic(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False,
                                          mode="w") as f:
            f.write("test content")
            f.flush()
            h1 = _file_sha256(Path(f.name))
            h2 = _file_sha256(Path(f.name))
            self.assertEqual(h1, h2)
            os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()
