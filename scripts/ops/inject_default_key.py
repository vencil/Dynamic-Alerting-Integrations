#!/usr/bin/env python3
"""Idempotently inject a numeric key into a `_defaults.yaml` defaults block.

Use case: `bench_e2e_run.sh` needs to register `bench_trigger=50` in the
active fixture's root `_defaults.yaml` so bench-run-N tenants' overrides
are served (an undeclared key gets no row: the shared key verdict reads
it as unread, `no_row`). For the
generator path this is now done in-place by `--extra-defaults`
(generate_tenant_fixture.py); this helper still exists for the
NON-generator path (customer-anon fixtures, hand-curated fixtures, or
re-runs against an already-staged tree).

Behavior:
    * Re-run safe: if `<key>:` is already in the file, exits 0 without
      writing.
    * Numeric guard: refuses a value the exporter does not serve as a
      default — anything but a finite decimal number of at most 20 digits
      with an optional exponent (#3457; so not `1_000`, `0x1F`, `nan`,
      `inf`), to align with the `_defaults.yaml` parser contract
      (`cycle-6` RCA, archive §S#37d).
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

import os
import sys
from pathlib import Path

# #3579: the run() exit (_lib_exitcodes, with _lib_compat) lives in scripts/tools/.
# Appended, so a copy beside this file (one directory holding all three) wins.
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), os.pardir, "tools"))

# ⛔ Same predicate as every other producer of a `defaults:` block, imported
# rather than respelled (#1412). This file lives outside `scripts/tools/`, so the
# path insert is explicit.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "tools" / "ops"))
from _registry_lib import is_shipped_optional_key  # noqa: E402
from _lib_validation import threshold_number, threshold_trim  # noqa: E402  (#3457)


def _is_numeric_str(s: str) -> bool:
    """Whether `s` is a number the exporter serves as a root default (#3457):
    trimmed as the exporter trims, it is in the grammar of a threshold number
    and finite. `int()` / `float()` also read `1_000`, `nan` and `inf`, for
    which the exporter serves no default."""
    return threshold_number(threshold_trim(s)) is not None


def inject(path: Path, key: str, value: str) -> int:
    """Inject `key: value` under `defaults:` in `path`. Returns exit code."""
    # ⛔ Shape before value, and the two refusals answer different questions. A
    # `<base>_critical` name is inert under `defaults:` however numeric its
    # value: the exporter resolves the critical tier from tenant overrides keyed
    # on `defaults[<base>]`, and nothing reports it — no parse error, no WARN,
    # no series (#1218 / #1412). A dimensional `key{…}` CAN be a root default
    # (#2031), but only once `dimensions:` declares its labels (#3475), which
    # this tool does not write — so it is refused too (#3478).
    if not is_shipped_optional_key(key):
        print(
            f"[inject_default_key] ERROR: key {key!r} cannot live under "
            f"`defaults:` via this tool: a `_critical` suffix is inert there and "
            f"fails SILENTLY, and a `key{{label=...}}` key is served only when the "
            f"root `dimensions:` declares its labels, which this tool does not "
            f"write. Inject the base key here and put the tier in a "
            f"`<tenant>.yaml` override, or write the dimensional key and its "
            f"`dimensions:` declaration by hand (#1218 / #1412 / #3478)",
            file=sys.stderr,
        )
        return 1
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
                out.append(f"  {key}: {threshold_trim(value)}")
                inserted = True
        if not inserted:
            # No `defaults:` block found — prepend a fresh one. Preserves
            # existing content.
            out = ["defaults:", f"  {key}: {threshold_trim(value)}", ""] + out
        path.write_text("\n".join(out) + "\n", encoding="utf-8", newline="\n")
        print(
            f"[inject_default_key] registered {key}={value} in {path}"
        )
        return 0

    # File doesn't exist — create a minimal one.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"defaults:\n  {key}: {threshold_trim(value)}\n",
        encoding="utf-8", newline="\n",
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


def _cli() -> int:
    """The process entry: `main` with the command line (`main` takes the
    argv explicitly, for importers and tests)."""
    return main(sys.argv)


if __name__ == "__main__":
    import _lib_exitcodes
    _lib_exitcodes.run(_cli)
