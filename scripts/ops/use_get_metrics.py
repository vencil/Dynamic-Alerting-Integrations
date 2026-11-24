#!/usr/bin/env python3
"""Replace `m.metrics.X(...)` and `m.metrics)` / `m.metrics,` with the
lazy-init getter `m.getMetrics()`. Skips lines inside the SetMetrics and
getMetrics function bodies themselves (where direct field access is
intentional).

Run on a list of files; idempotent.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# #3579: the run() exit (_lib_exitcodes, with _lib_compat) lives in scripts/tools/.
# Appended, so a copy beside this file (one directory holding all three) wins.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "tools"))

from _lib_output import (  # noqa: E402  (#3981: the artifact entry)
    check_repo_artifact, exit_on_output_write_error, write_repo_artifact)


def process_file(path: Path) -> tuple[str, int]:
    """``(new text, lines rewritten)``; nothing is written here (#3981). A FIFO or
    an unreadable file never reaches it: `classify_path(..., "file")` refuses
    it first."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    out: list[str] = []
    inside_internals = False  # True while scanning SetMetrics or getMetrics body
    depth_at_open = 0
    depth = 0
    n = 0
    for line in lines:
        # Track when we enter / leave the SetMetrics or getMetrics function
        # bodies so we don't rewrite their internal references.
        stripped = line.lstrip()
        if stripped.startswith("func (m *ConfigManager) SetMetrics(") or \
           stripped.startswith("func (m *ConfigManager) getMetrics()"):
            inside_internals = True
            depth_at_open = depth
        # Update brace depth (crude — ignores braces inside strings/comments,
        # acceptable for well-formatted Go).
        code = line.split("//", 1)[0]
        depth += code.count("{") - code.count("}")
        if inside_internals and depth == depth_at_open and "}" in code:
            # Closing brace of the function we were tracking — exit AFTER
            # processing this line.
            out.append(line)
            inside_internals = False
            continue
        if inside_internals:
            out.append(line)
            continue
        # Outside the off-limits zones — perform the substitution.
        new_line = re.sub(r"\bm\.metrics\b", "m.getMetrics()", line)
        if new_line != line:
            n += 1
        out.append(new_line)
    return "\n".join(out), n


@exit_on_output_write_error
def main() -> int:
    import argparse

    import _lib_paths
    parser = argparse.ArgumentParser(description='Replace m.metrics field access with m.getMetrics() in the given Go files.')
    parser.add_argument("paths", nargs="+", metavar="FILE")
    args = parser.parse_args()
    # #3605: every path is judged before the first one is rewritten, so a
    # missing one is a named refusal (rc 2) with nothing written.
    _lib_paths.raise_path_problems(
        [_lib_paths.classify_path(p, "file", lang="en") for p in args.paths])
    # #3981: every file is read and every target judged before the first write.
    pending: list[tuple[str, str, int]] = []
    # Two names of one file (`a.go ./a.go`, a hard link) are one target: the
    # second read still sees the old text, and would count and write it again.
    seen: set[tuple[int, int]] = set()
    for arg in args.paths:
        text, n = process_file(Path(arg))
        st = os.stat(arg)
        if n and (st.st_dev, st.st_ino) not in seen:
            seen.add((st.st_dev, st.st_ino))
            pending.append((arg, text, n))
    for arg, _text, _n in pending:
        check_repo_artifact(arg, flag="FILE")
    total = 0
    for arg, text, n in pending:
        write_repo_artifact(arg, text, flag="FILE")
        print(f"{arg}: +{n} m.metrics → m.getMetrics()")
        total += n
    print(f"Total: {total}")
    return 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
