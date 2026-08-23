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
    """
    readable = {p.resolve() for p in iter_config_files(with_unusable)}
    unusable = {p for p in unusable_config_paths(with_unusable)}
    assert not (readable & {p.resolve() for p in unusable if p.exists()})


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
