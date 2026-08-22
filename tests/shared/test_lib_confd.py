"""Unit tests for `_lib_confd` — the single answer to "what is in a conf.d" (#1339)."""

from __future__ import annotations

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
