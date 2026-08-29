"""Unit tests for `_lib_confd` — the single answer to "what is in a conf.d" (#1339)."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts" / "tools"))

from _lib_confd import (  # noqa: E402
    iter_config_files,
    nested_yaml_files,
    nested_yaml_warning,
    reset_warned_for_test,
    unusable_config_entries,
    unusable_config_paths,
    unusable_reason,
    warn_nested,
)


@pytest.fixture()
def hierarchical(tmp_path: pathlib.Path) -> pathlib.Path:
    """The shape ADR-016 describes and the exporter actually walks."""
    root = tmp_path / "conf.d"
    (root / "finance" / "us-east" / "prod").mkdir(parents=True)
    (root / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n", encoding="utf-8")
    (root / "finance" / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 90\n", encoding="utf-8")
    (root / "finance" / "us-east" / "prod" / "tenant-a.yaml").write_text(
        'tenants:\n  tenant-a:\n    mysql_connections: "95"\n', encoding="utf-8")
    return root


@pytest.fixture()
def flat(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "flat"
    root.mkdir()
    (root / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n", encoding="utf-8")
    (root / "tenant-a.yaml").write_text('tenants:\n  tenant-a:\n    mysql_connections: "95"\n', encoding="utf-8")
    return root


def test_iter_reads_the_whole_tree(hierarchical: pathlib.Path):
    got = [p.relative_to(hierarchical).as_posix() for p in iter_config_files(hierarchical)]
    assert got == [
        "_defaults.yaml",
        "finance/_defaults.yaml",
        "finance/us-east/prod/tenant-a.yaml",
    ]


def test_iter_order_is_posix_and_platform_stable(hierarchical: pathlib.Path):
    """Sorting on the POSIX relative path, not the OS-native one.

    #1341 lost a day to exactly this: a Windows-side regeneration wrote
    `db\\mariadb\\prod\\x.yaml` into a fixture and turned a parity test red
    on path strings alone, with no semantic change.
    """
    got = [p.relative_to(hierarchical).as_posix() for p in iter_config_files(hierarchical)]
    assert got == sorted(got)
    assert not any("\\" in g for g in got)


def test_iter_non_recursive_is_the_old_flat_behaviour(hierarchical: pathlib.Path):
    got = [p.name for p in iter_config_files(hierarchical, recursive=False)]
    assert got == ["_defaults.yaml"]


def test_iter_missing_dir_is_empty_not_an_error(tmp_path: pathlib.Path):
    assert list(iter_config_files(tmp_path / "nope")) == []


def test_nested_files_are_exactly_what_a_flat_reader_misses(hierarchical, flat):
    missed = [p.name for p in nested_yaml_files(hierarchical)]
    assert sorted(missed) == ["_defaults.yaml", "tenant-a.yaml"]
    assert nested_yaml_files(flat) == []


def test_warning_is_none_on_a_flat_dir(flat: pathlib.Path):
    """The guard must be silent when it has nothing to say.

    A warning that fires on every flat conf.d would be trained away in a
    week, and every real deployment today is flat.
    """
    assert nested_yaml_warning(flat, tool="unit-test") is None


def test_warning_names_the_skipped_files(hierarchical: pathlib.Path):
    msg = nested_yaml_warning(hierarchical, tool="unit-test")
    assert msg is not None
    # Actionable: which tool, how many, and which files — not just "hmm".
    assert "unit-test" in msg
    assert "2 config file(s)" in msg
    assert "finance/us-east/prod/tenant-a.yaml" in msg
    assert "#1339" in msg


def test_warning_truncates_but_says_how_many(tmp_path: pathlib.Path):
    root = tmp_path / "many"
    (root / "sub").mkdir(parents=True)
    for i in range(9):
        (root / "sub" / f"t{i}.yaml").write_text("tenants: {}\n", encoding="utf-8")
    msg = nested_yaml_warning(root, tool="unit-test", limit=5)
    assert "9 config file(s)" in msg
    assert "+4 more" in msg, "a truncated list must still report the true total"


def test_hidden_entries_are_skipped_like_the_exporter(tmp_path: pathlib.Path):
    """Skip rule is DERIVED from `startswith(".")`, not an allowlist of names.

    `pkg/config/hierarchy.go` skips any dot-prefixed entry — directories via
    `fs.SkipDir`, files outright — and every flat Python reader already does
    `not f.startswith(".")`. An enumerated allowlist (`.git`,
    `__pycache__`, ...) would make this module disagree with the oracle it
    exists to mirror, which is the whole defect #1339 is about.
    """
    root = tmp_path / "conf.d"
    (root / ".hidden").mkdir(parents=True)
    (root / "real").mkdir()
    (root / "tenant-a.yaml").write_text("tenants: {}\n", encoding="utf-8")
    (root / ".backup.yaml").write_text("tenants: {}\n", encoding="utf-8")
    (root / ".hidden" / "buried.yaml").write_text("tenants: {}\n", encoding="utf-8")
    (root / "real" / "tenant-b.yaml").write_text("tenants: {}\n", encoding="utf-8")

    got = [p.relative_to(root).as_posix() for p in iter_config_files(root)]
    assert got == ["real/tenant-b.yaml", "tenant-a.yaml"]
    # ...and the guard must not advertise files nobody would have read anyway.
    assert [p.name for p in nested_yaml_files(root)] == ["tenant-b.yaml"]


def test_order_is_lexicographic_not_depth_first(tmp_path: pathlib.Path):
    """Counter-example pinning what the order actually is (PR #1343 review).

    The docstring once promised "deepest path last". Lexicographic order
    never guaranteed that — a nested `a/...` sorts before a root-level
    `z.yaml` — and the original fixtures happened not to show it. Pin the
    real contract so nobody builds on the wrong one.
    """
    root = tmp_path / "conf.d"
    (root / "a").mkdir(parents=True)
    (root / "a" / "tenant.yaml").write_text("tenants: {}\n", encoding="utf-8")
    (root / "z.yaml").write_text("tenants: {}\n", encoding="utf-8")

    got = [p.relative_to(root).as_posix() for p in iter_config_files(root)]
    assert got == ["a/tenant.yaml", "z.yaml"], "nested-but-alphabetically-first comes first"
    assert got != ["z.yaml", "a/tenant.yaml"], "this is NOT shallow-first ordering"


def test_shared_helpers_name_the_entry_point_not_themselves(monkeypatch, tmp_path):
    """`tool=` omitted → the message names the command the operator ran.

    A shared helper (`_lib_io.iter_yaml_files`) is reached from several
    entry points. Labelling its warning with the helper's own name tells
    the operator nothing about which command produced it — the
    traceability concern CodeRabbit raised on PR #1343.
    """
    root = tmp_path / "conf.d"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "t.yaml").write_text("tenants: {}\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", ["/some/where/offboard_tenant.py", "db-a"])
    assert nested_yaml_warning(root).startswith("offboard_tenant:")

    # An explicit name still wins, for single-purpose tools.
    assert nested_yaml_warning(root, tool="explicit").startswith("explicit:")


def test_derived_tool_name_survives_a_missing_argv(monkeypatch, tmp_path):
    """Never crash the caller just to produce a label."""
    root = tmp_path / "conf.d"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "t.yaml").write_text("tenants: {}\n", encoding="utf-8")

    monkeypatch.setattr(sys, "argv", [])
    msg = nested_yaml_warning(root)
    assert msg and "conf.d reader" in msg


# ── warn_nested: says it once, and only to stderr ──────────────────────


def test_warn_nested_prints_once_per_directory(capsys, hierarchical):
    """One command often scans the same conf.d twice; say it once.

    `validate_config.py` reaches the routing parser twice and
    `migrate_to_operator.analyze_migration` both scans directly and calls
    `discover_tenant_configs` — printing the identical WARN twice reads as
    two separate problems (CodeRabbit, PR #1343). Note the fix is here, not
    "delete one of the guards": a scan whose only warning lives in another
    function is the gate-never-fires shape this module exists against.
    """
    reset_warned_for_test()
    assert warn_nested(hierarchical) is True
    assert warn_nested(hierarchical) is False, "second call must stay quiet"
    err = capsys.readouterr().err
    assert err.count("live in subdirectories") == 1


def test_warn_nested_writes_only_to_stderr(capsys, hierarchical):
    """stdout must stay parseable — several tools emit JSON on it."""
    reset_warned_for_test()
    warn_nested(hierarchical)
    cap = capsys.readouterr()
    assert cap.out == ""
    assert "WARN:" in cap.err


def test_warn_nested_is_silent_on_a_flat_dir(capsys, flat):
    reset_warned_for_test()
    assert warn_nested(flat) is False
    assert capsys.readouterr().err == ""


def test_reset_is_idempotent_not_save_restore(capsys, hierarchical):
    """Repo rule (CLAUDE.md): process-global state gets an idempotent CLEAR.

    A save-then-restore fixture is "last cleanup wins" and would undo a
    parallel test's writes.
    """
    reset_warned_for_test()
    reset_warned_for_test()  # calling twice must be harmless
    assert warn_nested(hierarchical) is True
    reset_warned_for_test()
    assert warn_nested(hierarchical) is True, "after reset it may warn again"


# ── #1469: the half that keeps the signal ──────────────────────────────
#
# Unifying the two readers' SELECTION (`_parse_config_files` now calls
# `iter_config_files(..., recursive=False)`) removes a divergence, but on
# its own it also removes the one message anybody was getting about a
# config-named directory. These pin the replacement.


@pytest.fixture()
def with_unusable(tmp_path: pathlib.Path) -> pathlib.Path:
    """A conf.d holding the shape #1469 was reported on."""
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "_defaults.yaml").write_text("defaults:\n  mysql_connections: 80\n",
                                         encoding="utf-8")
    (root / "acme.yaml").write_text(
        'tenants:\n  acme:\n    mysql_connections: "90"\n', encoding="utf-8")
    (root / "beta.yaml").mkdir()                    # interrupted mkdir / bad merge
    (root / "gamma.yaml").symlink_to(root / "nope.yaml")   # broken symlink
    (root / "notes.txt").write_text("not a config\n", encoding="utf-8")
    return root


def test_unusable_paths_names_directory_and_broken_symlink(with_unusable):
    got = [p.name for p in unusable_config_paths(with_unusable)]
    assert got == ["beta.yaml", "gamma.yaml"]


def test_unusable_paths_excludes_readable_files_and_non_config_names(with_unusable):
    got = {p.name for p in unusable_config_paths(with_unusable)}
    assert "_defaults.yaml" not in got and "acme.yaml" not in got
    # `notes.txt` is not config-shaped at all — it is not this list's business.
    assert "notes.txt" not in got


def test_unusable_paths_is_disjoint_from_iter_config_files(with_unusable):
    """The two lists partition the config-named entries — never overlap.

    An entry in both would mean a reader is told to read something the
    same module says it cannot read.

    ⛔ Two things in the original version of this test made it unable to
    fail, and the fixture it runs on contains the very entry it was blind
    to (`gamma.yaml`, a broken symlink):

      * it compared `p.resolve()`, which collapses a healthy symlink onto
        its target and could invent an overlap that is not one; and
      * it filtered the unusable side through `if p.exists()`, which drops
        every BROKEN symlink — and a broken symlink was the ONLY class that
        actually appeared in both lists. Measured on this fixture: raw name
        overlap `['gamma.yaml']`, the filtered assertion `True`.

    So it compares raw paths now, and nothing is filtered out.
    """
    readable = set(iter_config_files(with_unusable))
    unusable = set(unusable_config_paths(with_unusable))
    assert readable & unusable == set(), (
        f"reported twice: {sorted(p.name for p in readable & unusable)}")
    # And the broken symlink is on exactly one side, not neither.
    assert (with_unusable / "gamma.yaml") in unusable


def test_unusable_paths_respects_recursive_false(tmp_path: pathlib.Path):
    """Flat callers (the routing parser) must not inherit nested findings."""
    root = tmp_path / "conf.d"
    (root / "team-a").mkdir(parents=True)
    (root / "top.yaml").mkdir()
    (root / "team-a" / "deep.yaml").mkdir()
    assert [p.name for p in unusable_config_paths(root, recursive=False)] == ["top.yaml"]
    assert [p.relative_to(root).as_posix()
            for p in unusable_config_paths(root)] == ["team-a/deep.yaml", "top.yaml"]


def test_unusable_paths_skips_hidden_like_iter_config_files(tmp_path: pathlib.Path):
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / ".hidden.yaml").mkdir()
    assert unusable_config_paths(root) == []


