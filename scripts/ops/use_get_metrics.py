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


def process_file(path: Path) -> int:
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
    if n:
        path.write_text("\n".join(out), encoding="utf-8", newline="\n")
    return n


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
    total = 0
    for arg in args.paths:
        n = process_file(Path(arg))
        if n:
            print(f"{arg}: +{n} m.metrics → m.getMetrics()")
            total += n
    print(f"Total: {total}")
    return 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
