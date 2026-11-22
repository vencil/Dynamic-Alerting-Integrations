"""Every generator that rewrites a repo artifact in place (#2128, #3981).

They all reach the shared ``atomic_write_text`` through its entry,
``_lib_io.write_repo_artifact``, since #3981.

The helper now writes symlinks through, keeps hard links and a user's
``<out>.tmp``, and falls back in place (with a WARN) when the directory
cannot take a temp file. Those are helper properties, pinned in
``test_atomic_write.py``; what is pinned HERE is that each of the call
sites actually gets them end to end, and that a write failure leaves the
tool as rc 2 plus one line naming the target — not a traceback at rc 1.

Each call site is driven through its real ``main`` with only its INPUTS
stubbed (what to render, where the target is), so the write path — the
artifact entry, the helper, ``exit_on_output_write_error``
— is the code that ships. ``CALLERS`` holds the sites (#2128's eight, less
``audit_rules_drift`` since #3982); every test is
parametrized over all of them.

The adapters are module-level on purpose: the same scenarios can be replayed
against another checkout of the tools (the #2128 before/after table was
measured that way).
"""
from __future__ import annotations

import ast
import contextlib
import errno
import importlib
import io
import os
import signal
import stat
import subprocess
import sys
import tempfile
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import pytest
from _platform_fs import require_posix_modes, symlink_or_skip  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS = REPO_ROOT / "scripts" / "tools"
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
    run.argv = argv
    return run


def _setup_doc_map(mod, mp, root, target):
    mp.setattr(mod, "REPO_ROOT", root)
    mp.setattr(mod, "_get_map_path", lambda lang: target)
    mp.setattr(mod, "generate_doc_map", lambda lang, include_adr=True: NEW)
    return _argv_runner(mod, mp, ["generate_doc_map.py", "--generate", "--lang", "zh"])


def _setup_tool_map(mod, mp, root, target):
    mp.setattr(mod, "REPO_ROOT", root)
    mp.setattr(mod, "TOOL_MAP", target)
    mp.setattr(mod, "gather_tools", lambda: {c: [] for c in mod.CATEGORY_ORDER})
    mp.setattr(mod, "generate_tool_map", lambda categorized, lang: NEW)
    return _argv_runner(mod, mp, ["generate_tool_map.py", "--generate", "--lang", "zh"])


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
    mp.setattr(mod, "status_mismatches", lambda entries: [])
    mp.setattr(mod, "render_table", lambda entries, prefix="adr/", lang="zh": "")
    mp.setattr(mod, "replace_sentinel_block", lambda cur, table, tgt=None: NEW)
    return _argv_runner(mod, mp, ["generate_adr_index.py", "--write", "--target", str(target)])


def _setup_planning_index(mod, mp, root, target):
    mp.setattr(mod, "discover_all", lambda repo_root: [])
    mp.setattr(mod, "render_index", lambda entries: "")
    mp.setattr(mod, "replace_sentinel_block", lambda cur, body: NEW)
    return _argv_runner(mod, mp, ["generate_planning_index.py", "--write",
                                  "--repo-root", str(root), "--target", str(target)])


# ── #3981, second batch: five tools that rewrote a tracked file with a bare
# `open(w)` / `write_text`. Each is driven at one target; what the tool would
# compute is stubbed so that the write is OLD -> NEW.

def _setup_rule_pack_readme(mod, mp, root, target):
    mp.setattr(mod, "generate_table_rows", lambda d: ([], 0, 0))
    mp.setattr(mod, "count_preloaded_packs", lambda d: 0)
    mp.setattr(mod, "generate_readme_content", lambda rows, records, alerts, packs: NEW)
    mp.setattr(mod, "_readme_path", lambda d: target)
    return _argv_runner(mod, mp, ["generate_rule_pack_readme.py", "--update",
                                  "--rule-packs-dir", str(root)])


def _alert_reference_common(mod, mp, root):
    aux = root / "aux"
    aux.mkdir(exist_ok=True)
    other = aux / "other.md"
    other.write_text("UNCHANGED\n", encoding="utf-8")
    mp.setattr(mod, "load_rule_packs", lambda d: {"zpack": []})
    mp.setattr(mod, "generate_markdown_zh", lambda alerts: NEW)
    mp.setattr(mod, "generate_markdown_en", lambda alerts: NEW)
    return other, _argv_runner(mod, mp, ["generate_alert_reference.py",
                                         "--output-dir", str(root)])


