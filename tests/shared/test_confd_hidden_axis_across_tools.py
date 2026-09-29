#!/usr/bin/env python3
"""#2067 — every conf.d reader must be BLIND to `.`-prefixed names.

The exporter's walker skips any entry whose name starts with `.`: files
outright, directories with `SkipDir` (`pkg/config.ScanDirTree`,
`config_hierarchy.go`). A Python tool that reads them answers about a tree
the exporter does not have — `generate_tenant_metadata` put `.hidden.yaml`'s
tenant in the portal list (#2055), `describe_tenant` described tenants in
`.snap/` (#2054), `tenant_carriers` handed `.ghost` on as a tenant or as an
"invalid name" warning (#2067). Each was fixed one at a time; this file is
what stops the NEXT tool from repeating it.

Shape, per the ticket: ONE tree, every reader.

    conf.d/
      _defaults.yaml
      acme.yaml            tenant `acme`       <- CONTROL: must be seen
      .ghost.yaml          tenant `ghost`      <- hidden file
      .snap/ghostsnap.yaml tenant `ghostsnap`  <- under a hidden directory

Each cell calls one tool's enumeration entry point and asserts that no
identifier it returns names a hidden path, AND that `acme` is there.

⛔ The control is not decoration. "No `ghost` in the output" is satisfied
just as well by a tool that read nothing at all — a wrong `config_dir`, an
adapter calling the wrong function, an empty return after an exception the
adapter swallowed. `acme` must be in every cell's answer, or the cell has
no detection power and says so as a failure.

⛔ WHO IS IN THE TABLE (the other half of the promise). A tool NOT in
`CELLS` would pass by absence. `test_every_enumerating_reader_is_accounted_for`
therefore derives the set of population files that list a directory
themselves (`_confd_population` + the enumeration contract's own call
classifier) and requires each to be a cell, a pinned known-open reader, or
an entry in `NOT_A_CELL` with the reason. A new tool that lists a conf.d
turns that test red until someone decides which.

⚠️ SCOPE, so a green run is not over-read:

* The oracle is the return value, not every byte of stderr: a tool that
  WARNS about a hidden entry it skipped (`assemble_config_dir`) is not a
  failure here; a tool that counts, lists, or declares one is.
* Flat readers see `.snap/` only as a directory; the hidden-directory arm
  bites the recursive ones. Both arms are in one tree so no reader can pass
  by being handed only the arm it happens to handle.
* `KNOWN_OPEN` readers still read hidden paths on this tree (measured, see
  the table). They are pinned as STILL LEAKING rather than skipped, so a
  fix that lands without moving them into `CELLS` turns red here too.
"""

from __future__ import annotations

import contextlib
import functools
import io
import pathlib
import sys
from typing import Callable, Iterable

import pytest

from test_confd_enumeration_contract import _enumeration_kinds, _readers

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO / "scripts" / "tools"
for _extra in (TOOLS_DIR, TOOLS_DIR / "ops", TOOLS_DIR / "dx",
               TOOLS_DIR / "lint"):
    if str(_extra) not in sys.path:
        sys.path.insert(0, str(_extra))

CONTROL = "acme"
# Every hidden-path identifier contains one of these; nothing visible does.
HIDDEN_MARKS = ("ghost", ".snap")

# A body most readers understand: a threshold (gitops_check counts it,
# deprecate_rule scans for it), `_metadata.db_type` (check_retire_drift,
# migrate_conf_d) and a `_custom_alerts` instance (the loader emits a triple
# only for a tenant that carries one — without it the loader's cell would
# see nothing in EITHER arm and pass vacuously).
_BODY = (
    "tenants:\n"
    "  {tid}:\n"
    "    cpu_usage: \"80\"\n"
    "    _metadata:\n"
    "      db_type: postgres\n"
    "    _custom_alerts:\n"
    "      - recipe: pg_connections_high\n"
    "        params:\n"
    "          threshold: 95\n"
)


def build_tree(root: pathlib.Path, *, control: bool = True) -> pathlib.Path:
    (root / ".snap").mkdir(parents=True)
    (root / "_defaults.yaml").write_text("defaults:\n  cpu_usage: 70\n",
                                         encoding="utf-8")
    if control:
        (root / "acme.yaml").write_text(_BODY.format(tid=CONTROL),
                                        encoding="utf-8")
    (root / ".ghost.yaml").write_text(_BODY.format(tid="ghost"),
                                      encoding="utf-8")
    (root / ".snap" / "ghostsnap.yaml").write_text(
        _BODY.format(tid="ghostsnap"), encoding="utf-8")
    return root


