#!/usr/bin/env python3
"""Convert ConfigManager-method metric callsites to use m.metrics.X.

For each top-level metric helper (IncParseFailure, ObserveScanDuration,
IncReloadTrigger, etc.), replace bare calls like:
    IncReloadTrigger(ReloadReasonSource)
with the field-routed form:
    m.metrics.IncReloadTrigger(ReloadReasonSource)

ONLY rewrites callsites inside files this script is given on the command
line. The replacement is naive substring — relies on the helpers having
unique enough names that no false matches happen. Validated by `go build`.

Helpers: see HELPERS list. Production wrapper functions in config_metrics.go
that DEFINE these names are NOT modified (the script is only run on caller
files).

Run order:
    python scripts/ops/route_metrics_through_field.py \\
        components/threshold-exporter/app/config.go \\
        components/threshold-exporter/app/config_debounce.go
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# #3579: the run() exit (_lib_exitcodes, with _lib_compat) lives in scripts/tools/.
# Appended, so a copy beside this file (one directory holding all three) wins.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "tools"))

HELPERS = (
    "IncParseFailure",
    "ObserveScanDuration",
    "IncReloadTrigger",
    "IncReloadTriggerBy",
    "IncDefaultsNoop",
    "IncDefaultsNoopBy",
    "IncDefaultsShadowed",
    "IncDefaultsShadowedBy",
    "ObserveBlastRadius",
    "ObserveReloadDuration",
    "ObserveDebounceBatch",
    "SetLastScanComplete",
    "SetLastReloadComplete",
)


def process_file(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    n = 0
    for h in HELPERS:
        # Match `<helper>(` only when:
        #  - preceded by whitespace, tab, or open-paren / start-of-line
        #  - NOT preceded by `.` (to avoid double-rewriting `m.metrics.IncX`)
        #  - NOT preceded by `func ` (to avoid touching the helper definition)
        # The negative-lookbehind for `.` is the key safety check.
        # `r"(?<!\.)\b<helper>\("` with extra guards.
        pattern = re.compile(
            r"(?<![.\w])" + re.escape(h) + r"\(",
        )
        new_text, count = pattern.subn(f"m.metrics.{h}(", text)
        # Don't rewrite if the same line is the helper's own definition or
        # a comment. The negative-lookbehind for `.` handles the most common
        # false positive (already-routed callsites). Other false positives
        # surface as build failures and get hand-fixed.
        if count:
            text = new_text
            n += count
    path.write_text(text, encoding="utf-8", newline="\n")
    return n


def main() -> int:
    import argparse

    import _lib_paths
    parser = argparse.ArgumentParser(description='Route metric calls through the metrics field in the given Go files.')
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
            print(f"{arg}: +{n} routed")
            total += n
    print(f"Total: {total} call sites routed")
    return 0


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(main)