def _setup_alert_reference_zh(mod, mp, root, target):
    other, run = _alert_reference_common(mod, mp, root)
    mp.setattr(mod, "_reference_paths", lambda d: (str(target), str(other)))
    return run


def _setup_alert_reference_en(mod, mp, root, target):
    other, run = _alert_reference_common(mod, mp, root)
    mp.setattr(mod, "_reference_paths", lambda d: (str(other), str(target)))
    return run


def _setup_repo_name(mod, mp, root, target):
    mp.setattr(mod, "iter_scan_targets", lambda full_scan, base: [(str(target), "target.md")])
    mp.setattr(mod, "_line_violates", lambda line: True)
    mp.setattr(mod, "WRONG_REPO_URL", OLD.strip())
    mp.setattr(mod, "CORRECT_REPO_URL", NEW.strip())
    mp.setattr(mod, "_read_pr_body", lambda path: "")
    return _argv_runner(mod, mp, ["check_repo_name.py", "--full-scan", "--fix"])


def _setup_add_frontmatter(mod, mp, root, target):
    mp.setattr(mod, "find_markdown_files", lambda base: [str(target)])
    mp.setattr(mod, "has_frontmatter", lambda path: False)
    mp.setattr(mod, "extract_version", lambda path, base: "v0")
    mp.setattr(mod, "generate_frontmatter", lambda *a, **k: "")
    mp.setattr(mod, "read_file_content", lambda path: NEW)
    return _argv_runner(mod, mp, ["add_frontmatter.py", "--base-dir", str(root)])


def _setup_schema_json(mod, mp, root, target):
    """gen_tenant_id_json / gen_am_duration_json: one copy, at *target*."""
    (root / "schema.json").write_text("{}", encoding="utf-8")
    mp.setattr(mod, "_repo_root", lambda: root)
    mp.setattr(mod, "SCHEMA_REL", "schema.json")
    mp.setattr(mod, "OUT_RELS", (target.relative_to(root).as_posix(),))
    mp.setattr(mod, "render", lambda schema: NEW)
    return _argv_runner(mod, mp, [f"{mod.__name__}.py"])


def _setup_reserved_key_lists(mod, mp, root, target):
    """sync_schema --update: one generated target, named by its absolute
    path (so the message names it as the other callers do)."""
    mp.setattr(mod, "load_registry", lambda path: {})
    mp.setattr(mod, "derive", lambda reg: {})
    mp.setattr(mod, "targets", lambda root_, d: [(str(target), lambda text: NEW)])
    return _argv_runner(mod, mp, ["sync_schema.py", "--update", "--root", str(root)])


def _setup_recipe_contract(mod, mp, root, target):
    """gen_recipe_contract --update: one generated copy, as above."""
    mp.setattr(mod, "load", lambda root_: (None, None))
    # A whole-file copy (the portal data files): the one kind that may be
    # absent, and whose directory the tool then makes.
    mp.setattr(mod, "WHOLE_FILE_TARGETS", frozenset({str(target)}))
    mp.setattr(mod, "targets", lambda root_, c, grammar: [(str(target), lambda text: NEW)])
    return _argv_runner(mod, mp, ["gen_recipe_contract.py", "--update", "--root", str(root)])