# ── adapters: tree -> the identifiers the tool reports ─────────────────
# Each returns strings (tenant ids, file names, paths relative to the
# tree). They call the tool's own entry point; none re-implements a filter.

def _names(paths: Iterable[pathlib.Path], root: pathlib.Path) -> list[str]:
    return [pathlib.Path(p).relative_to(root).as_posix() for p in paths]


def _tenant_carriers(root):
    from _lib_confd import tenant_carriers
    # validate=None: the lane where a hidden stem used to come back as a
    # TENANT. The `invalid` lane is what the operator cells below cover.
    got = tenant_carriers(root)
    return got.tenants + got.invalid + _names(got.unusable, root)


def _declared_tenant_ids(root):
    from _lib_confd import declared_tenant_ids
    return sorted(declared_tenant_ids(root))


def _iter_config_files(root):
    from _lib_confd import iter_config_files
    return _names(iter_config_files(root), root)


def _list_config_tree(root):
    from _lib_confd import list_config_tree
    got = list_config_tree(root)
    return _names(got.files + got.unusable, root)


def _iter_yaml_files(root):
    import _lib_io
    return [name for name, _ in _lib_io.iter_yaml_files(str(root))]


def _load_tenant_configs(root):
    import _lib_io
    return sorted(_lib_io.load_tenant_configs(str(root)))


def _operator_generate(root):
    import operator_generate
    # stderr too: the pre-#2067 leak here was a `Skipping invalid tenant
    # name '.ghost'` warning, not a tenant.
    return operator_generate.discover_tenant_configs(root)


def _migrate_to_operator(root):
    import migrate_to_operator
    src = root.parent / "h2067-empty-source"
    src.mkdir(exist_ok=True)
    analysis = migrate_to_operator.analyze_migration(src, root)
    # `tenants` is a COUNT; the tree has exactly one visible carrier, so
    # every count above 1 is a hidden file being counted.
    return ([CONTROL] * min(analysis["tenants"], 1)
            + ["ghost?"] * max(analysis["tenants"] - 1, 0)
            + list(analysis["issues"])
            + migrate_to_operator.discover_tenant_configs(root))


def _check_path_metadata(root):
    import check_path_metadata_consistency as cpm
    return _names(cpm.iter_tenant_files(root), root)


def _describe_tenant(root):
    import describe_tenant
    return sorted(describe_tenant.ConfDScanner(root).tenants)


def _generate_tenant_metadata(root):
    import generate_tenant_metadata as gtm
    return sorted(gtm.build_tenant_metadata(root)["tenant_metadata"])


def _gitops_check(root):
    import gitops_check
    details = gitops_check.check_local(str(root)).details
    n = details.get("tenant_files", 0)
    # A count, like `_migrate_to_operator`: one visible carrier in the tree.
    return [CONTROL] * min(n, 1) + ["ghost?"] * max(n - 1, 0)


def _custom_alerts_loader(root):
    from custom_alerts import loader
    triples, errors = loader.collect_instances(root)
    return ([t[0] for t in triples] + [t[2] for t in triples]
            + [str(e) for e in errors])


def _check_routing_profiles(root):
    import check_routing_profiles
    data = check_routing_profiles._collect_data(str(root))
    return (sorted(data["tenant_ids"]) + list(map(str, data["unreadable"]))
            + list(map(str, data["unreadable_files"])))


def _config_history(root):
    import config_history
    return [f["name"] for f in config_history._scan_config_dir(str(root))]


def _drift_detect(root):
    import drift_detect
    return sorted(drift_detect.compute_dir_manifest(str(root)).files)


def _migrate_conf_d(root):
    import migrate_conf_d
    # Directories are reported as `skip_already_nested` whatever they are
    # called — a skip record, not a read — so only FILE actions count.
    return [a["tenant_id"] or a["source"]
            for a in migrate_conf_d.plan_migration(root)
            if a["status"] != "skip_already_nested"]


def _offboard_tenant(root):
    import offboard_tenant
    return sorted(offboard_tenant.load_all_configs(str(root)))


def _assemble_config_dir(root):
    import assemble_config_dir
    # It WARNS on stderr about each hidden entry it leaves out (#1827);
    # the return value is what it assembles.
    return _names(assemble_config_dir.list_visible_config_entries(root), root)


def _deprecate_rule(root):
    import deprecate_rule
    return [h["filename"]
            for h in deprecate_rule.scan_for_metric("cpu_usage", str(root))]


def _check_confd_schema(root):
    import check_confd_schema
    return [pathlib.Path(p).relative_to(root).as_posix()
            for p in check_confd_schema._iter_yaml_files(str(root))]


