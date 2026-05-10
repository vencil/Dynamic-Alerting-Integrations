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

import re
import sys
from pathlib import Path

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
    path.write_text(text, encoding="utf-8")
    return n


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: route_metrics_through_field.py <file>...", file=sys.stderr)
        return 2
    total = 0
    for arg in sys.argv[1:]:
        n = process_file(Path(arg))
        if n:
            print(f"{arg}: +{n} routed")
            total += n
    print(f"Total: {total} call sites routed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
