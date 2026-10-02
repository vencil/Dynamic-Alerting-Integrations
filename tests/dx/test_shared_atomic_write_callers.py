"""Every caller of the shared ``_atomic_write.atomic_write_text`` (#2128).

The helper now writes symlinks through, keeps hard links and a user's
``<out>.tmp``, and falls back in place (with a WARN) when the directory
cannot take a temp file. Those are helper properties, pinned in
``test_atomic_write.py``; what is pinned HERE is that each of the eight call
sites actually gets them end to end, and that a write failure leaves the
tool as rc 2 plus one line naming the target — not a traceback at rc 1.

Each call site is driven through its real ``main`` with only its INPUTS
stubbed (what to render, where the target is), so the write path — the
``with output_write(...)`` wrapper, the helper, ``exit_on_output_write_error``
— is the code that ships. ``CALLERS`` holds the eight sites; every test is
parametrized over all of them.

The adapters are module-level on purpose: the same scenarios can be replayed
against another checkout of the tools (the #2128 before/after table was
measured that way).
"""
from __future__ import annotations

import contextlib
import errno
import importlib
import io
import os
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from _platform_fs import symlink_or_skip  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
for _d in (REPO_ROOT / "scripts" / "dx", REPO_ROOT / "scripts" / "ops"):
    if str(_d) not in sys.path:
        sys.path.insert(0, str(_d))

OLD = "OLD CONTENT\n"
NEW = "NEW CONTENT\n"


# ═══════════════════════════════════════════════════════════════════════════
# Adapters: drive one call site's real main() at a target of our choosing
# ═══════════════════════════════════════════════════════════════════════════
@dataclass(frozen=True)
class Caller:
    module: str
    site: str                      # "file:line" as surveyed in #2128
    flag: str | None               # the flag its error message must name
    setup: Callable                # (mod, mp, root, target) -> argv-driven runner


def _argv_runner(mod, mp, argv):
    def run():
        mp.setattr(sys, "argv", argv)
        return mod.main()
    return run


def _setup_doc_map(mod, mp, root, target):
    mp.setattr(mod, "REPO_ROOT", root)
    mp.setattr(mod, "_get_map_path", lambda lang: target)
    mp.setattr(mod, "generate_doc_map", lambda lang, include_adr=True: NEW)
    return _argv_runner(mod, mp, ["generate_doc_map.py", "--generate", "--lang", "zh", "--safe"])


def _setup_tool_map(mod, mp, root, target):
    mp.setattr(mod, "REPO_ROOT", root)
    mp.setattr(mod, "TOOL_MAP", target)
    mp.setattr(mod, "gather_tools", lambda: {c: [] for c in mod.CATEGORY_ORDER})
    mp.setattr(mod, "generate_tool_map", lambda categorized, lang: NEW)
    return _argv_runner(mod, mp, ["generate_tool_map.py", "--generate", "--lang", "zh", "--safe"])


def _rule_pack_stats_common(mod, mp, root):
    aux = root / "aux"
    aux.mkdir(exist_ok=True)
    for name in ("arch.md", "design.md"):
        (aux / name).write_text("UNCHANGED\n", encoding="utf-8")
    mp.setattr(mod, "REPO_ROOT", root)
    mp.setattr(mod, "gather_stats", lambda: {})
    mp.setattr(mod, "generate_markdown_table", lambda stats, lang="zh": NEW)
    mp.setattr(mod, "_table_body", lambda table: table)
    mp.setattr(mod, "render_design_table", lambda stats, lang="zh": "")
    mp.setattr(mod, "_design_doc", lambda lang: aux / "design.md")
    mp.setattr(mod, "_replace_block", lambda cur, *a, **k: cur)
    return aux


def _setup_rule_pack_stats_fragment(mod, mp, root, target):
    aux = _rule_pack_stats_common(mod, mp, root)
    mp.setattr(mod, "INCLUDE_DIR", target.parent)
    mp.setattr(mod, "_get_stats_file", lambda lang: target)
    mp.setattr(mod, "_arch_doc", lambda lang: aux / "arch.md")
    mp.setattr(mod, "_replace_arch_block", lambda cur, body, rel: cur)
    return _argv_runner(mod, mp, ["generate_rule_pack_stats.py", "--generate"])