CALLERS: dict[str, Caller] = {
    "tenant_id_json": Caller(
        "gen_tenant_id_json", "scripts/tools/dx/gen_tenant_id_json.py:135",
        None, _setup_schema_json),
    "am_duration_json": Caller(
        "gen_am_duration_json", "scripts/tools/dx/gen_am_duration_json.py:138",
        None, _setup_schema_json),
    "rule_pack_readme": Caller(
        "generate_rule_pack_readme", "scripts/tools/dx/generate_rule_pack_readme.py:361",
        None, _setup_rule_pack_readme),
    "alert_reference_zh": Caller(
        "generate_alert_reference", "scripts/tools/dx/generate_alert_reference.py:560",
        None, _setup_alert_reference_zh),
    "alert_reference_en": Caller(
        "generate_alert_reference", "scripts/tools/dx/generate_alert_reference.py:562",
        None, _setup_alert_reference_en),
    "repo_name_fix": Caller(
        "check_repo_name", "scripts/tools/lint/check_repo_name.py:127",
        None, _setup_repo_name),
    "add_frontmatter": Caller(
        "add_frontmatter", "scripts/tools/dx/add_frontmatter.py:272",
        None, _setup_add_frontmatter),
    "rule_pack_stats_fragment": Caller(
        "generate_rule_pack_stats", "scripts/tools/dx/generate_rule_pack_stats.py:445",
        None, _setup_rule_pack_stats_fragment),
    "rule_pack_stats_inject": Caller(
        "generate_rule_pack_stats", "scripts/tools/dx/generate_rule_pack_stats.py:448",
        None, _setup_rule_pack_stats_inject),
    "tool_map": Caller(
        "generate_tool_map", "scripts/tools/dx/generate_tool_map.py:370", None,
        _setup_tool_map),
    "doc_map": Caller(
        "generate_doc_map", "scripts/tools/dx/generate_doc_map.py:506", None,
        _setup_doc_map),
    "byo_rulepack_table": Caller(
        "generate_byo_rulepack_table", "scripts/tools/dx/generate_byo_rulepack_table.py:284",
        None, _setup_byo),
    "adr_index": Caller(
        "generate_adr_index", "scripts/dx/generate_adr_index.py:370", "--target",
        _setup_adr_index),
    "planning_index": Caller(
        "generate_planning_index", "scripts/dx/generate_planning_index.py:525", "--target",
        _setup_planning_index),
    # #3981, fourth batch: both write through sync_schema.write_targets.
    "reserved_key_lists": Caller(
        "sync_schema", "scripts/tools/dx/sync_schema.py:667", None,
        _setup_reserved_key_lists),
    "recipe_contract": Caller(
        "gen_recipe_contract", "scripts/tools/dx/sync_schema.py:667", None,
        _setup_recipe_contract),
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


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("mode", [0o755, 0o644])
def test_the_execute_bit_is_kept_and_nothing_else_of_the_mode(name, mode, root, monkeypatch):
    """#3981: a generator passes no ``mode=`` — the entry keeps the execute
    bit of the file it replaces and does not read the umask."""
    require_posix_modes()
    target = _target(root)
    os.chmod(target, mode)
    old = os.umask(0o077)
    try:
        rc, err = run_caller(name, monkeypatch, root, target)
    finally:
        os.umask(old)
    assert (rc, target.read_text(encoding="utf-8")) == (0, NEW), err
    assert stat.S_IMODE(os.stat(target).st_mode) == mode


# …and, second batch, the generators whose bare `open(w)` blocked on the FIFO
# itself: the artifact entry refuses the target before opening it.
_READS_ITS_TARGET_FIRST = ["rule_pack_stats_fragment", "rule_pack_stats_inject",
                           "byo_rulepack_table", "tool_map", "doc_map",
                           "rule_pack_readme", "alert_reference_zh", "alert_reference_en",
                           # third batch: their bare write blocked on the FIFO
                           "tenant_id_json", "am_duration_json",
                           # fourth batch: their read of the target blocked
                           "reserved_key_lists", "recipe_contract"]


@pytest.mark.parametrize("name", _READS_ITS_TARGET_FIRST)
def test_a_fifo_target_is_rc2_before_anything_reads_it(name, root, monkeypatch):
    """#3981: the two injectors read the document they rewrite; a FIFO there
    blocked the read forever. An alarm stands in for the hang."""
    if not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"):
        pytest.skip("no FIFO / SIGALRM on this platform")
    target = _target(root)
    target.unlink()
    os.mkfifo(target)

    def hung(signum, frame):
        raise AssertionError("blocked on the FIFO")

    prev = signal.signal(signal.SIGALRM, hung)
    signal.alarm(20)
    try:
        rc, err = run_caller(name, monkeypatch, root, target)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)
    assert rc == 2 and "Traceback" not in err, err
    assert str(target) in err and len(err.strip().splitlines()) == 1, err
    assert stat.S_ISFIFO(os.stat(target).st_mode)


@pytest.mark.parametrize("name", ["tenant_id_json", "am_duration_json",
                                  "reserved_key_lists", "recipe_contract"])
