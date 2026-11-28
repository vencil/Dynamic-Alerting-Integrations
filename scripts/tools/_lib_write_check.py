"""_lib_write_check.py — a writer's check of the tree it writes into (#3206).

ADR-036 §2: only Go writes conf.d. A Python tool that changes a conf.d file
sends what to change to `da-guard write` (`_lib_confedit`), which judges the
tree with the files in place, reads them back and writes them; there is no
Python-side confirmation of a generated text any more (the read-back
`confirm` / `confirm_all` / `confirm_tree` this module used to hold went with
the last Python conf.d writer, #3206 step 10).

What is left is the one check a one-shot CLI makes before it writes ANYTHING
— a conf.d file, a report, a CSV — under an output directory that already
exists: :func:`require_output_tree`, the writers' fail-closed exit. The run
writes into a tree it did not build, so that tree must first pass the shared
refusal (`_lib_parse_cache.require_servable`).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

#: How long the tree's judgement may take (seconds).
DEFAULT_TIMEOUT = 120


def require_output_tree(root: "str | os.PathLike[str]", *,
                        binary: Optional[str] = None,
                        timeout: float = DEFAULT_TIMEOUT) -> None:
    """⛔ THE writers' fail-closed exit (#3206), for a one-shot CLI: a run
    that writes ANYTHING under *root* — a conf.d file, a report, a CSV —
    calls this before its first write. When *root* is a directory that
    already exists, the run writes INTO a tree it did not build, so that
    tree must pass the shared refusal (`_lib_parse_cache.require_servable`:
    any entry Go gives a `verdict` ends the process with exit 2, every such
    path named, `--json` reason `conf_d_not_servable` with
    `refused_paths`), and nothing is written.

    ⛔ It judges the output directory AS the tree. A directory inside a
    larger conf.d (`-o conf.d/sub`) is judged as its own tree: what lies
    beside it, under the conf.d root the exporter serves, is not looked at
    — this tool cannot know which root that is (an ancestor test would
    refuse `-o /tmp/new` under any flagged directory above it). To have the
    whole conf.d judged, pass the conf.d root.

    * *root* missing (or not a directory): a fresh tree; nothing to refuse.
    * *root* an existing directory that holds no conf.d (an empty one, or
      only files no reader reads): Go serves it (no entry has a verdict), so
      the run goes on — Go is the judge of whether it can be a conf.d root,
      not a Python "looks like a conf.d" test.

    Every writer that names an output directory calls it itself
    (`scaffold_tenant`, `init_project`, `onboard_platform`, `migrate_rule`,
    `da_assembler --render-cr`). Judged once per tree and process (a reader
    of this run that already read the tree whole is not asked again). A
    long-running writer, which must judge afresh before every write and must
    not exit, takes `_lib_parse_cache.refusal_now` instead (`da_assembler`'s
    controller). Raises `_lib_parse_tree.ParseTreeError` for no usable
    da-guard."""
    if Path(root).is_dir():
        import _lib_parse_cache  # lazy: a reader library
        _lib_parse_cache.require_servable(root, binary=binary, timeout=timeout)


#: ⛔ THE one table (#4014): each one-shot writer whose output is a draft
#: owns a dot-directory of its own under its output directory, and writes
#: every YAML it makes there — its draft conf.d at `<root>/conf.d/`
#: (`draft_confd_rel`), its other YAML (rule packs, relabel suggestions) in
#: `<root>/` itself. The CSVs and reports, which no conf.d reader reads,
#: stay where an operator looks.
#:
#: * Dot-prefixed: the outputs are a draft (a tenant to review, a
#:   `_defaults.yaml` fragment to merge by hand), and `-o` may lie inside a
#:   served conf.d — pointed at it, or the relative default run from inside
#:   it. A `.`-prefixed directory is pruned whole by every conf.d reader
#:   (Go's `confdname.IsHidden`, shared by the exporter's walk and
#:   `da-guard parse-tree`), so nothing under it is served wherever `-o`
#:   lands; the draft conf.d passed as a `--config-dir` root of its own is
#:   judged whole.
#: * One per tool: the tools' outputs are meant to be chained through one
#:   `-o` (`scaffold --from-onboard <-o>/onboard-hints.json -o <-o>`), so a
#:   directory they shared let one tool replace another's draft whole, rc 0.
#:
#: tests/shared/test_writers_keep_served_tree.py derives the conf.d writers,
#: runs every draft writer inside a served tree (asking Go which tenants it
#: serves) and all of them into one -o (no draft replaced by another tool).
DRAFT_ROOTS = {
    "onboard_platform": ".onboard",
    "scaffold_tenant": ".scaffold",
}

#: The draft conf.d's name under a tool's draft root.
DRAFT_CONFD = "conf.d"


def draft_confd_rel(tool: str) -> str:
    """*tool*'s draft conf.d, relative to its output directory."""
    return f"{DRAFT_ROOTS[tool]}/{DRAFT_CONFD}"


def draft_confd_extras(conf_dir: "str | os.PathLike[str]", mine) -> list:
    """The tenant files of the draft conf.d at *conf_dir* other than *mine*
    (the relative paths this run writes), in walk order; [] when it is not
    there (#3671): another run's tenant is another tenant in the draft. The
    tree as the exporter's walk sees it (da-guard's list-only walk) and the
    class Go gives each name: a hidden or non-config name is read by no
    reader, so it adds no tenant; a platform file the operator put there (a
    `_profiles.yaml` the draft tenant's `--profile` names, #3591) is not a
    tenant and stays. Raises `_lib_parse_tree.ParseTreeError` for no usable
    da-guard."""
    conf_dir = Path(conf_dir)
    if not conf_dir.is_dir():
        return []
    import _lib_parse_tree  # noqa: PLC0415 — lazy: a reader library
    return [e.path for e in _lib_parse_tree.list_tree(conf_dir).config_files()
            if e.cls == "tenant" and e.path not in mine]


def refuse_draft_layout(output_dir: "str | os.PathLike[str]", tool: str, mine, *,
                        why: str, writes_tenants: bool = True,
                        made_later=()) -> Optional[str]:
    """Why *tool*'s run may not write under *output_dir*, or None — asked
    before anything is written or made, so a refused run leaves nothing
    behind (#3997, #4014):

    * a directory the run makes after its conf.d batch exists and is not a
      directory: the tool's draft root, its draft conf.d, and *made_later*
      (names under *output_dir*, such as a CSV directory);
    * the draft conf.d holds a tenant file this run does not write (*mine*;
      `draft_confd_extras`): an earlier run's tenant is another tenant in
      the draft tree (#3671). *why* names what such a file is, for the
      message. Only a run that writes tenants (*writes_tenants*) owns the
      draft's tenant set: a run of onboard's Phase 2 alone, after a Phase 1
      run into the same -o, writes `_defaults.yaml` beside them.

    Raises `_lib_parse_tree.ParseTreeError` for no usable da-guard."""
    out = Path(output_dir)
    for rel in (DRAFT_ROOTS[tool], draft_confd_rel(tool), *made_later):
        path = out / rel
        if path.exists() and not path.is_dir():
            return f"{path} is not a directory; remove it or use another -o"
    conf_dir = out / draft_confd_rel(tool)
    other = draft_confd_extras(conf_dir, set(mine)) if writes_tenants else []
    if other:
        return (f"{conf_dir} holds {', '.join(other)}, which this run does not write "
                f"({why}); remove it or use another -o")
    return None