def test_unusable_paths_on_missing_dir_is_empty(tmp_path: pathlib.Path):
    assert unusable_config_paths(tmp_path / "nope") == []


def test_unusable_reason_distinguishes_the_causes(with_unusable):
    assert "directory" in unusable_reason(with_unusable / "beta.yaml")
    assert "symlink" in unusable_reason(with_unusable / "gamma.yaml")


# ── `unusable_reason`'s remaining answers ────────────────────────────────
#
# This function exists to put ONE sentence in front of the operator, and
# coverage showed three of its four possible answers had no test: the
# permission clause, the stat-failure clause, and the fallback. A wrong or
# missing sentence here is the same defect class #1469 is about — the
# reader says something, and what it says is not what happened.


def test_unusable_reason_fallback_for_a_non_regular_file(tmp_path: pathlib.Path):
    """A FIFO is not a dir, not a broken symlink, and is readable — yet it
    is not a config file. That combination is the fallback clause, and it
    is reachable in the wild (a socket or device node dropped into conf.d
    by a bad mount)."""
    fifo = tmp_path / "queue.yaml"
    try:
        os.mkfifo(fifo)
    except (AttributeError, NotImplementedError, OSError) as e:
        pytest.skip(f"platform cannot create a FIFO here: {e}")

    assert unusable_reason(fifo) == "is not a readable file"
    # And it must be REPORTED, not silently skipped — the whole point.
    assert fifo in unusable_config_paths(tmp_path, recursive=False)