def _check_retire_drift(root):
    import check_retire_drift
    return sorted(check_retire_drift.conf_d_declared_db_type_tenants(root))


# cell id -> (defining file, relative to scripts/tools; adapter)
CELLS: dict[str, tuple[str, Callable[[pathlib.Path], list[str]]]] = {
    "tenant_carriers": ("_lib_confd.py", _tenant_carriers),
    "declared_tenant_ids": ("_lib_confd.py", _declared_tenant_ids),
    "iter_config_files": ("_lib_confd.py", _iter_config_files),
    "list_config_tree": ("_lib_confd.py", _list_config_tree),
    "iter_yaml_files": ("_lib_io.py", _iter_yaml_files),
    "load_tenant_configs": ("_lib_io.py", _load_tenant_configs),
    "operator_generate": ("ops/operator_generate.py", _operator_generate),
    "migrate_to_operator": ("ops/migrate_to_operator.py",
                            _migrate_to_operator),
    "check_path_metadata_consistency": (
        "lint/check_path_metadata_consistency.py", _check_path_metadata),
    "describe_tenant": ("dx/describe_tenant.py", _describe_tenant),
    "generate_tenant_metadata": ("dx/generate_tenant_metadata.py",
                                 _generate_tenant_metadata),
    "gitops_check": ("ops/gitops_check.py", _gitops_check),
    "custom_alerts_loader": ("dx/custom_alerts/loader.py",
                             _custom_alerts_loader),
    "check_routing_profiles": ("lint/check_routing_profiles.py",
                               _check_routing_profiles),
    "config_history": ("ops/config_history.py", _config_history),
    "drift_detect": ("ops/drift_detect.py", _drift_detect),
    "migrate_conf_d": ("dx/migrate_conf_d.py", _migrate_conf_d),
    "offboard_tenant": ("ops/offboard_tenant.py", _offboard_tenant),
    "assemble_config_dir": ("ops/assemble_config_dir.py",
                            _assemble_config_dir),
    "deprecate_rule": ("ops/deprecate_rule.py", _deprecate_rule),
    # #2360: both were KNOWN_OPEN below until they pruned hidden directories
    # (check_confd_schema) and hidden names at all (check_retire_drift).
    "check_confd_schema": ("lint/check_confd_schema.py", _check_confd_schema),
    "check_retire_drift": ("lint/check_retire_drift.py", _check_retire_drift),
}

# Readers that STILL read hidden paths on this tree, pinned as leaking so a
# fix that lands without moving the reader into `CELLS` turns red. Empty
# since #2360 moved `check_confd_schema` and `check_retire_drift` into
# `CELLS`; a newly found leak is added here (measured) until it is fixed.
KNOWN_OPEN: dict[str, tuple[str, Callable[[pathlib.Path], list[str]]]] = {}

# Population files that list a directory themselves but get no cell here.
NOT_A_CELL: dict[str, str] = {
    "dx/generate_tenant_fixture.py":
        "lists its own OUTPUT directory (what it just generated), not a "
        "conf.d it reads",
    "dx/run_chaos_soak.py":
        "`trigger_reload` WRITES a carrier; its hidden axis is pinned by "
        "tests/dx/test_run_chaos_soak.py (#1630)",
    "lint/check_threshold_unit_sanity.py":
        "walks fixed repo-relative dirs, takes no conf.d argument; hidden "
        "axis pinned by test_hidden_carriers_are_not_linted (#1630)",
    "ops/backtest_threshold.py":
        "needs a git repo with HEAD~1; its `.hidden.yaml` neighbour is "
        "pinned by test_confd_case_parity_across_tools's backtest tests",
    "ops/_grar_parse.py":
        "lists only through `_lib_confd.iter_config_files` (library cell)",
    "ops/diagnose.py":
        "lists only through `_lib_confd.iter_config_files` (library cell)",
    "ops/validate_config.py":
        "lists only through `_lib_confd.iter_config_files` (library cell)",
    "ops/init_project.py":
        "reads conf.d only through `_lib_confd.iter_config_files`; its "
        "other walks are over the tree it scaffolds",
    "lint/_rule_tree.py": "walks rule-packs, not a conf.d",
    "lint/check_cli_contract.py": "walks docs/, not a conf.d",
    "lint/check_cli_default_drift.py": "walks scripts/, not a conf.d",
    "lint/check_doc_datools_cmds.py": "walks docs/, not a conf.d",
    "lint/check_frontmatter_versions.py": "walks docs/, not a conf.d",
    "lint/check_k8s_manifests.py": "walks k8s manifests, not a conf.d",
    "lint/check_md_yaml_drift.py": "walks docs/, not a conf.d",
    "lint/check_scrape_reachability.py": "walks k8s manifests, not a conf.d",
    "lint/check_threshold_reachability.py":
        "walks scripts/ for a census, not a conf.d",
    "validate_all.py": "snapshots repo mtimes, not a conf.d",
}