def _setup_rule_pack_stats_inject(mod, mp, root, target):
    aux = _rule_pack_stats_common(mod, mp, root)
    mp.setattr(mod, "INCLUDE_DIR", aux)
    mp.setattr(mod, "_get_stats_file", lambda lang: aux / "fragment.md")
    mp.setattr(mod, "_arch_doc", lambda lang: target)
    mp.setattr(mod, "_replace_arch_block", lambda cur, body, rel: NEW)
    return _argv_runner(mod, mp, ["generate_rule_pack_stats.py", "--generate"])


def _setup_byo(mod, mp, root, target):
    mp.setattr(mod, "REPO_ROOT", root)
    mp.setattr(mod, "_doc_path", lambda lang: target)
    mp.setattr(mod, "render_table", lambda lang: "")
    mp.setattr(mod, "replace_sentinel_block", lambda cur, table, doc: NEW)
    return _argv_runner(mod, mp, ["generate_byo_rulepack_table.py", "--generate"])


def _setup_adr_index(mod, mp, root, target):
    mp.setattr(mod, "discover_adrs", lambda adr_dir: [root / "0001.md"])
    mp.setattr(mod, "parse_adr", lambda p: None)
    mp.setattr(mod, "render_table", lambda entries, prefix="adr/", lang="zh": "")
    mp.setattr(mod, "replace_sentinel_block", lambda cur, table, tgt=None: NEW)
    return _argv_runner(mod, mp, ["generate_adr_index.py", "--write", "--target", str(target)])


def _setup_planning_index(mod, mp, root, target):
    mp.setattr(mod, "discover_all", lambda repo_root: [])
    mp.setattr(mod, "render_index", lambda entries: "")
    mp.setattr(mod, "replace_sentinel_block", lambda cur, body: NEW)
    return _argv_runner(mod, mp, ["generate_planning_index.py", "--write",
                                  "--repo-root", str(root), "--target", str(target)])


def _setup_audit_rules_drift(mod, mp, root, target):
    mp.setattr(mod, "REPO_ROOT", root)
    mp.setattr(mod, "DEFAULT_MEMORY", root / "no-memory-here")
    for loader in ("load_dev_rules", "load_hooks", "load_skills"):
        mp.setattr(mod, loader, lambda: [])
    mp.setattr(mod, "render_report", lambda *a, **k: NEW)
    return lambda: mod.main(["--out", str(target)])


CALLERS: dict[str, Caller] = {
    "audit_rules_drift": Caller(
        "audit_rules_drift", "scripts/ops/audit_rules_drift.py:490", "--out",
        _setup_audit_rules_drift),
    "rule_pack_stats_fragment": Caller(
        "generate_rule_pack_stats", "scripts/tools/dx/generate_rule_pack_stats.py:448",
        None, _setup_rule_pack_stats_fragment),
    "rule_pack_stats_inject": Caller(
        "generate_rule_pack_stats", "scripts/tools/dx/generate_rule_pack_stats.py:451",
        None, _setup_rule_pack_stats_inject),
    "tool_map": Caller(
        "generate_tool_map", "scripts/tools/dx/generate_tool_map.py:374", None,
        _setup_tool_map),
    "doc_map": Caller(
        "generate_doc_map", "scripts/tools/dx/generate_doc_map.py:507", None,
        _setup_doc_map),
    "byo_rulepack_table": Caller(
        "generate_byo_rulepack_table", "scripts/tools/dx/generate_byo_rulepack_table.py:258",
        None, _setup_byo),
    "adr_index": Caller(
        "generate_adr_index", "scripts/dx/generate_adr_index.py:253", "--target",
        _setup_adr_index),
    "planning_index": Caller(
        "generate_planning_index", "scripts/dx/generate_planning_index.py:503", "--target",
        _setup_planning_index),
}