def test_unusable_reason_reports_a_stat_failure_instead_of_raising(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
):
    """If the stat itself fails, the reason must carry the errno text.

    ⛔ The alternative — letting OSError escape — is exactly the shape
    #1469's sibling defect had: a reader that dies instead of naming the
    file it could not read.
    """
    target = tmp_path / "weird.yaml"
    target.write_text("a: 1\n", encoding="utf-8")

    def boom(self):  # noqa: ANN001
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(pathlib.Path, "is_dir", boom)

    reason = unusable_reason(target)
    assert "could not be stat" in reason
    assert "OSError" in reason
    assert "Input/output error" in reason


def test_is_readable_file_survives_a_stat_failure(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
):
    """The selection helper must answer False, not raise, when stat fails —
    otherwise one bad inode takes down the whole enumeration."""
    target = tmp_path / "weird.yaml"
    target.write_text("a: 1\n", encoding="utf-8")

    def boom(self):  # noqa: ANN001
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(pathlib.Path, "is_file", boom)

    # Reached through the public entry point, not by importing the private
    # helper: what matters is that enumeration does not explode.
    assert unusable_config_paths(tmp_path, recursive=False) == [target]


def _deny_read(monkeypatch: pytest.MonkeyPatch, target: pathlib.Path) -> None:
    """Make one path unreadable without needing non-root.

    `chmod 000` is invisible to uid 0, and the container this suite is
    usually developed in runs as root — so the overlap this guards against
    would be untestable here. Patching `os.access` reproduces the exact
    condition `_is_readable_file` consults.
    """
    real = os.access

    def fake(path, mode, **kw):  # noqa: ANN001
        if os.fspath(path) == os.fspath(target) and mode == os.R_OK:
            return False
        return real(path, mode, **kw)

    monkeypatch.setattr(os, "access", fake)