def test_a_fifo_target_under_check_is_rc2_before_anything_reads_it(name, root, monkeypatch):
    """#3981, fourth batch: the four `--check` hooks compared the target's
    bytes with what they would generate — a read that blocked forever on a
    FIFO (rc 124 under `timeout`). The target is judged first now."""
    if not hasattr(os, "mkfifo") or not hasattr(signal, "SIGALRM"):
        pytest.skip("no FIFO / SIGALRM on this platform")
    target = _target(root)
    target.unlink()
    os.mkfifo(target)
    caller = CALLERS[name]
    run = caller.setup(importlib.import_module(caller.module), monkeypatch, root, target)
    run.argv[:] = [a for a in run.argv if a != "--update"] + ["--check"]
    assert "--check" in run.argv and "--update" not in run.argv, run.argv

    def hung(signum, frame):
        raise AssertionError("blocked on the FIFO")

    prev = signal.signal(signal.SIGALRM, hung)
    signal.alarm(20)
    err = io.StringIO()
    try:
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            rc = run()
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, prev)
    assert rc == 2 and str(target) in err.getvalue(), err.getvalue()
    assert len(err.getvalue().strip().splitlines()) == 1, err.getvalue()
    assert stat.S_ISFIFO(os.stat(target).st_mode)


def test_a_refused_english_reference_leaves_the_chinese_one_alone(root, monkeypatch):
    """#3981: the two references are one regeneration. Both targets are judged
    before either is written — the Chinese file used to be rewritten and the
    run then failed on the English one, leaving the pair out of step."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFO on this platform")
    target = _target(root)
    target.unlink()
    os.mkfifo(target)
    rc, err = run_caller("alert_reference_en", monkeypatch, root, target)
    assert rc == 2 and str(target) in err, err
    assert (root / "aux" / "other.md").read_text(encoding="utf-8") == "UNCHANGED\n"


@pytest.mark.parametrize("name", ["reserved_key_lists", "recipe_contract"])
def test_a_refused_target_leaves_the_stale_one_before_it_alone(name, root, monkeypatch):
    """#3981, fourth batch (independent review): `--update` judges every
    target before the first write. A stale target first and a FIFO second:
    the run is refused, and the stale one keeps its bytes — with the write
    inside the read loop it was already rewritten."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFO on this platform")
    stale = _target(root)
    fifo = root / "out" / "second.md"
    os.mkfifo(fifo)
    caller = CALLERS[name]
    mod = importlib.import_module(caller.module)
    run = caller.setup(mod, monkeypatch, root, stale)
    pair = [(str(stale), lambda text: NEW), (str(fifo), lambda text: NEW)]
    if name == "reserved_key_lists":
        monkeypatch.setattr(mod, "targets", lambda root_, d: pair)
    else:
        monkeypatch.setattr(mod, "targets", lambda root_, c, grammar: pair)
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        rc = run()
    assert rc == 2 and str(fifo) in err.getvalue(), err.getvalue()
    assert stale.read_text(encoding="utf-8") == OLD


@pytest.mark.parametrize("script", ["sync_schema.py", "gen_recipe_contract.py",
                                    "gen_tenant_id_json.py", "gen_am_duration_json.py"])
def test_check_with_a_target_as_stdin_keeps_its_answer(script):
    """#3981, fourth batch (independent review): `--check` only reads its
    targets, so one that is also this process's stdin is still compared —
    rc 0 on the in-sync repository, as before. Only a target `--update`
    replaces must not be a standard stream."""
    mod = importlib.import_module(script[:-3])
    rel = next(iter(getattr(mod, "OUT_RELS", None) or sorted(mod.WHOLE_FILE_TARGETS)))
    with open(REPO_ROOT / rel, "rb") as stdin:
        done = subprocess.run([sys.executable, str(TOOLS / "dx" / script), "--check"],
                              cwd=REPO_ROOT, stdin=stdin, capture_output=True, text=True,
                              encoding="utf-8", timeout=120)
    assert done.returncode == 0, (rel, done.stdout, done.stderr)


@pytest.mark.parametrize("name", ["rule_pack_stats_fragment", "tenant_id_json",
                                  "am_duration_json", "recipe_contract"])
def test_the_target_directory_is_created_when_absent(name, root, monkeypatch):
    """The generators that made the directory of a copy they add (a new
    portal data file) still do."""
    target = root / "fresh" / "deeper" / "target.md"
    rc, err = run_caller(name, monkeypatch, root, target)
    assert (rc, target.read_text(encoding="utf-8")) == (0, NEW), err


def test_a_refused_copy_leaves_the_other_copies_alone(root, monkeypatch):
    """#3981, third batch: `gen_tenant_id_json` writes two copies. With the
    second one no regular file, the first was already rewritten when the
    write of the second failed (or blocked); every copy is judged first."""
    if not hasattr(os, "mkfifo"):
        pytest.skip("no FIFO on this platform")
    first = _target(root)
    second = root / "out" / "second.json"
    os.mkfifo(second)
    mod = importlib.import_module("gen_tenant_id_json")
    run = _setup_schema_json(mod, monkeypatch, root, first)
    monkeypatch.setattr(mod, "OUT_RELS", ("out/target.md", "out/second.json"))
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        rc = run()
    assert rc == 2 and str(second) in err.getvalue(), err.getvalue()
    assert first.read_text(encoding="utf-8") == OLD