def run_caller(name: str, mp, root: Path, target: Path) -> tuple[int, str]:
    """``(rc, stderr)`` of one run, the way the CLI would end it.

    ``SystemExit`` gives its code; any other exception is what the
    interpreter would do with it — a traceback on stderr and rc 1.
    """
    caller = CALLERS[name]
    mod = importlib.import_module(caller.module)
    run = caller.setup(mod, mp, root, target)
    err, out = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        try:
            rc = run()
            rc = 0 if rc is None else rc
        except SystemExit as exc:
            rc = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        except Exception:  # noqa: BLE001 — reproduce the interpreter's ending
            traceback.print_exc(file=err)
            rc = 1
    return rc, err.getvalue()


# ═══════════════════════════════════════════════════════════════════════════
# Scenarios
# ═══════════════════════════════════════════════════════════════════════════
def _target(root: Path) -> Path:
    d = root / "out"
    d.mkdir(exist_ok=True)
    t = d / "target.md"
    t.write_text(OLD, encoding="utf-8")
    return t


def _refuse_mkstemp_beside(mp, target: Path) -> None:
    """The directory holding *target* cannot take a new file (EACCES) — the
    in-process stand-in for a real unwritable directory, which root cannot
    have. The real one is exercised as uid 65534 in test_atomic_write."""
    real = tempfile.mkstemp
    where = os.path.realpath(target.parent)

    def mkstemp(*a, **k):
        if os.path.realpath(str(k.get("dir", ""))) == where:
            raise PermissionError(errno.EACCES, "Permission denied", str(k["dir"]))
        return real(*a, **k)

    mp.setattr(tempfile, "mkstemp", mkstemp)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path


_NAMES = sorted(CALLERS)


@pytest.mark.parametrize("name", _NAMES)
def test_must_fire_a_plain_file_is_rewritten_rc0(name, root, monkeypatch):
    target = _target(root)
    rc, err = run_caller(name, monkeypatch, root, target)
    assert (rc, target.read_text(encoding="utf-8")) == (0, NEW), err
    assert "WARN" not in err, err
    assert sorted(p.name for p in target.parent.iterdir()) == ["target.md"]


@pytest.mark.parametrize("name", _NAMES)
def test_a_symlink_target_stays_a_symlink_and_its_target_is_updated(name, root, monkeypatch):
    real = root / "elsewhere" / "real.md"
    real.parent.mkdir()
    real.write_text(OLD, encoding="utf-8")
    (root / "out").mkdir()
    link = root / "out" / "target.md"
    symlink_or_skip(real, link)
    rc, err = run_caller(name, monkeypatch, root, link)
    assert rc == 0, err
    assert link.is_symlink(), "the symlink was replaced by a regular file"
    assert real.read_text(encoding="utf-8") == NEW


@pytest.mark.parametrize("name", _NAMES)
def test_a_hard_linked_target_updates_every_name_with_a_warn(name, root, monkeypatch):
    target = _target(root)
    other = root / "other-name.md"
    os.link(target, other)
    rc, err = run_caller(name, monkeypatch, root, target)
    assert rc == 0, err
    assert other.read_text(encoding="utf-8") == NEW
    assert os.stat(target).st_nlink == 2
    assert "WARN" in err and "hard links" in err, err


@pytest.mark.parametrize("name", _NAMES)
def test_a_users_own_dot_tmp_is_left_alone(name, root, monkeypatch):
    target = _target(root)
    users = target.with_name(target.name + ".tmp")
    users.write_text("MINE\n", encoding="utf-8")
    rc, err = run_caller(name, monkeypatch, root, target)
    assert (rc, target.read_text(encoding="utf-8")) == (0, NEW), err
    assert users.read_text(encoding="utf-8") == "MINE\n"


@pytest.mark.parametrize("name", _NAMES)
def test_an_unwritable_directory_is_rc0_in_place_with_a_warn(name, root, monkeypatch):
    """#2128's one behaviour change: rc 1 + traceback before, rc 0 + WARN now."""
    target = _target(root)
    _refuse_mkstemp_beside(monkeypatch, target)
    rc, err = run_caller(name, monkeypatch, root, target)
    assert (rc, target.read_text(encoding="utf-8")) == (0, NEW), err
    assert "WARN" in err and "writing it in place" in err, err
    assert "Traceback" not in err, err