def test_unusable_reason_names_permission_denied(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
):
    """The permission clause, exercised on EVERY runner including root.

    ⛔ This test used to exist only in the `chmod 000` form below, which is
    skipped under uid 0 — and that skip hid a contradiction between two
    tests in this file for two commits: one asserted an unreadable regular
    file IS in `unusable_config_paths`, the other (correctly) that it is
    not. Only the non-root CI runner ran both, so CI found it and the
    development container never could. A skip makes "untestable here"
    visible; it does not make it tested. Hence this monkeypatched twin.
    """
    locked = tmp_path / "locked.yaml"
    locked.write_text("a: 1\n", encoding="utf-8")
    _deny_read(monkeypatch, locked)
    assert unusable_reason(locked) == "is not readable (permission denied)"


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root bypasses the read permission bit, so os.access always returns True",
)
def test_unusable_reason_names_permission_denied_for_real(tmp_path: pathlib.Path):
    """Same clause via a real `chmod 000`, so the monkeypatched twin above
    is pinned against the genuine syscall on runners that can observe it.

    ⚠️ Skipped when running as root — and this test exists partly to make
    that skip VISIBLE. The container this suite is usually developed in runs
    as uid 0, where the clause is unreachable; CI runners do not, so the
    branch is exercised there rather than silently never.
    """
    locked = tmp_path / "locked.yaml"
    locked.write_text("a: 1\n", encoding="utf-8")
    locked.chmod(0o000)
    try:
        assert unusable_reason(locked) == "is not readable (permission denied)"
        # ⛔ NOT in `unusable_config_paths`, and that is the contract rather
        # than an omission: an unreadable REGULAR file is still a file, so
        # `iter_config_files` hands it to the reader and the reader's own
        # `open()` names it — with the real errno. Listing it here as well
        # would report it twice; see `test_the_two_enumerations_are_disjoint`,
        # which pins the same partition through the monkeypatched route.
        names = {q.name for q in unusable_config_paths(tmp_path, recursive=False)}
        assert "locked.yaml" not in names
        assert "locked.yaml" in {
            q.name for q in iter_config_files(tmp_path, recursive=False)}
    finally:
        locked.chmod(0o600)


