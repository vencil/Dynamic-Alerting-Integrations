"""The capability probes in ``tests/_platform_fs.py`` (#2559).

A probe-driven skip is only safe if it cannot spread to the hosts where the
tests carry weight: a probe that answered False on Linux CI would turn every
symlink test into a skip under a green run. The first test is that guard.
"""
from __future__ import annotations

import ast
import os
import shutil
import sys

import pytest

import _platform_fs as pf
from _pysource import parse_py
from _tree import REPO_ROOT, repo_files


@pytest.mark.skipif(os.name == "nt", reason="Windows may lack the privilege; that is what the probe is for")
def test_a_posix_host_can_create_symlinks():
    """⛔ On POSIX the probe must be True — otherwise the symlink tests are
    skipping where they are supposed to run (CI, the dev container)."""
    assert pf.can_symlink(), (
        "tests/_platform_fs.can_symlink() is False on a POSIX host: every test "
        "using symlink_or_skip / require_symlinks is being skipped here")


_ON_LINUX = sys.platform.startswith("linux")
_NOT_LINUX = "the guard is for Linux, where CI and the dev container run"

#: Every name a test passes to ``require_file_name``. Linux holds them all.
_HOSTILE_NAMES = (
    b"evil\nx.yaml", b"has\nnewline.yaml", b'has"quote.yaml',
    b"has\\backslash.yaml", b"legacy-\xff.yaml", b"b\xff.yaml",
    "db-\x1b[2J\x1b[Hevil.yaml", "2024-01-01 10:00:00+00:00.yaml",
)


@pytest.mark.skipif(not _ON_LINUX, reason=_NOT_LINUX)
def test_linux_has_every_filesystem_capability_the_probes_ask_about():
    """⛔ Same guard as above for the other probes: any of these False on
    Linux means tests are skipping where they are supposed to run."""
    unmet = [repr(n) for n in _HOSTILE_NAMES if not pf.can_name_file(n)]
    if not pf.has_posix_modes():
        unmet.append("POSIX mode bits")
    if not pf.has_case_sensitive_names():
        unmet.append("case-sensitive names")
    if not pf.can_exec_shebang_scripts():
        unmet.append("running a #! script as a command")
    unmet += [f"os.{a}" for a in ("geteuid", "getegid", "listxattr", "getxattr")
              if not hasattr(os, a)]
    assert not unmet, f"probes answering False on Linux (tests are skipping): {unmet}"


@pytest.mark.skipif(not (_ON_LINUX and os.environ.get("GITHUB_ACTIONS") == "true"),
                    reason="the tools are only guaranteed on the CI runner")
@pytest.mark.parametrize("tool", ["make", "kubectl"])
def test_the_ci_runner_has_the_tools_tests_require(tool):
    """⛔ ``require_tool`` skips when a tool is absent. On the CI runner that
    would be a silent loss, so there the tool must exist."""
    assert shutil.which(tool), f"`{tool}` is not on PATH on the CI runner"


@pytest.mark.parametrize("probe, require, args", [
    ("can_name_file", "require_file_name", ("x.yaml",)),
    ("has_posix_modes", "require_posix_modes", ()),
    ("has_case_sensitive_names", "require_case_sensitive_names", ()),
    ("can_exec_shebang_scripts", "require_shebang_scripts", ()),
])
def test_each_require_skips_exactly_when_its_probe_says_no(monkeypatch, probe, require, args):
    monkeypatch.setattr(pf, probe, lambda *a: True)
    getattr(pf, require)(*args)          # capability present: returns
    monkeypatch.setattr(pf, probe, lambda *a: False)
    with pytest.raises(pytest.skip.Exception):
        getattr(pf, require)(*args)


def test_require_os_attrs_and_tool_skip_only_for_what_is_missing():
    pf.require_os_attrs("getcwd")
    pf.require_tool(os.path.basename(sys.executable))
    with pytest.raises(pytest.skip.Exception) as ei:
        pf.require_os_attrs("getcwd", "no_such_os_attr_2559")
    assert "os.no_such_os_attr_2559" in str(ei.value) and "getcwd" not in str(ei.value)
    with pytest.raises(pytest.skip.Exception):
        pf.require_tool("definitely-not-a-real-binary-2559")