@pytest.mark.parametrize("name", _NAMES)
def test_a_failing_replace_is_rc2_one_line_naming_the_target(name, root, monkeypatch):
    """An error nobody falls back from (EIO on the rename): rc 2, one ERROR
    line naming the TARGET — not the tmp, whose name the attribution rule in
    ``_lib_io.output_write`` would not accept — with the caller's flag, and
    the previous file intact."""
    target = _target(root)
    real_replace = os.replace
    where = os.path.realpath(target)
    fired = []

    def replace(src, dst, *a, **k):
        if os.path.realpath(dst) == where:
            fired.append(src)
            raise OSError(errno.EIO, "Input/output error (simulated)", src, dst)
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(os, "replace", replace)
    rc, err = run_caller(name, monkeypatch, root, target)
    print(f"fired={bool(fired)}")
    assert fired, "the injected failure never ran"
    assert rc == 2, err
    assert "Traceback" not in err, err
    assert f"ERROR: cannot write {target}:" in err, err
    assert ".tmp" not in err, err
    flag = CALLERS[name].flag
    if flag is None:
        assert "check the value given to" not in err, err
    else:
        assert f"check the value given to {flag}" in err, err
    assert target.read_text(encoding="utf-8") == OLD
    assert sorted(p.name for p in target.parent.iterdir()) == ["target.md"]


@pytest.mark.parametrize("name", _NAMES)
def test_enospc_mid_write_is_rc2_and_keeps_the_previous_file(name, root, monkeypatch):
    """The Trap #60 / #2082 fault: the byte stream stops part-way. Injected on
    the handle the helper writes the tmp through (``os.fdopen``)."""
    target = _target(root)
    real_fdopen = os.fdopen
    where = os.path.realpath(target.parent)
    fired = []

    class _Half:
        def __init__(self, fh):
            self._fh = fh

        def write(self, s):
            self._fh.write(s[: len(s) // 2])
            self._fh.flush()
            fired.append(True)
            raise OSError(errno.ENOSPC, "No space left on device (simulated)")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._fh.close()
            return False

        def __getattr__(self, attr):
            return getattr(self._fh, attr)

    def fdopen(fd, mode="r", *a, **k):
        # The helper's tmp is the fd's inode among `where`'s entries — found
        # by fstat, not /proc/self/fd, so this runs where /proc does not.
        # ``os.stat(e.path)``, not ``e.stat()``: on Windows DirEntry.stat()
        # reports st_dev == st_ino == 0 (the directory listing carries no file
        # id), so the pair never matched and the fault was never injected.
        # os.stat asks the file itself; on POSIX it is the same lstat.
        st = os.fstat(fd)

        def _ident(entry):
            s = os.stat(entry.path, follow_symlinks=False)
            return (s.st_dev, s.st_ino)

        in_where = any(_ident(e) == (st.st_dev, st.st_ino)
                       for e in os.scandir(where))
        fh = real_fdopen(fd, mode, *a, **k)
        if "w" in mode and in_where:
            return _Half(fh)
        return fh

    monkeypatch.setattr(os, "fdopen", fdopen)
    rc, err = run_caller(name, monkeypatch, root, target)
    print(f"fired={bool(fired)}")
    assert fired, "the injected failure never ran"
    assert rc == 2, err
    assert "Traceback" not in err, err
    assert f"cannot write {target}:" in err and "full or over quota" in err, err
    assert "previous file is unchanged" in err, err
    assert target.read_text(encoding="utf-8") == OLD
    assert sorted(p.name for p in target.parent.iterdir()) == ["target.md"]


def test_the_caller_table_is_the_surveyed_population():
    """Anti-vacuity for the parametrization: the eight #2128 sites, each at a
    line that still calls the helper — so a moved call is noticed here rather
    than the table silently testing a neighbour."""
    assert len(CALLERS) == 8
    for caller in CALLERS.values():
        rel, line = caller.site.rsplit(":", 1)
        src = (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()
        window = "\n".join(src[int(line) - 1: int(line) + 12])
        assert "atomic_write_text(" in window, caller.site