# ── the two enumerations must not overlap ────────────────────────────────


@pytest.mark.parametrize("recursive", [False, True])
def test_the_two_enumerations_are_disjoint(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, recursive: bool,
):
    """An unreadable REGULAR file must not appear in both lists.

    ⛔ Callers consume the two together, so anything in both is reported
    twice: `validate-config` put it in `unusable_files` twice, and the
    routing parser and `diagnose` printed the permission clause and then
    the `PermissionError`. Measured before the fix — both lists returned
    `locked.yaml`.

    The unreadable file is NOT lost by narrowing this: `iter_config_files`
    still yields it, and the reader's own `open()` names it, with the real
    errno rather than a one-clause summary.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "_defaults.yaml").write_text("defaults:\n  a: 1\n", encoding="utf-8")
    locked = root / "locked.yaml"
    locked.write_text("tenants: {}\n", encoding="utf-8")
    (root / "beta.yaml").mkdir()  # the case unusable_config_paths is FOR

    _deny_read(monkeypatch, locked)

    usable = {p.name for p in iter_config_files(root, recursive=recursive)}
    unusable = {p.name for p in unusable_config_paths(root, recursive=recursive)}

    assert usable & unusable == set(), (
        f"overlap would be double-reported: {sorted(usable & unusable)}")
    # Still enumerated, so the reader's open() will speak for it.
    assert "locked.yaml" in usable
    # And the directory — which no open() can ever speak for — is still named.
    assert "beta.yaml" in unusable


# ── the recursive branch is not a second implementation ──────────────────


@pytest.mark.parametrize("recursive", [False, True])
def test_a_broken_symlink_is_on_exactly_one_side(
    tmp_path: pathlib.Path, recursive: bool,
):
    """⛔ The regression `test_the_two_enumerations_are_disjoint` could not see.

    That test parametrises `recursive` too, but its unusable entry is an
    unreadable REGULAR file — and `is_file()` is True for one in BOTH
    branches, so the asymmetry between them was invisible to it.

    The asymmetry was real: `iter_config_files`'s flat branch filtered on
    `p.is_file()` and its recursive branch filtered on nothing at all, so
    `os.walk` handed broken symlinks, FIFOs and symlink loops straight
    through while `unusable_config_paths` also collected them. Measured:

        recursive=True   OVERLAP: ['a.yaml', 'b.yaml']
        recursive=False  OVERLAP: []

    and end to end, `check_yaml_syntax` reported one `ghost.yaml` twice —
    once as `OSError: [Errno 40] Too many levels of symbolic links`, once
    as `is a broken symlink` — so its caveat line said "2 file(s)".
    """
    root = tmp_path / "conf.d"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "real.yaml").write_text("tenants: {}\n", encoding="utf-8")
    (root / "sub" / "ghost.yaml").symlink_to(root / "sub" / "nope.yaml")
    # A symlink LOOP as well: `is_file()` raises ELOOP rather than
    # returning False, which is the other way the shared predicate has to
    # answer without exploding.
    (root / "sub" / "loop_a.yaml").symlink_to(root / "sub" / "loop_b.yaml")
    (root / "sub" / "loop_b.yaml").symlink_to(root / "sub" / "loop_a.yaml")

    scan = root if recursive else root / "sub"
    readable = set(iter_config_files(scan, recursive=recursive))
    unusable = set(unusable_config_paths(scan, recursive=recursive))

    assert readable & unusable == set(), (
        f"reported twice: {sorted(p.name for p in readable & unusable)}")
    names_r = {p.name for p in readable}
    names_u = {p.name for p in unusable}
    assert names_r == {"real.yaml"}
    assert {"ghost.yaml", "loop_a.yaml", "loop_b.yaml"} <= names_u


# ── a directory the scan could not enter is not "no findings" ────────────


def _deny_scandir(monkeypatch: pytest.MonkeyPatch, target: pathlib.Path) -> None:
    """Make one directory unlistable at any uid.

    `chmod 000` is invisible to root and this suite is usually developed in
    a uid-0 container, so the real syscall cannot be provoked here.
    Patching `os.scandir` reproduces exactly what `os.walk` reacts to.
    """
    real = os.scandir

    def fake(path=".", *a, **kw):  # noqa: ANN001
        if os.fspath(path) == os.fspath(target):
            raise PermissionError(13, "Permission denied", os.fspath(target))
        return real(path, *a, **kw)

    monkeypatch.setattr(os, "scandir", fake)


def test_an_untraversable_subdir_is_named_not_silently_dropped(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
):
    """⛔ `os.walk` defaults to `onerror=None`, which SWALLOWS the failure.

    The subtree simply is not there — not filtered, never seen — so both
    enumerations return as if the tree were healthy. Measured before the
    `onerror` callback, with one chmod-000 sub-directory holding a tenant
    file, `check_yaml_syntax` answered:

        status: pass / "1 files parsed successfully" / unusable_files: []

    which is ADR-016's own description of the #1339 defect ("a green light
    for a directory it never read") reproduced one level down, inside the
    list whose entire job is to make such things audible.
    """
    root = tmp_path / "conf.d"
    locked = root / "locked"
    locked.mkdir(parents=True)
    (locked / "tenant.yaml").write_text("tenants: {}\n", encoding="utf-8")
    (root / "top.yaml").write_text("tenants: {}\n", encoding="utf-8")

    _deny_scandir(monkeypatch, locked)

    unusable = unusable_config_paths(root)
    assert locked in unusable, (
        "a subtree that was never scanned must not read as 'nothing found'")
    # ⚠️ And it must NOT be described as "you named a directory like a
    # config file" — the operator's problem is the subtree, not the name.
    assert "could not be read" in unusable_reason(locked)
    assert "NOT scanned" in unusable_reason(locked)
    # The rest of the scan still works; this is a signal, not a refusal.
    assert [p.name for p in iter_config_files(root)] == ["top.yaml"]


@pytest.mark.parametrize("recursive", [False, True])
def test_an_unlistable_root_names_itself_instead_of_raising(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, recursive: bool,
):
    """⛔ Both wrong answers were live at once, one per branch.

    A `chmod 111` conf.d root (traversable, not readable) made the FLAT
    branch of both functions raise `PermissionError` — and `_grar_parse`
    and `diagnose` call `unusable_config_paths` OUTSIDE any `try`, so the
    tool died with a traceback instead of naming the path, which is the
    death #1447 is about. The RECURSIVE branch did the opposite and
    returned `[]`, a clean bill of health for a directory it never opened.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "x.yaml").write_text("tenants: {}\n", encoding="utf-8")
    _deny_scandir(monkeypatch, root)
    monkeypatch.setattr(
        pathlib.Path, "iterdir",
        lambda self: (_ for _ in ()).throw(
            PermissionError(13, "Permission denied", str(self)))
        if os.fspath(self) == os.fspath(root) else iter([]))

    assert unusable_config_paths(root, recursive=recursive) == [root]
    assert list(iter_config_files(root, recursive=recursive)) == []


