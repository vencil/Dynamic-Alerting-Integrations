"""Mutation-test pilot runner for Go-side pure functions in
`components/threshold-exporter/app/pkg/config`.

Mirrors the design of `_mutation_pilot.py` (Python pilot, 67/70
caught at 31 functions per #333). Underscored prefix → pytest does
NOT collect this module; it's a re-runnable research artifact, not
part of the test suite.

Why hand-crafted vs `gremlins.dev` / `go-mutesting`
---------------------------------------------------

Same rationale as the Python pilot:

  - Avoid adding a dev-only Go dependency for a pilot whose value
    is the methodology demo + the catalog of MEANINGFUL mutations.
  - Hand-crafted mutations focus on constants / operators / control
    flow that map to real bug classes (off-by-one in time-window
    boundary, missing nil check, swapped merge priority). Auto
    mutators produce many equivalent-mutant noise.
  - Output of this script is the audit's reproducible evidence.

Targets
-------

`pkg/config/parse.go`
  - parseHHMM         — pure HH:MM parser, range-checked (5 muts)
  - matchTimeWindow   — same/cross-midnight branch (4 muts)
  - parsePromDuration — Prometheus-style "5m" / "4h" / "2d" parser (2 muts)

`pkg/config/hierarchy.go`
  - deepMerge         — ADR-018 inheritance, _metadata skip, nil-delete (3 muts)
  - extractDefaultsBlock — pulls `defaults:` sub-tree, falls back to root (1 mut)

Total: 15 mutations across 5 functions. Existing Go tests
(`parse_test.go`, `hierarchy_test.go`, `config_dimensional_test.go`,
golden-parity tests) are the kill targets.

Usage
-----

  # In Dev Container (preferred — Go toolchain available):
  make dc-run CMD="python tests/shared/_go_mutation_pilot.py"

  # Local (requires Go installed at /usr/local/go or PATH):
  python tests/shared/_go_mutation_pilot.py [--target FUNC]

The runner expects to be invoked from the repo root. It chdirs into
`components/threshold-exporter/app/` to run `go test ./pkg/config/...`.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
GO_APP_DIR = REPO_ROOT / "components" / "threshold-exporter" / "app"


@dataclass
class Mutation:
    target_file: str        # source file relative to GO_APP_DIR
    test_target: str        # `go test` package selector
    label: str              # short description
    old: str                # exact string to find
    new: str                # replacement
    fn_name: str            # which target function

    def apply(self) -> None:
        path = GO_APP_DIR / self.target_file
        with open(path, encoding="utf-8", newline="") as f:
            src = f.read()
        if self.old not in src:
            raise ValueError(
                f"old_string not found in {self.target_file}: {self.label}"
            )
        if src.count(self.old) > 1:
            raise ValueError(
                f"old_string ambiguous (>1 match) in {self.target_file}: {self.label}"
            )
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(src.replace(self.old, self.new))

    def revert(self, original: str) -> None:
        path = GO_APP_DIR / self.target_file
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(original)


# ── Mutation catalog ──────────────────────────────────────────────────

MUTATIONS: list[Mutation] = [
    # ── parseHHMM (parse.go) ─────────────────────────────────────────
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./pkg/config/...",
        label="parseHHMM: drop hour upper bound (h>23 accepted)",
        fn_name="parseHHMM",
        old="if err != nil || h < 0 || h > 23 {",
        new="if err != nil || h < 0 {",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./pkg/config/...",
        label="parseHHMM: drop minute upper bound (m>59 accepted)",
        fn_name="parseHHMM",
        old="if err != nil || m < 0 || m > 59 {",
        new="if err != nil || m < 0 {",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./pkg/config/...",
        label="parseHHMM: drop hour lower bound (h<0 accepted, e.g. -5)",
        fn_name="parseHHMM",
        old="if err != nil || h < 0 || h > 23 {",
        new="if err != nil || h > 23 {",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./pkg/config/...",
        label="parseHHMM: drop format split check (single-token input passes)",
        fn_name="parseHHMM",
        old='if len(parts) != 2 {\n\t\treturn 0, 0, fmt.Errorf("invalid HH:MM format: %q", s)\n\t}',
        new='if false {\n\t\treturn 0, 0, fmt.Errorf("invalid HH:MM format: %q", s)\n\t}',
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./pkg/config/...",
        label="parseHHMM: drop TrimSpace (leading whitespace breaks parse)",
        fn_name="parseHHMM",
        old="s = strings.TrimSpace(s)\n\tparts := strings.SplitN(s, \":\", 2)",
        new="parts := strings.SplitN(s, \":\", 2)",
    ),
    # ── matchTimeWindow (parse.go) ───────────────────────────────────
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./pkg/config/...",
        label="matchTimeWindow: invert same-day end-bound (< → <=)",
        fn_name="matchTimeWindow",
        old="return nowMinutes >= startMinutes && nowMinutes < endMinutes",
        new="return nowMinutes >= startMinutes && nowMinutes <= endMinutes",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./pkg/config/...",
        label="matchTimeWindow: invert cross-midnight branch (or → and)",
        fn_name="matchTimeWindow",
        old="return nowMinutes >= startMinutes || nowMinutes < endMinutes",
        new="return nowMinutes >= startMinutes && nowMinutes < endMinutes",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./pkg/config/...",
        label="matchTimeWindow: swap branch condition (always cross-midnight)",
        fn_name="matchTimeWindow",
        old="if startMinutes <= endMinutes {",
        new="if startMinutes > endMinutes {",
    ),
    # ── parsePromDuration (parse.go) ─────────────────────────────────
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./pkg/config/...",
        label="parsePromDuration: 'd' unit returns hours instead of days",
        fn_name="parsePromDuration",
        old="return time.Duration(num * 24 * float64(time.Hour)), nil",
        new="return time.Duration(num * float64(time.Hour)), nil",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./pkg/config/...",
        label="parsePromDuration: drop length check (1-char input crashes)",
        fn_name="parsePromDuration",
        old="if len(s) < 2 {\n\t\treturn 0, fmt.Errorf(\"duration too short: %q\", s)\n\t}",
        new="if false {\n\t\treturn 0, fmt.Errorf(\"duration too short: %q\", s)\n\t}",
    ),
    # ── deepMerge (hierarchy.go) ─────────────────────────────────────
    Mutation(
        target_file="pkg/config/hierarchy.go",
        test_target="./pkg/config/...",
        label="deepMerge: drop _metadata skip (override _metadata leaks into base)",
        fn_name="deepMerge",
        old='if k == "_metadata" {\n\t\t\tcontinue\n\t\t}',
        new='if false {\n\t\t\tcontinue\n\t\t}',
    ),
    Mutation(
        target_file="pkg/config/hierarchy.go",
        test_target="./pkg/config/...",
        label="deepMerge: drop nil-delete (override:nil overwrites with nil instead of deleting)",
        fn_name="deepMerge",
        old="if v == nil {\n\t\t\tdelete(result, k)\n\t\t\tcontinue\n\t\t}",
        new="if false {\n\t\t\tdelete(result, k)\n\t\t\tcontinue\n\t\t}",
    ),
    Mutation(
        target_file="pkg/config/hierarchy.go",
        test_target="./pkg/config/...",
        label="deepMerge: skip recursive merge for nested maps (override replaces wholesale)",
        fn_name="deepMerge",
        old="if overrideMap, ok := v.(map[string]any); ok {\n\t\t\tif baseMap, ok2 := result[k].(map[string]any); ok2 {\n\t\t\t\tresult[k] = deepMerge(baseMap, overrideMap)\n\t\t\t\tcontinue\n\t\t\t}\n\t\t}",
        new="if false {\n\t\t\tif baseMap, ok2 := result[k].(map[string]any); ok2 {\n\t\t\t\tresult[k] = deepMerge(baseMap, overrideMap)\n\t\t\t\tcontinue\n\t\t\t}\n\t\t}",
    ),
    # ── extractDefaultsBlock (hierarchy.go) ──────────────────────────
    Mutation(
        target_file="pkg/config/hierarchy.go",
        test_target="./pkg/config/...",
        label="extractDefaults: return nil instead of root fallback (no `defaults:` key → nil)",
        fn_name="extractDefaultsBlock",
        old='if inner, ok := m["defaults"].(map[string]any); ok {\n\t\treturn inner\n\t}\n\treturn m',
        new='if inner, ok := m["defaults"].(map[string]any); ok {\n\t\treturn inner\n\t}\n\treturn nil',
    ),
]


def _go_executable() -> str:
    """Locate `go` on PATH; fail fast with helpful error otherwise."""
    go = shutil.which("go")
    if not go:
        sys.stderr.write(
            "ERROR: `go` not on PATH. Run inside Dev Container:\n"
            "  make dc-run CMD=\"python tests/shared/_go_mutation_pilot.py\"\n"
        )
        sys.exit(2)
    return go


def run_tests(test_target: str) -> tuple[int, str]:
    """Run `go test` against the package; return (returncode, output_tail)."""
    go = _go_executable()
    cmd = [go, "test", test_target, "-count=1", "-timeout", "60s"]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(GO_APP_DIR),
        timeout=180, encoding="utf-8", errors="replace",
    )
    tail_lines = (proc.stdout + proc.stderr).splitlines()[-3:]
    return proc.returncode, " | ".join(tail_lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        help="Filter to mutations whose fn_name contains this substring",
    )
    args = parser.parse_args()

    selected = [m for m in MUTATIONS if not args.target or args.target in m.fn_name]
    print(f"Running {len(selected)} Go mutations from {GO_APP_DIR}\n")

    results: list[tuple[Mutation, str]] = []
    for i, m in enumerate(selected, 1):
        path = GO_APP_DIR / m.target_file
        with open(path, encoding="utf-8", newline="") as f:
            original = f.read()

        try:
            m.apply()
        except ValueError as e:
            results.append((m, f"SETUP-FAIL: {e}"))
            continue

        try:
            rc, tail = run_tests(m.test_target)
            verdict = "CAUGHT" if rc != 0 else "SURVIVED"
            results.append((m, f"{verdict} (rc={rc}) :: {tail[:160]}"))
        finally:
            m.revert(original)

        print(f"[{i:2d}/{len(selected)}] {m.fn_name}: {m.label[:60]}")
        print(f"      → {results[-1][1]}\n")

    # Summary
    caught = sum(1 for _, v in results if v.startswith("CAUGHT"))
    survived = sum(1 for _, v in results if v.startswith("SURVIVED"))
    setup_fail = sum(1 for _, v in results if v.startswith("SETUP-FAIL"))
    print(
        f"\n=== SUMMARY: {caught}/{len(results)} caught, "
        f"{survived} survived, {setup_fail} setup-failures ===\n"
    )

    if survived:
        print("SURVIVING MUTATIONS (test gaps):")
        for m, v in results:
            if v.startswith("SURVIVED"):
                print(f"  - {m.fn_name}: {m.label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
