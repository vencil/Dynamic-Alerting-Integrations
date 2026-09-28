"""_rule_tree's git listings keep a non-UTF-8 file name's identity (#1374).

`git ls-files -z` hands back raw path bytes. Decoded with ``errors="replace"``
a name that is not valid UTF-8 becomes a different name — one that opens
nothing — so the scanner would skip a tracked file while its non-empty guard
still passes. ``surrogateescape`` round-trips the bytes.

The name is written straight into a scratch repo's index through
``git update-index --index-info`` (read from stdin as bytes), so the test runs
on Windows too, where a file system cannot hold such a name.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools" / "lint"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "tools"))

import _rule_tree  # noqa: E402

_RAW = b"k8s/r\xe8gles.yaml"          # Latin-1 byte: not valid UTF-8
_RULES_RAW = b"rule-packs/rule-pack-r\xe8gles.yaml"


@pytest.fixture
def scratch_repo(tmp_path, monkeypatch):
    git = shutil.which("git")
    if git is None:
        pytest.skip("git not installed")
    repo = tmp_path / "repo"
    repo.mkdir()

    def run(*args, stdin=None):
        return subprocess.run([git, *args], cwd=repo, input=stdin, check=True,
                              capture_output=True, timeout=60)

    run("init", "-q")
    blob = run("hash-object", "-w", "--stdin", stdin=b"groups: []\n").stdout.strip()
    index = b"".join(b"100644 " + blob + b"\t" + name + b"\n" for name in (_RAW, _RULES_RAW))
    run("update-index", "--index-info", stdin=index)
    monkeypatch.setattr(_rule_tree, "_REPO_ROOT", str(repo))
    _rule_tree._expected_rule_files.cache_clear()
    yield repo
    _rule_tree._expected_rule_files.cache_clear()


def test_tracked_yaml_paths_keeps_the_raw_name(scratch_repo):
    paths = _rule_tree._tracked_yaml_paths()
    assert [p.encode("utf-8", "surrogateescape") for p in paths if p.startswith("k8s/")] == [_RAW]


def test_expected_rule_files_keeps_the_raw_name(scratch_repo):
    files = _rule_tree._expected_rule_files()
    assert {p.encode("utf-8", "surrogateescape") for p in files} == {_RULES_RAW}