def test_a_config_named_directory_that_is_also_unscannable_is_listed_once(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch,
):
    """One path, one entry — even when it qualifies twice over.

    ⛔ Surviving mutation (B-tier, mutation review round 3): dropping the
    `if d not in found` filter where the unscannable directories are
    appended left the whole suite green. Nothing covered the overlap,
    because every existing fixture puts a path in exactly one of the two
    branches: `beta.yaml` (a config-named directory) is listable, and
    `locked` (an unlistable directory) is not config-named.

    A directory called `beta.yaml` that ALSO cannot be scanned qualifies
    on both counts — config-named and not a regular file, so the walk of
    its PARENT records it; unscannable, so `_walk_error` records it too —
    and is emitted twice without the filter.

    ⚠️ Duplicates are not cosmetic here, and this exact failure has already
    shipped twice in this function's history (an unreadable regular
    `.yaml`, then a broken symlink): callers COUNT this list. `check_yaml_
    syntax`'s caveat line said "2 file(s)" for one path, which sends an
    operator looking for a second problem that does not exist — from the
    list whose entire purpose is to describe the tree accurately.
    """
    root = tmp_path / "conf.d"
    both = root / "beta.yaml"
    both.mkdir(parents=True)
    (root / "top.yaml").write_text("tenants: {}\n", encoding="utf-8")

    _deny_scandir(monkeypatch, both)

    unusable = unusable_config_paths(root)
    assert unusable.count(both) == 1, (
        "a path that is BOTH config-named-but-not-a-file AND unscannable "
        "must appear once; callers count this list, so a duplicate reports "
        "one problem as two", [str(p) for p in unusable])
    assert unusable == [both], (
        "nothing else in this tree is unusable", [str(p) for p in unusable])
    # ⚠️ NOT a control for the de-duplication — `iter_config_files` is
    # untouched by that filter, so this line can neither hide nor reveal a
    # duplicate. What it does prove is that the injected `os.scandir`
    # failure is scoped to `beta.yaml`: the other reader still sees the
    # tree, so a red above is the overlap and not a broken fixture.
    assert [p.name for p in iter_config_files(root)] == ["top.yaml"]


