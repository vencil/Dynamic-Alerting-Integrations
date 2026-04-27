#!/usr/bin/env python3
"""Idempotently inject a numeric key into a `_defaults.yaml` defaults block.

Use case: `bench_e2e_run.sh` needs to register `bench_trigger=50` in the
active fixture's root `_defaults.yaml` so bench-run-N tenants' overrides
pass the exporter's `unknown key not in defaults` check. For the
generator path this is now done in-place by `--extra-defaults`
(generate_tenant_fixture.py); this helper still exists for the
NON-generator path (customer-anon fixtures, hand-curated fixtures, or
re-runs against an already-staged tree).

Behavior:
    * Re-run safe: if `<key>:` is already in the file, exits 0 without
      writing.
    * Numeric guard: refuses non-numeric values to align with the
      `_defaults.yaml` parser contract (`cycle-6` RCA, archive §S#37d).
    * Format-preserving: appends a single line under the existing
      `defaults:` block; does NOT round-trip the entire YAML through
      a parser/dumper (avoids comment loss).

Usage:
    python3 inject_default_key.py <path/to/_defaults.yaml> <key> <numeric>

Exit codes:
    0  — injected (or already present, no-op)
    1  — usage error / non-numeric value / file unreadable
"""
from __future__ import annotations

import sys
from pathlib import Path


def _is_numeric_str(s: str) -> bool:
    """Return True if `s` parses as int or float."""
    s = s.strip()
    if not s:
        return False
    try:
        int(s)
        return True
    except ValueError:
        pass
    try:
        float(s)
        return True
    except ValueError:
        return False


def inject(path: Path, key: str, value: str) -> int:
    """Inject `key: value` under `defaults:` in `path`. Returns exit code."""
    if not _is_numeric_str(value):
        print(
            f"[inject_default_key] ERROR: value {value!r} for key {key!r} is "
            f"not numeric; _defaults.yaml parser rejects non-numeric "
            f"(cycle-6 RCA, archive §S#37d)",
            file=sys.stderr,
        )
        return 1

    if path.exists():
        text = path.read_text(encoding="utf-8")
        # Idempotent: skip if key already present anywhere in file.
        if f"{key}:" in text:
            print(
                f"[inject_default_key] {key} already present in {path}; "
                f"skipping (idempotent)"
            )
            return 0
        # Find the `defaults:` line and insert `  <key>: <value>` after it.
        lines = text.splitlines()
        out: list[str] = []
        inserted = False
        for line in lines:
            out.append(line)
            if not inserted and line.rstrip() == "defaults:":
                out.append(f"  {key}: {value.strip()}")
                inserted = True
        if not inserted:
            # No `defaults:` block found — prepend a fresh one. Preserves
            # existing content.
            out = ["defaults:", f"  {key}: {value.strip()}", ""] + out
        path.write_text("\n".join(out) + "\n", encoding="utf-8")
        print(
            f"[inject_default_key] registered {key}={value} in {path}"
        )
        return 0

    # File doesn't exist — create a minimal one.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"defaults:\n  {key}: {value.strip()}\n",
        encoding="utf-8",
    )
    print(
        f"[inject_default_key] created {path} with {key}={value}"
    )
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: inject_default_key.py <path> <key> <numeric_value>",
            file=sys.stderr,
        )
        return 1
    return inject(Path(argv[1]), argv[2], argv[3])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
