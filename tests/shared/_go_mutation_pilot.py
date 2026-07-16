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
  - parseHHMM         — pure HH:MM parser, range-checked (6 muts)
  - matchTimeWindow   — same/cross-midnight branch (3 muts)
  - parsePromDuration — Prometheus-style "5m" / "4h" / "2d" parser (2 muts)

`pkg/config/hierarchy.go`
  - deepMerge         — ADR-017 inheritance, _metadata skip, nil-delete (3 muts)
  - extractDefaultsBlock — pulls `defaults:` sub-tree, falls back to root (1 mut)

Total: 15 mutations across 5 functions. Existing Go tests in the
parent `package main` (e.g., config_three_state_test.go for
parseHHMM, config_hierarchy_test.go for deepMerge, golden-parity
tests) are the kill targets — the lowercase functions in
`pkg/config` are exercised indirectly via the lowercase wrappers
in `app/config_inheritance.go`. That's why the runner uses
`go test ./...` from `app/` instead of `./pkg/config/...`: the
in-package tests for `pkg/config` only cover scope-resolution +
benchmarks, not the parse/merge primitives.

Run history
-----------

  PR #348 (initial): 12/14 caught (~86%). 2 survivors:
    - parseHHMM: drop hour lower bound — REAL gap
    - parseHHMM: drop outer TrimSpace — equivalent (see below)

  This PR (gap closure): expects 14/15 caught (~93%). Closes the
  hour-lower-bound gap by adding "-5:00" / "12:-5" cases to
  TestParseHHMM, plus adds a symmetric "drop minute lower bound"
  mutation that the new test cases also cover. The outer-TrimSpace
  mutation is now KNOWN-EQUIVALENT and stays as a documented
  noise-bin entry — no test can kill it without overspecifying
  redundant trimming behavior the inner `strings.TrimSpace(parts[i])`
  already provides.

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
TENANT_API_DIR = REPO_ROOT / "components" / "tenant-api"

# Module key → module root dir. The root is BOTH the base for a mutation's
# target_file AND the cwd `go test` runs from (each is its own Go module with
# its own go.mod, so the test package selector is resolved relative to it).
# "exporter" stays the default so every pre-round-4 catalog entry — which
# omits the module field — keeps resolving against threshold-exporter/app
# byte-identically. Round 4 (ROI refactor) adds the "tenant-api" module to
# cover the RBAC/identity pure functions (LD-6 security core).
GO_MODULES: dict[str, Path] = {
    "exporter": GO_APP_DIR,
    "tenant-api": TENANT_API_DIR,
}


@dataclass
class Mutation:
    target_file: str        # source file relative to the module root (module_dir)
    test_target: str        # `go test` package selector (relative to module_dir)
    label: str              # short description
    old: str                # exact string to find
    new: str                # replacement
    fn_name: str            # which target function
    # Which Go module the target lives in (key into GO_MODULES). Defaults to
    # "exporter" so existing entries stay unchanged; tenant-api entries set it
    # explicitly. Governs BOTH target_file resolution and the `go test` cwd.
    module: str = "exporter"
    # True = documented equivalent mutation (survives by construction, no
    # behavioral test can kill it without overspecifying the impl). Known
    # equivalents do NOT fail the run — see main()'s exit contract.
    known_equivalent: bool = False

    def module_dir(self) -> Path:
        """Root dir of the Go module this mutation targets (base for
        target_file resolution AND the `go test` cwd)."""
        return GO_MODULES[self.module]

    def apply(self) -> None:
        path = self.module_dir() / self.target_file
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
        path = self.module_dir() / self.target_file
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(original)


# ── Mutation catalog ──────────────────────────────────────────────────