# ---------------------------------------------------------------------------
# unusable_config_entries — the already-listed sibling (#1607)
# ---------------------------------------------------------------------------

def test_entries_agrees_with_paths_on_the_same_flat_tree(with_unusable):
    """⛔ The two must not become two answers.

    `unusable_config_paths` LISTS the directory; `unusable_config_entries`
    takes a listing the caller already made. Same question, so on the same
    flat tree they must return the same set — otherwise a reader that walks
    with its own `iterdir()` reports different unusable files than one going
    through `iter_config_files`, which is the #1339 shape.
    """
    listed = {p.name for p in unusable_config_paths(with_unusable,
                                                    recursive=False)}
    given = {p.name for p in
             unusable_config_entries(sorted(with_unusable.iterdir()))}
    assert listed == given == {"beta.yaml", "gamma.yaml"}


def test_entries_honours_the_callers_suffix_set(with_unusable):
    """A reader that only reads `*.yaml` must not be handed a `.yml` finding.

    It could not have read the file either way, so naming it would put a
    finding in front of the operator that the tool printing it cannot act
    on. (That such narrow readers exist at all is #1603.)
    """
    (with_unusable / "delta.yml").mkdir()
    both = {p.name for p in
            unusable_config_entries(sorted(with_unusable.iterdir()))}
    narrow = {p.name for p in
              unusable_config_entries(sorted(with_unusable.iterdir()),
                                      suffixes=(".yaml",))}
    assert "delta.yml" in both, "default must mirror iter_config_files"
    assert "delta.yml" not in narrow
    assert {"beta.yaml", "gamma.yaml"} <= narrow