@pytest.mark.parametrize("name", ["tool_map", "doc_map"])
def test_the_safe_flag_is_a_usage_error(name, root, monkeypatch):
    """#3981: ``--safe`` is gone from both map generators — argparse refuses
    it (rc 2) and the map is not rewritten."""
    target = _target(root)
    caller = CALLERS[name]
    mod = importlib.import_module(caller.module)
    # setup() stubs the inputs; the runner it returns is what sets argv, and
    # is not called — the argv here is that runner's own (so a generator that
    # took the flag again would write the stubbed target, nothing real), plus
    # the flag.
    run = caller.setup(mod, monkeypatch, root, target)
    monkeypatch.setattr(sys, "argv", [*run.argv, "--safe"])
    err = io.StringIO()
    with pytest.raises(SystemExit) as exc, contextlib.redirect_stderr(err):
        mod.main()
    assert exc.value.code == 2
    assert "--safe" in err.getvalue(), err.getvalue()
    assert target.read_text(encoding="utf-8") == OLD


def _modules_loaded_with(module: str) -> set[str]:
    """The names in ``sys.modules`` after importing *module* in a fresh
    interpreter — what it cannot be imported without."""
    code = (f"import sys; sys.path[:0] = {[str(TOOLS), str(TOOLS / 'dx')]!r}; "
            f"import {module}; print('\\n'.join(sorted(sys.modules)))")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          encoding="utf-8", stdin=subprocess.DEVNULL, timeout=120)
    assert done.returncode == 0, done.stderr
    return set(done.stdout.split())


@pytest.mark.parametrize("module, constant", [("sync_schema", "PY_REL"),
                                              ("gen_recipe_contract", "CONSTANTS_REL")])
def test_a_generator_never_loads_what_loads_the_file_it_repairs(module, constant):
    """#3981, third batch (blind review): `sync_schema --update` and
    `gen_recipe_contract --update` rewrite a generated block of
    `_lib_constants.py`. `_lib_io` imports that file — so with the artifact
    entry taken from it, a block left broken (a merge conflict in it) made
    `--update` die on the import, and it is `--update` that repairs it.
    #4223 moved the entries to `_lib_output`, which loads neither; the
    fourth batch takes them from there, and this says no import of theirs
    loads the file back."""
    mod = importlib.import_module(module)
    target = getattr(mod, constant)
    assert target == "scripts/tools/_lib_constants.py", target
    # Anti-vacuity: the measurement sees a module that does load it.
    assert "_lib_constants" in _modules_loaded_with("_lib_io"), (
        "_lib_io no longer loads _lib_constants: the measurement is unproven")
    # Measured, not read off the imports (independent review): a direct
    # `import _lib_constants`, or one through a sibling tool such as
    # sync_schema, is caught as well as one through a `_lib_*` module.
    loaded = sorted(m for m in _modules_loaded_with(module)
                    if m == "_lib_constants" or m.startswith("_lib_io"))
    assert not loaded, (
        f"importing {module} loads {loaded}: _lib_constants.py is the file its "
        "--update repairs")


def test_the_caller_table_is_the_surveyed_population():
    """Anti-vacuity for the parametrization: the #2128 sites, each at a
    line that still calls the helper — so a moved call is noticed here rather
    than the table silently testing a neighbour. #3107: a committed artifact
    may reach the helper through its entry point, `_lib_io.write_repo_artifact`.
    #3982 (owner 2026-11-18): `audit_rules_drift --out` is an operator
    output now and left the table (seven of the eight remain). #3981, second
    batch: five more sites that rewrote a tracked file with a bare write;
    third batch: two generators of committed copies of a schema definition;
    fourth batch: sync_schema and gen_recipe_contract."""
    assert len(CALLERS) == 16
    for caller in CALLERS.values():
        rel, line = caller.site.rsplit(":", 1)
        src = (REPO_ROOT / rel).read_text(encoding="utf-8").splitlines()
        window = "\n".join(src[int(line) - 1: int(line) + 12])
        assert "atomic_write_text(" in window or "write_repo_artifact(" in window, caller.site