def test_the_name_probe_reads_the_listing_back(tmp_path):
    """A plain name is storable everywhere; the probe must say so (a probe
    that always answered False would skip every caller and stay green)."""
    assert pf.can_name_file("plain-2559.yaml")
    assert pf.can_name_file(b"plain-2559.yaml")


def test_without_the_capability_it_skips_and_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "can_symlink", lambda: False)
    link = tmp_path / "link"
    with pytest.raises(pytest.skip.Exception) as ei:
        pf.symlink_or_skip(tmp_path / "target", link)
    assert pf.SYMLINK_SKIP_REASON in str(ei.value)
    assert not os.path.lexists(link)


def test_only_the_missing_capability_skips(tmp_path, monkeypatch):
    """A failure of the call itself is a real failure on every host: it must
    raise, not be absorbed into the skip."""
    monkeypatch.setattr(pf, "can_symlink", lambda: True)

    def refuse(src, dst, **kwargs):
        raise FileExistsError(17, "File exists", str(dst))

    monkeypatch.setattr(pf.os, "symlink", refuse)
    with pytest.raises(FileExistsError):
        pf.symlink_or_skip(tmp_path / "target", tmp_path / "link")


def test_with_the_capability_it_creates_the_link(tmp_path):
    pf.require_symlinks()
    target = tmp_path / "target"
    target.write_text("x\n", encoding="utf-8")
    link = tmp_path / "link"
    pf.symlink_or_skip(target, link)
    assert link.is_symlink()
    assert link.read_text(encoding="utf-8") == "x\n"


def test_symlink_or_else_falls_back_only_without_the_capability(tmp_path, monkeypatch):
    """Without the capability the fallback runs and no link is attempted;
    with it, the link is attempted and a failure of the call still raises."""
    called = []
    monkeypatch.setattr(pf, "can_symlink", lambda: False)
    monkeypatch.setattr(pf.os, "symlink", lambda *a, **k: pytest.fail("attempted a symlink"))
    pf.symlink_or_else("t", tmp_path / "l", lambda: called.append("fallback"))
    assert called == ["fallback"]

    def refuse(src, dst, **kwargs):
        raise PermissionError(1, "Operation not permitted", str(dst))

    monkeypatch.setattr(pf, "can_symlink", lambda: True)
    monkeypatch.setattr(pf.os, "symlink", refuse)
    with pytest.raises(PermissionError):
        pf.symlink_or_else("t", tmp_path / "l", lambda: called.append("fallback"))
    assert called == ["fallback"], "the fallback must not absorb a refused symlink"


def _bare_symlink_calls(tree: ast.AST) -> list[int]:
    """Line numbers of ``os.symlink(...)`` and ``<x>.symlink_to(...)`` CALLS.
    A reference that is not called (``monkeypatch.setattr(os, "symlink", f)``)
    creates nothing and is not counted."""
    lines = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        f = node.func
        if f.attr == "symlink_to" or (
                f.attr == "symlink" and isinstance(f.value, ast.Name) and f.value.id == "os"):
            lines.append(node.lineno)
    return lines


def test_the_scanner_sees_both_spellings_and_only_calls():
    """The guard below is only as good as this predicate: both spellings are
    caught, and a non-call reference is not."""
    src = "\n".join([
        "import os",
        "os.symlink(a, b)",
        "(p / 'l').symlink_to(t)",
        "monkeypatch.setattr(os, 'symlink', f)",
        "x = os.symlink",
    ])
    assert _bare_symlink_calls(ast.parse(src)) == [2, 3]


def test_tests_create_symlinks_only_through_the_helper():
    """⛔ A test that calls ``os.symlink`` / ``Path.symlink_to`` directly goes
    red on a host without the capability instead of skipping (#2559): seven
    such tests reached main within days of the helper landing. Use
    ``symlink_or_skip(target, link)`` from ``tests/_platform_fs.py``."""
    tests_dir = REPO_ROOT / "tests"
    exempt = {tests_dir / "_platform_fs.py"}
    scanned, offenders = 0, []
    for path in repo_files(".py"):
        if tests_dir not in path.parents or path in exempt:
            continue
        scanned += 1
        offenders += [f"{path.relative_to(REPO_ROOT).as_posix()}:{n}"
                      for n in _bare_symlink_calls(parse_py(path))]
    assert scanned > 100, f"scanned only {scanned} test files: the listing is wrong"
    assert not offenders, (
        "create symlinks with symlink_or_skip(target, link) from _platform_fs, "
        f"not directly: {offenders}")