def test_entries_excludes_readable_and_non_config_names(with_unusable):
    got = {p.name for p in
           unusable_config_entries(sorted(with_unusable.iterdir()))}
    assert "acme.yaml" not in got and "_defaults.yaml" not in got
    assert "notes.txt" not in got


def test_entries_is_case_insensitive_like_every_other_predicate(tmp_path):
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "UPPER.YAML").mkdir()
    got = [p.name for p in unusable_config_entries(sorted(root.iterdir()))]
    assert got == ["UPPER.YAML"], (
        "a directory named like an UPPERCASE config file is exactly the "
        "shape #1588 taught this module to see")


def test_entries_does_not_list_the_directory_itself(tmp_path, capsys):
    """⛔ It must make NO filesystem listing of its own.

    `defaults_files_in` takes names for this reason and
    `test_confd_enumeration_contract` reddens `_lib_confd.py` for a sentinel
    flat scan. Passing entries from a DIFFERENT directory than the one the
    paths live in would still work if the function only inspects what it is
    given — and would silently re-scan if it did not.
    """
    root = tmp_path / "conf.d"
    root.mkdir()
    (root / "ghost.yaml").mkdir()
    (root / "real.yaml").write_text("tenants: {}\n", encoding="utf-8")
    entries = sorted(root.iterdir())
    for p in root.iterdir():
        if p.is_dir():
            p.rmdir()
    # `ghost.yaml` no longer exists; a function that re-listed would return
    # nothing, one that inspects the given entries still calls it unusable.
    got = [p.name for p in unusable_config_entries(entries)]
    assert got == ["ghost.yaml"]


def test_entries_returns_posix_path_order_not_basename_order(tmp_path):
    """Recursive callers pass paths from several directories."""
    root = tmp_path / "conf.d"
    (root / "b").mkdir(parents=True)
    (root / "a").mkdir(parents=True)
    (root / "b" / "same.yaml").mkdir()
    (root / "a" / "same.yaml").mkdir()
    got = [str(p.relative_to(root)) for p in
           unusable_config_entries(sorted(root.rglob("*")))]
    assert got == ["a/same.yaml", "b/same.yaml"], (
        "sorting by basename would make two same-named entries in different "
        "directories order arbitrarily")