MUTATIONS: list[Mutation] = [
    # ── parseHHMM (parse.go) ─────────────────────────────────────────
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="parseHHMM: drop hour upper bound (h>23 accepted)",
        fn_name="parseHHMM",
        old="if err != nil || h < 0 || h > 23 {",
        new="if err != nil || h < 0 {",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="parseHHMM: drop minute upper bound (m>59 accepted)",
        fn_name="parseHHMM",
        old="if err != nil || m < 0 || m > 59 {",
        new="if err != nil || m < 0 {",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="parseHHMM: drop hour lower bound (h<0 accepted, e.g. -5)",
        fn_name="parseHHMM",
        old="if err != nil || h < 0 || h > 23 {",
        new="if err != nil || h > 23 {",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="parseHHMM: drop minute lower bound (m<0 accepted, e.g. 12:-5)",
        fn_name="parseHHMM",
        old="if err != nil || m < 0 || m > 59 {",
        new="if err != nil || m > 59 {",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="parseHHMM: drop format split check (single-token input passes)",
        fn_name="parseHHMM",
        old='if len(parts) != 2 {\n\t\treturn 0, 0, fmt.Errorf("invalid HH:MM format: %q", s)\n\t}',
        new='if false {\n\t\treturn 0, 0, fmt.Errorf("invalid HH:MM format: %q", s)\n\t}',
    ),
    # NOTE: known equivalent mutation. The function applies
    # `strings.TrimSpace` again on each part after SplitN
    # (`strings.TrimSpace(parts[0])` / `parts[1]`), so the outer
    # TrimSpace is redundant — removing it doesn't change behavior
    # for any leading/trailing-whitespace input. Kept in the catalog
    # as a documented equivalent so future readers don't try to
    # "close" it by adding a redundant test.
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="parseHHMM: drop outer TrimSpace (KNOWN EQUIVALENT — inner TrimSpace covers it)",
        fn_name="parseHHMM",
        old="s = strings.TrimSpace(s)\n\tparts := strings.SplitN(s, \":\", 2)",
        new="parts := strings.SplitN(s, \":\", 2)",
        # Inner strings.TrimSpace(parts[i]) already trims each token, so the
        # outer TrimSpace is redundant for any whitespace-padded input.
        known_equivalent=True,
    ),
    # ── matchTimeWindow (parse.go) ───────────────────────────────────
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="matchTimeWindow: invert same-day end-bound (< → <=)",
        fn_name="matchTimeWindow",
        old="return nowMinutes >= startMinutes && nowMinutes < endMinutes",
        new="return nowMinutes >= startMinutes && nowMinutes <= endMinutes",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="matchTimeWindow: invert cross-midnight branch (or → and)",
        fn_name="matchTimeWindow",
        old="return nowMinutes >= startMinutes || nowMinutes < endMinutes",
        new="return nowMinutes >= startMinutes && nowMinutes < endMinutes",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="matchTimeWindow: swap branch condition (always cross-midnight)",
        fn_name="matchTimeWindow",
        old="if startMinutes <= endMinutes {",
        new="if startMinutes > endMinutes {",
    ),
    # ── parsePromDuration (parse.go) ─────────────────────────────────
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="parsePromDuration: 'd' unit returns hours instead of days",
        fn_name="parsePromDuration",
        old="return time.Duration(num * 24 * float64(time.Hour)), nil",
        new="return time.Duration(num * float64(time.Hour)), nil",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="parsePromDuration: drop length check (1-char input crashes)",
        fn_name="parsePromDuration",
        old="if len(s) < 2 {\n\t\treturn 0, fmt.Errorf(\"duration too short: %q\", s)\n\t}",
        new="if false {\n\t\treturn 0, fmt.Errorf(\"duration too short: %q\", s)\n\t}",
    ),
    # ── deepMerge (hierarchy.go) ─────────────────────────────────────
    Mutation(
        target_file="pkg/config/hierarchy.go",
        test_target="./...",
        label="deepMerge: drop _metadata skip (override _metadata leaks into base)",
        fn_name="deepMerge",
        old='if k == "_metadata" {\n\t\t\tcontinue\n\t\t}',
        new='if false {\n\t\t\tcontinue\n\t\t}',
    ),
    Mutation(
        target_file="pkg/config/hierarchy.go",
        test_target="./...",
        label="deepMerge: drop nil-delete (override:nil overwrites with nil instead of deleting)",
        fn_name="deepMerge",
        old="if v == nil {\n\t\t\tdelete(result, k)\n\t\t\tcontinue\n\t\t}",
        new="if false {\n\t\t\tdelete(result, k)\n\t\t\tcontinue\n\t\t}",
    ),
    Mutation(
        target_file="pkg/config/hierarchy.go",
        test_target="./...",
        label="deepMerge: skip recursive merge for nested maps (override replaces wholesale)",
        fn_name="deepMerge",
        # Drop the entire `if overrideMap, ok := ...` scope. Code falls
        # through to the existing `result[k] = deepCopyValue(v)` line,
        # which is the "always overwrite" semantic — same Go syntax,
        # different runtime behavior. (The previous version of this
        # mutation just stubbed the outer if to `if false`, leaving an
        # unused `overrideMap` reference inside that triggered a Go
        # compile error — that's a "caught for the wrong reason" false
        # positive.)
        old="if overrideMap, ok := v.(map[string]any); ok {\n\t\t\tif baseMap, ok2 := result[k].(map[string]any); ok2 {\n\t\t\t\tresult[k] = deepMerge(baseMap, overrideMap)\n\t\t\t\tcontinue\n\t\t\t}\n\t\t}\n\t\tresult[k] = deepCopyValue(v)",
        new="result[k] = deepCopyValue(v)",
    ),
    # ── extractDefaultsBlock (hierarchy.go) ──────────────────────────
    Mutation(
        target_file="pkg/config/hierarchy.go",
        test_target="./...",
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


def run_tests(test_target: str, cwd: Path) -> tuple[int, str]:
    """Run `go test` against the package from cwd (the target's module root);
    return (returncode, output_tail).

    cwd is the mutation's module_dir — each Go module (threshold-exporter/app,
    tenant-api) has its own go.mod, so the package selector must be resolved
    from that module's root, not a single hard-coded app dir.
    """
    go = _go_executable()
    # The exporter `./...` runs the full suite (parent `package main` + nested
    # pkg/config); the tenant-api entries scope to `./internal/rbac/...` etc.
    # The exporter integration tests use fsnotify debounce loops, so allow
    # several minutes per mutation.
    cmd = [go, "test", test_target, "-count=1", "-timeout", "180s"]
    proc = subprocess.run(
        cmd, capture_output=True, text=True, cwd=str(cwd),
        timeout=360, encoding="utf-8", errors="replace",
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
    if not selected:
        # A typo'd --target used to yield "0/0 caught" + rc 0 — a silent
        # no-op that looks green. Make it a hard, explained error instead.
        print(
            f"ERROR: --target {args.target!r} matched no mutation fn_name "
            f"(catalog has {len(MUTATIONS)} mutations; check the spelling "
            f"against MUTATIONS[].fn_name)",
            file=sys.stderr,
        )
        return 2
    print(f"Running {len(selected)} Go mutations across {len(GO_MODULES)} modules\n")

    results: list[tuple[Mutation, str]] = []
    for i, m in enumerate(selected, 1):
        path = m.module_dir() / m.target_file
        with open(path, encoding="utf-8", newline="") as f:
            original = f.read()

        try:
            m.apply()
        except ValueError as e:
            # Catalog rot (old_string drifted from source) — record AND print
            # per-item, so a rotted entry is visible in the run log instead of
            # being silently skipped (pre-2026-07 fail-open behavior).
            results.append((m, f"SETUP-FAIL: {e}"))
        else:
            try:
                rc, tail = run_tests(m.test_target, m.module_dir())
                if rc == 0:
                    results.append((m, f"SURVIVED (rc=0) :: {tail[:160]}"))
                elif rc == 1:
                    results.append((m, f"CAUGHT (rc=1) :: {tail[:160]}"))
                else:
                    # `go test` rc other than 0/1 (e.g. 2) = the runner
                    # itself failed — bad package selector, toolchain error —
                    # so the kill suite never ran. Bin with SETUP-FAIL, same
                    # catalog-rot class as a stale old_string. (A mutation
                    # that merely breaks compilation still exits 1 and stays
                    # a "caught for the wrong reason" case — see the
                    # deepMerge recursive-merge mutation's note.)
                    results.append((m, (
                        f"SETUP-FAIL: test runner rc={rc} — kill suite did "
                        f"not run (stale test_target? toolchain error) "
                        f":: {tail[:160]}"
                    )))
            finally:
                m.revert(original)

        print(f"[{i:2d}/{len(selected)}] {m.fn_name}: {m.label[:60]}")
        print(f"      → {results[-1][1]}\n")

    # Summary
    caught = sum(1 for _, v in results if v.startswith("CAUGHT"))
    survivors = [(m, v) for m, v in results if v.startswith("SURVIVED")]
    equivalent = [(m, v) for m, v in survivors if m.known_equivalent]
    new_survivors = [(m, v) for m, v in survivors if not m.known_equivalent]
    setup_fails = [(m, v) for m, v in results if v.startswith("SETUP-FAIL")]
    print(
        f"\n=== SUMMARY: {caught}/{len(results)} caught, "
        f"{len(survivors)} survived "
        f"({len(equivalent)} known-equivalent, {len(new_survivors)} NEW), "
        f"{len(setup_fails)} setup-failures ===\n"
    )

    if equivalent:
        print("KNOWN-EQUIVALENT SURVIVORS (documented noise bin, not failures):")
        for m, _ in equivalent:
            print(f"  - {m.fn_name}: {m.label}")
    if new_survivors:
        print("NEW SURVIVING MUTATIONS (real test gaps — close the gap or "
              "document equivalence via known_equivalent=True):")
        for m, _ in new_survivors:
            print(f"  - {m.fn_name}: {m.label}")
    if setup_fails:
        print("SETUP FAILURES (catalog rot — re-anchor the entry's old=/new= "
              "to the current source, or re-point a stale test reference):")
        for m, v in setup_fails:
            print(f"  - {m.fn_name}: {m.label}\n      {v}")

    # Exit contract (actionable-red): non-zero ONLY on a real signal — a NEW
    # (non-equivalent) survivor or catalog rot. Known equivalents keep the
    # nightly green so red always deserves investigation.
    if new_survivors or setup_fails:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
