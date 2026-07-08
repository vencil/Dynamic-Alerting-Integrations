#!/usr/bin/env python3
"""check_threshold_observed_map.py — Drift-guard for the threshold observed-map (#719).

Why this exists
---------------
``scripts/tools/ops/metric_observed_map.yaml`` is the SoT mapping
``conf.d threshold key -> observed recording-rule series`` that
``threshold-recommend`` uses to query the OBSERVED workload (not the
configured ``user_threshold``, which was the prod bug / echo chamber — #719).

The map is GENERATED from the rule-pack alert rules, but is committed (so it
can carry human-resolved ``observed_series`` for composite alerts). That makes
it driftable: a domain expert can rename a recording rule or add an alert and
forget to regenerate the map. This guard is the codified防腐 gate — same
"generate → commit → CI drift-gate" pattern as the ADR-024 configmap/operator
hard gates.

What it checks (via _observed_map_lib.check_consistency)
--------------------------------------------------------
- **errors (FAIL)**: a mapped ``(key, observed_series)`` pair not found together
  in any rule-pack alert (stale map), OR ``scope`` inconsistent with the
  observed-series prefix (split-brain). These fail CI.
- **infos (OK)**: known-deferred keys (KNOWN_DEFERRED allowlist, e.g. the
  version-aware ``container_cpu`` / ``container_memory`` whose comparison lives
  in a ``:core`` recording rule, deferred to #916). Printed as INFO,
  NEVER an error — otherwise this bugfix could never merge. EXCEPTION
  (#916 Item B exit-lock): the *deferral itself* is enforced — a deferred key
  that becomes alert-extractable, disappears from the rule packs, or is
  hand-added to the map IS a hard error (``enforce_known_deferred=True`` on the
  real-map path), so the allowlist can't silently rot.
- **orphan_thresholds (WARN)**: a ``record: tenant:alert_threshold:<key>`` with
  NO alert referencing it — a rule-pack gap, surfaced for the pack authors, not
  a map bug.
- **coverage_gaps (WARN)**: alert-referenced keys absent from the map and not
  known-deferred — genuine extractor gaps worth a regenerate.

Severity model
--------------
``--ci`` exits 1 ONLY on ``errors``. infos/orphans/gaps are reported but never
fail CI (Gemini final-check: unmapped known-deferred keys must not red-gate the
PR). Without ``--ci`` it is report-only (exit 0).

Lint class & scope (lint-policy.md §3)
--------------------------------------
Class (a) — cross-artifact consistency (map <-> rule packs). Full-scan by
nature (the map is a single SoT file, not a diff target).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# ops lib (the shared extractor + consistency checker)
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "tools" / "ops"))
import _observed_map_lib as observed_map_lib  # noqa: E402

# try_utf8_stdout from the shared compat lib (cp950/cp936 console safety).
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, str(_THIS_DIR))
sys.path.insert(0, os.path.join(str(_THIS_DIR), ".."))
from _lib_compat import try_utf8_stdout  # noqa: E402
from _lib_exitcodes import EXIT_OK, EXIT_VIOLATION  # noqa: E402


def run_check() -> dict[str, list[str]]:
    """Load the committed map + rule packs and return the consistency result.

    Passes ``enforce_known_deferred=True`` so the KNOWN_DEFERRED allowlist is
    exit-locked on the REAL map (a deferred key becoming alert-extractable /
    disappearing from the packs / hand-added to the map is a hard error). The
    hermetic unit tests call ``check_consistency`` without this flag.
    """
    observed_map = observed_map_lib.load_observed_map()
    packs = observed_map_lib.default_pack_paths()
    return observed_map_lib.check_consistency(
        observed_map, packs, enforce_known_deferred=True
    )


def main(argv: list[str] | None = None) -> int:
    try_utf8_stdout()
    parser = argparse.ArgumentParser(
        description="Drift-guard for the threshold observed-map (#719)",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Exit 1 on hard drift (errors). infos/orphans/gaps never fail CI.",
    )
    args = parser.parse_args(argv)

    result = run_check()
    errors = result["errors"]
    infos = result["infos"]
    orphans = result["orphan_thresholds"]
    gaps = result["coverage_gaps"]

    for msg in infos:
        print(f"[INFO] {msg}")
    for key in orphans:
        print(
            f"[WARN] orphan threshold '{key}': has a recording rule but no alert "
            f"references it — rule-pack gap (not a map bug)."
        )
    for key in gaps:
        print(
            f"[WARN] coverage gap '{key}': referenced by an alert but absent from "
            f"the observed-map — run `threshold-recommend --generate-observed-map`."
        )

    if errors:
        print(
            f"\n[FAIL] {len(errors)} observed-map drift error(s):",
            file=sys.stderr,
        )
        for msg in errors:
            print(f"  - {msg}", file=sys.stderr)
        print(
            "\nFix: re-run `da-tools threshold-recommend --generate-observed-map` "
            "and re-resolve any needs_review entries, then commit the updated\n"
            "scripts/tools/ops/metric_observed_map.yaml.",
            file=sys.stderr,
        )
        return EXIT_VIOLATION if args.ci else EXIT_OK

    print(
        f"\n[OK] observed-map consistent "
        f"({len(infos)} known-deferred, {len(orphans)} orphan, {len(gaps)} gap)."
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
