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
