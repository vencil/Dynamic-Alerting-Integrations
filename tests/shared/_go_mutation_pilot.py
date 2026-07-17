"""Mutation-test pilot runner for Go-side pure functions across two
modules:
  - `components/threshold-exporter/app/pkg/config` (the parse/merge
    primitives — the original round-1/2 scope), and
  - `components/tenant-api/internal/rbac` + `.../federation/token`
    (the RBAC / identity permission-evaluation core — round 4, the
    LD-6 security main battleground).

Each is a separate Go module (its own go.mod), so a mutation declares
which `module` it targets (GO_MODULES); that key selects BOTH the base
dir for its `target_file` and the cwd `go test` runs from.

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

MODULE "exporter" — `pkg/config/parse.go`
  - parseHHMM         — pure HH:MM parser, range-checked (6 muts)
  - matchTimeWindow   — same/cross-midnight branch (3 muts)
  - parsePromDuration — Prometheus-style "5m" / "4h" / "2d" parser (2 muts)

MODULE "exporter" — `pkg/config/hierarchy.go`
  - deepMerge         — ADR-017 inheritance, _metadata skip, nil-delete (3 muts)
  - extractDefaultsBlock — pulls `defaults:` sub-tree, falls back to root (1 mut)

MODULE "tenant-api" — `internal/rbac/rbac.go` (LD-6 permission core)
  - permCovers        — permission hierarchy admin⊇write⊇read (2 muts)
  - tenantMatches     — tenant wildcard/prefix matcher + "**" fail-open backstop (2 muts)
  - validTenantPattern— pattern allowlist that keeps "**" out of the matcher (2 muts)
  - scopeSetModes     — org-axis shadow/enforce set membership (3 muts)
  - scopeFieldModes   — metadata-axis shadow/enforce field match (1 mut)
  - metadataMatches   — pure env/domain membership (1 mut)

MODULE "tenant-api" — `internal/rbac/context.go` + `.../principal.go`
  - parseForwardedGroups — X-Forwarded-Groups splitter, empty-entry drop (1 mut)
  - ParseClaimHeaders — fail-loud --identity-claim-headers parser (2 muts)

MODULE "tenant-api" — `internal/federation/token/manager.go`
  - audienceFor       — capability→JWT audience (cross-plane replay guard) (1 mut)

Total: 30 mutations across 14 functions (15 exporter + 15 tenant-api).

Kill targets, exporter: existing Go tests in the parent `package main`
(e.g., config_three_state_test.go for parseHHMM, config_hierarchy_test.go
for deepMerge, golden-parity tests). The lowercase functions in
`pkg/config` are exercised indirectly via the lowercase wrappers in
`app/config_inheritance.go`, so the runner uses `go test ./...` from
`app/` (the in-package pkg/config tests only cover scope-resolution +
benchmarks, not the parse/merge primitives).

Kill targets, tenant-api: the round-3-reorganised rbac test files
(match_eval_test.go for tenantMatches/permCovers, org_scope_test.go for
scopeSetModes, metadata_scope_test.go for scopeFieldModes/metadataMatches,
principal_test.go for parseForwardedGroups/ParseClaimHeaders) and the
token manager_test.go for audienceFor. These are in-package tests, so
the runner scopes to `./internal/rbac/...` / `./internal/federation/token/...`.
Every tenant-api mutation models a permission-WIDENS-unexpectedly (fail-open)
bug class — the direction that leaks access — not a narrowing one.

Run history
-----------

  PR #348 (initial): 12/14 caught (~86%). 2 survivors:
    - parseHHMM: drop hour lower bound — REAL gap
    - parseHHMM: drop outer TrimSpace — equivalent (see below)

  #349 (gap closure): expects 14/15 caught (~93%). Closes the
  hour-lower-bound gap by adding "-5:00" / "12:-5" cases to
  TestParseHHMM, plus adds a symmetric "drop minute lower bound"
  mutation that the new test cases also cover. The outer-TrimSpace
  mutation is now KNOWN-EQUIVALENT and stays as a documented
  noise-bin entry — no test can kill it without overspecifying
  redundant trimming behavior the inner `strings.TrimSpace(parts[i])`
  already provides.

  Round 4 (ROI refactor, tenant-api scope): adds 15 mutations across
  9 RBAC/identity/token pure functions, all modelling a fail-open
  (permission-widens) bug class. Each was injection-verified in the Dev
  Container against `go test ./internal/rbac/...` /
  `./internal/federation/token/...`: mutation red -> revert green, all
  15 caught (0 survivors, 0 known-equivalent). Combined catalog: 30
  mutations, 29 caught + 1 known-equivalent survivor (the exporter
  outer-TrimSpace entry).

Usage
-----

  # In Dev Container (preferred — Go toolchain available):
  make dc-run CMD="python tests/shared/_go_mutation_pilot.py"

  # Local (requires Go installed at /usr/local/go or PATH):
  python tests/shared/_go_mutation_pilot.py [--target FUNC]

The runner expects to be invoked from the repo root. For each mutation it
runs `go test <test_target>` from the target's module root (module_dir):
`components/threshold-exporter/app/` for exporter entries,
`components/tenant-api/` for tenant-api entries.
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
    # Primary kill test OBSERVED red when this mutation was injection-verified
    # (rot-triage attribution: when a rename/refactor drifts the catalog, this
    # names where to look). Guarded by test_mutation_catalog.py's kill-test
    # lane: a non-None name must exist as `func <name>(` in a *_test.go under
    # the test_target scope, so a renamed kill test fails at PR time instead
    # of the attribution silently rotting in a comment. None = not (yet)
    # attributed: pre-round-4 exporter entries were verified per run history
    # at package level, so only those whose docstring/run-history names a
    # specific test carry one. Attribution only — the package-scope
    # test_target, not this field, decides what the runner executes.
    kill_test: str | None = None
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
        # #349 gap closure added the "-5:00" case to TestParseHHMM
        # specifically to kill this (former survivor) mutation.
        kill_test="TestParseHHMM",
    ),
    Mutation(
        target_file="pkg/config/parse.go",
        test_target="./...",
        label="parseHHMM: drop minute lower bound (m<0 accepted, e.g. 12:-5)",
        fn_name="parseHHMM",
        old="if err != nil || m < 0 || m > 59 {",
        new="if err != nil || m > 59 {",
        # #349 gap closure added the "12:-5" case to TestParseHHMM together
        # with this symmetric mutation.
        kill_test="TestParseHHMM",
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

    # ══════════════════════════════════════════════════════════════════════
    # MODULE "tenant-api" — RBAC / identity permission core (round 4)
    #
    # Every entry below models a FAIL-OPEN bug class: the mutation makes the
    # gate grant access it should refuse (a widened permission, a defeated
    # scope axis, an accepted malformed pattern, a cross-plane audience). The
    # opposite direction (accidental narrowing) is a correctness bug too but
    # not the one that leaks tenant data, so it is deliberately not the focus.
    # ══════════════════════════════════════════════════════════════════════

    # ── permCovers (rbac.go) — permission hierarchy admin ⊇ write ⊇ read ──
    Mutation(
        target_file="internal/rbac/rbac.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="permCovers: write check accepts a read-only grant (read→write privesc)",
        fn_name="permCovers",
        # bug class: a read-only rule would satisfy a write gate — the caller
        # mutates data with read credentials. Kill: TestPermCovers
        # "read not covers write".
        old="return grant == PermWrite || grant == PermAdmin",
        new="return grant == PermRead || grant == PermWrite || grant == PermAdmin",
        kill_test="TestPermCovers",
    ),
    Mutation(
        target_file="internal/rbac/rbac.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="permCovers: admin check accepts a write grant (write→admin privesc)",
        fn_name="permCovers",
        # bug class: a write rule would satisfy an admin gate — tenant operator
        # gains platform-admin actions. Kill: TestPermCovers
        # "write not covers admin".
        old="return grant == PermAdmin",
        new="return grant == PermAdmin || grant == PermWrite",
        kill_test="TestPermCovers",
    ),

    # ── tenantMatches (rbac.go) — tenant wildcard/prefix matcher ──────────
    Mutation(
        target_file="internal/rbac/rbac.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="tenantMatches: drop malformed-prefix backstop (\"**\" fails open onto platform gate)",
        fn_name="tenantMatches",
        # bug class: without the backstop a rule with tenants ["**"] collapses
        # to prefix "*" and HasPrefix("*","*")==true, so the rule matches the
        # platform-scope "*" gate query while granting zero real per-tenant
        # access — a fail-open inconsistency. Kill: TestTenantMatches
        # "double-star vs platform gate does not fail open".
        old='if prefix == "" || strings.Contains(prefix, "*") {\n\t\t\t\tcontinue\n\t\t\t}',
        new="if false {\n\t\t\t\tcontinue\n\t\t\t}",
        kill_test="TestTenantMatches",
    ),
    Mutation(
        target_file="internal/rbac/rbac.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="tenantMatches: prefix match uses HasSuffix instead of HasPrefix (wrong tenants match)",
        fn_name="tenantMatches",
        # bug class: a "db-a-*" rule would match by suffix, granting unrelated
        # tenants whose id ENDS with the literal — matching semantics inverted.
        # Kill (observed at injection): TestReverseAccessReport_SmokeHappyPath.
        # TestTenantMatches' "prefix match" row also pins this semantic, but
        # under this mutation the test binary panic-aborts first (index panic
        # in TestReverseAccessReport_ModesAndRedact, reverse_smoke_test.go),
        # so the smoke test is the red actually seen before the abort.
        old="if strings.HasPrefix(tenantID, prefix) {",
        new="if strings.HasSuffix(tenantID, prefix) {",
        kill_test="TestReverseAccessReport_SmokeHappyPath",
    ),

    # ── validTenantPattern (rbac.go) — the allowlist keeping "**" out ─────
    Mutation(
        target_file="internal/rbac/rbac.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="validTenantPattern: accept repeated-\"*\" patterns (\"**\"/\"*a*\" pass validation)",
        fn_name="validTenantPattern",
        # bug class: validateConfig would accept "**" at load, which then reaches
        # tenantMatches and (per the entry above) fails open onto platform gates.
        # Kill: TestValidateConfig_TenantPatterns "double star rejected".
        old='return false // repeated "*"',
        new='return true // repeated "*"',
        kill_test="TestValidateConfig_TenantPatterns",
    ),
    Mutation(
        target_file="internal/rbac/rbac.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="validTenantPattern: accept blank/whitespace exact id (empty tenant pattern valid)",
        fn_name="validTenantPattern",
        # bug class: a blank tenant entry would pass validation as a valid exact
        # id — an authoring mistake silently accepted rather than failing loud.
        # Kill: TestValidateConfig_TenantPatterns "empty entry rejected".
        old="return strings.TrimSpace(pat) != \"\"",
        new="return true",
        kill_test="TestValidateConfig_TenantPatterns",
    ),

    # ── scopeSetModes (rbac.go) — org-axis shadow/enforce membership ──────
    Mutation(
        target_file="internal/rbac/rbac.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="scopeSetModes: unlabeled tenant passes ENFORCE (org scope defeated, the exact leak)",
        fn_name="scopeSetModes",
        # bug class: an org-unlabeled tenant would be granted even under enforce,
        # so org-scope grants every unassigned tenant to every caller — the leak
        # org-scope exists to prevent. Kill: TestScopeSetModes
        # "unlabeled (nil): shadow yes, enforce no".
        old="if len(tenantOrgs) == 0 {\n\t\treturn true, false",
        new="if len(tenantOrgs) == 0 {\n\t\treturn true, true",
        kill_test="TestScopeSetModes",
    ),
    Mutation(
        target_file="internal/rbac/rbac.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="scopeSetModes: caller with no org claim matches a LABELED tenant (shadow fail-open)",
        fn_name="scopeSetModes",
        # bug class: a caller carrying no org claim would still match a labeled
        # tenant in shadow mode — no basis to match, yet granted. Kill:
        # TestScopeSetModes "labeled + no caller claim: both no".
        old='if userOrgVal == "" {\n\t\treturn false, false',
        new='if userOrgVal == "" {\n\t\treturn true, false',
        kill_test="TestScopeSetModes",
    ),
    Mutation(
        target_file="internal/rbac/rbac.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="scopeSetModes: org membership test inverted (== → !=; non-member matches)",
        fn_name="scopeSetModes",
        # bug class: the caller's org value would match every tenant org it is
        # NOT a member of — a caller sees exactly the orgs it does not belong to.
        # Kill: TestScopeSetModes "labeled non-member: both no".
        old="if o == userOrgVal {",
        new="if o != userOrgVal {",
        kill_test="TestScopeSetModes",
    ),

    # ── scopeFieldModes (rbac.go) — metadata-axis shadow/enforce ──────────
    Mutation(
        target_file="internal/rbac/rbac.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="scopeFieldModes: unlabeled tenant passes ENFORCE on a restricted field (fail-open)",
        fn_name="scopeFieldModes",
        # bug class: an env/domain-unlabeled tenant would stay visible even under
        # enforce, so the metadata scope filter never actually hides anything.
        # Kill: TestScopeFieldModes "unlabeled on restricted: shadow yes, enforce no".
        old='if value == "" {\n\t\treturn true, false',
        new='if value == "" {\n\t\treturn true, true',
        kill_test="TestScopeFieldModes",
    ),

    # ── metadataMatches (rbac.go) — pure env/domain membership ────────────
    Mutation(
        target_file="internal/rbac/rbac.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="metadataMatches: membership test inverted (== → !=; non-members match)",
        fn_name="metadataMatches",
        # bug class: a tenant whose env/domain is NOT in the allow-list would
        # pass the filter — the restriction becomes an anti-restriction. Kill:
        # TestMetadataMatches "value not in allowList".
        old="if allowed == value {",
        new="if allowed != value {",
        kill_test="TestMetadataMatches",
    ),

    # ── parseForwardedGroups (context.go) — group header splitter ─────────
    Mutation(
        target_file="internal/rbac/context.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="parseForwardedGroups: keep empty group entries (empty group name enters the subject set)",
        fn_name="parseForwardedGroups",
        # bug class: an empty group "" would enter the caller's group set —
        # e.g. an absent X-Forwarded-Groups header splits to [""] — so any
        # config shape whose effective matched name is empty (groupSet[""])
        # would match every request. Defense-in-depth framing: this is ONE
        # layer of a two-layer guard — the config layer independently rejects
        # a blank match.groups entry at load (validateConfig, pinned by
        # TestValidateConfig_Branches), so full fail-open via THAT shape needs
        # both layers wrong. The parser-layer invariant stands on its own:
        # the subject set must never contain manufactured empty names,
        # regardless of what config shapes could consume them. Kill:
        # TestHeaderResolver_NoGroups (absent header must yield 0 groups).
        old='if g != "" {\n\t\t\tgroups = append(groups, g)\n\t\t}',
        new="if true {\n\t\t\tgroups = append(groups, g)\n\t\t}",
        kill_test="TestHeaderResolver_NoGroups",
    ),

    # ── ParseClaimHeaders (principal.go) — fail-loud config parser ────────
    Mutation(
        target_file="internal/rbac/principal.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="ParseClaimHeaders: drop duplicate-key guard (later pair silently overwrites)",
        fn_name="ParseClaimHeaders",
        # bug class: a duplicate claim key would silently take its last value
        # instead of failing loud, so the operator's declared axis is not the
        # one enforced. Kill: TestParseClaimHeaders_MalformedIsError "duplicate key".
        old="if _, dup := out[key]; dup {",
        new="if false {",
        kill_test="TestParseClaimHeaders_MalformedIsError",
    ),
    Mutation(
        target_file="internal/rbac/principal.go",
        test_target="./internal/rbac/...",
        module="tenant-api",
        label="ParseClaimHeaders: drop header-name charset guard (unreachable claim axis accepted)",
        fn_name="ParseClaimHeaders",
        # bug class: a header name a request can never carry (spaces, embedded
        # '=') would be accepted, leaving the claim axis silently absent at
        # runtime — a rule keyed on it can never match yet loads clean. Kill:
        # TestParseClaimHeaders_MalformedIsError "space in header name".
        old="if !headerNameRe.MatchString(header) {",
        new="if false {",
        kill_test="TestParseClaimHeaders_MalformedIsError",
    ),

    # ── audienceFor (federation/token/manager.go) — cross-plane replay ────
    Mutation(
        target_file="internal/federation/token/manager.go",
        test_target="./internal/federation/token/...",
        module="tenant-api",
        label="audienceFor: logs token gets the metrics audience (cross-plane replay)",
        fn_name="audienceFor",
        # bug class: a logs-plane token would be signed with the metrics
        # audience, so it could be replayed against the metrics proxy (the exact
        # confusion the distinct audience exists to prevent). Kill:
        # manager_test.go logs-plane `aud == audienceLogs` assertion.
        old="if c == CapLogs {\n\t\treturn audienceLogs\n\t}",
        new="if c == CapLogs {\n\t\treturn audienceMetrics\n\t}",
        kill_test="TestIssueLogs_EmbedsAccountIDAndLogsAudience",
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
    # Count the DISTINCT modules of the selected set — a --target filter can
    # narrow the run to one module, and printing the full GO_MODULES count
    # there would misreport the run's actual scope.
    n_modules = len({m.module for m in selected})
    print(f"Running {len(selected)} Go mutations across {n_modules} module(s)\n")

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