def _run(adapter, root) -> tuple[list[str], str]:
    err = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
        got = adapter(root)
    return [str(x) for x in got], err.getvalue()


def _leaks(seen: list[str]) -> list[str]:
    return [s for s in seen if any(m in s for m in HIDDEN_MARKS)]


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_reader_does_not_see_hidden_paths(cell, tmp_path) -> None:
    _, adapter = CELLS[cell]
    root = build_tree(tmp_path / "conf.d")
    seen, err = _run(adapter, root)

    assert any(CONTROL in s for s in seen), (
        f"{cell}: the CONTROL tenant `{CONTROL}` is missing from {seen!r} — "
        f"this cell cannot tell 'skipped the hidden paths' from 'read "
        f"nothing'. stderr: {err!r}"
    )
    assert not _leaks(seen), (
        f"{cell}: reported {_leaks(seen)!r} from a `.`-prefixed path the "
        f"exporter's walker never reads. Full answer: {seen!r}"
    )
    if cell in ("operator_generate", "migrate_to_operator", "tenant_carriers"):
        # These three leaked through a WARNING before #2067, not a tenant.
        assert "ghost" not in err, (
            f"{cell}: warned about a hidden entry as if it were a lost "
            f"tenant: {err!r}"
        )


@pytest.mark.parametrize("cell", sorted(CELLS))
def test_the_control_is_what_makes_each_cell_pass(cell, tmp_path) -> None:
    """Remove `acme.yaml` and each cell must LOSE the control: proves the
    presence assertion above is answered by the file, not by a constant."""
    _, adapter = CELLS[cell]
    root = build_tree(tmp_path / "conf.d", control=False)
    seen, _ = _run(adapter, root)
    assert not any(CONTROL in s for s in seen), (
        f"{cell}: `{CONTROL}` reported from a tree without `acme.yaml`: "
        f"{seen!r} — the adapter's presence signal is not from the tree"
    )


@pytest.mark.parametrize("cell", sorted(KNOWN_OPEN))
def test_known_open_readers_still_leak(cell, tmp_path) -> None:
    """Pinned as STILL WRONG, so a fix must move the reader into `CELLS`."""
    _, adapter = KNOWN_OPEN[cell]
    root = build_tree(tmp_path / "conf.d")
    seen, _ = _run(adapter, root)
    assert any(CONTROL in s for s in seen), (cell, seen)
    assert _leaks(seen), (
        f"{cell} no longer reads hidden paths ({seen!r}) — move it from "
        f"KNOWN_OPEN into CELLS"
    )


@functools.lru_cache(maxsize=1)
def _own_enumerators() -> frozenset[str]:
    """Population files (`_readers` is the enumeration contract's union
    population) whose own AST lists a directory — flat or recursive,
    including through `iter_config_files` / `list_config_tree`."""
    return frozenset(
        p.relative_to(TOOLS_DIR).as_posix()
        for p in _readers()
        if _enumeration_kinds(p)
    )


def test_every_enumerating_reader_is_accounted_for() -> None:
    derived = _own_enumerators()
    accounted = ({f for f, _ in CELLS.values()}
                 | {f for f, _ in KNOWN_OPEN.values()}
                 | set(NOT_A_CELL))
    # `operator_generate` lists only through `tenant_carriers`, so it is not
    # an own-enumerator; it is a cell because its leak lived in a warning.
    unaccounted = derived - accounted
    stale = (set(NOT_A_CELL) | {f for f, _ in KNOWN_OPEN.values()}) - derived
    assert not unaccounted, (
        f"these files list a directory and are in the conf.d population, "
        f"but have no hidden-axis cell: {sorted(unaccounted)}. Add a cell to "
        f"CELLS (preferred), or a reason to NOT_A_CELL."
    )
    assert not stale, (
        f"NOT_A_CELL / KNOWN_OPEN name files that no longer list a "
        f"directory (or are gone): {sorted(stale)}"
    )


def test_the_derivation_did_not_collapse() -> None:
    """A derived set of 0 would make the reconciliation above vacuous."""
    derived = _own_enumerators()
    assert len(derived) >= 20, sorted(derived)
    for must in ("dx/describe_tenant.py", "ops/gitops_check.py",
                 "dx/generate_tenant_metadata.py"):
        assert must in derived, (must, sorted(derived))
