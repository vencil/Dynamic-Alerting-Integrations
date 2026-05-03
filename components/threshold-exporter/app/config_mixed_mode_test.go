package main

// ============================================================
// B-5 — Mixed-mode 驗證 (v2.8.0 Phase B Track B)
// ============================================================
//
// "Mixed mode" = a conf.d/ tree containing BOTH:
//   - flat layout: tenant YAMLs at root (`<root>/<tenant>.yaml`)
//   - hierarchical layout: tenant YAMLs nested under domain/region/env
//     (`<root>/<domain>/<region>/<env>/<tenant>.yaml`)
//
// Mixed mode is the *transient* state during a flat→hierarchical
// migration (per ADR-017 + docs/scenarios/incremental-migration-
// playbook.md). The platform must:
//
//  1. Apply root `_defaults.yaml` to BOTH flat tenants and nested
//     tenants (root defaults is the only level both share).
//  2. Reject a tenant ID appearing in both layouts — silent dedup
//     would mask migration bugs.
//  3. Scope blast-radius emissions correctly: a mid-level
//     `_defaults.yaml` change must NOT count flat tenants as
//     affected (their defaults chain doesn't include that level).
//  4. Tolerate hot-migration (mv flat → nested + introduce
//     `_defaults.yaml`) without spurious duplicate errors after
//     the move completes.
//  5. Stay in `hierarchicalMode=true` once flipped — even if every
//     `_defaults.yaml` is later deleted, do not revert to the flat
//     scan path (one-way switch, prevents thrash).
//
// Existing coverage at scan-primitive level lives in
// config_hierarchy_test.go (TestScanDirHierarchical_MixedMode +
// _DuplicateTenant). These tests exercise the same invariants
// at the **manager** level (`ConfigManager.Load()` + `diffAndReload`
// + `populateHierarchyState`) so production hot paths are covered.

import (
	"bytes"
	"errors"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeMixedModeFlatRootDefaults builds a baseline mixed-mode tree:
//
//	<root>/_defaults.yaml                          (root, mysql=80 redis=50)
//	<root>/db-flat.yaml                            (flat tenant, override redis=60)
//	<root>/finance/_defaults.yaml                  (mid-level, region_alert_schedule="08-22")
//	<root>/finance/db-fin.yaml                     (nested tenant, override mysql=900)
//
// Both tenants inherit root defaults; only the nested one inherits
// finance/_defaults too. Used by Tests 1 + 3.
func writeMixedModeFixture(t *testing.T, root string) {
	t.Helper()
	writeTestYAML(t, filepath.Join(root, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
  redis_connections: 50
`)
	writeTestYAML(t, filepath.Join(root, "db-flat.yaml"), `
tenants:
  db-flat:
    redis_connections: "60"
`)
	if err := os.MkdirAll(filepath.Join(root, "finance"), 0o755); err != nil {
		t.Fatalf("mkdir finance: %v", err)
	}
	writeTestYAML(t, filepath.Join(root, "finance", "_defaults.yaml"), `
defaults:
  region_alert_schedule: 5
`)
	writeTestYAML(t, filepath.Join(root, "finance", "db-fin.yaml"), `
tenants:
  db-fin:
    mysql_connections: "900"
`)
}

// ─────────────────────────────────────────────────────────────────
// Test 1 — RootDefaultsCascadeToBoth
// Root `_defaults.yaml` must apply to both flat AND nested tenants
// (it's the one level they share). Without this invariant, flat
// tenants would silently miss platform-wide defaults during the
// migration window.
// ─────────────────────────────────────────────────────────────────

func TestMixedMode_RootDefaultsCascadeToBoth(t *testing.T) {
	root := t.TempDir()
	writeMixedModeFixture(t, root)

	mgr := NewConfigManager(root)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	// Both tenants should be discovered.
	mgr.mu.RLock()
	graph := mgr.hierarchy.graph
	mgr.mu.RUnlock()
	if graph == nil {
		t.Fatal("inheritanceGraph nil; populateHierarchyState should have built it")
	}

	// Flat tenant's chain: only root _defaults.yaml.
	flatChain := graph.TenantDefaults["db-flat"]
	if len(flatChain) != 1 {
		t.Errorf("db-flat: expected 1-element chain (root only), got %d: %v", len(flatChain), flatChain)
	} else {
		want := filepath.Clean(filepath.Join(root, "_defaults.yaml"))
		if flatChain[0] != want {
			t.Errorf("db-flat chain[0] = %q, want %q", flatChain[0], want)
		}
	}

	// Nested tenant's chain: root + finance/_defaults.yaml.
	hierChain := graph.TenantDefaults["db-fin"]
	if len(hierChain) != 2 {
		t.Errorf("db-fin: expected 2-element chain (root + finance), got %d: %v", len(hierChain), hierChain)
	}

	// merged_hash present for both — proves both went through
	// computeMergedHash with the chain applied.
	mgr.mu.RLock()
	flatHash := mgr.hierarchy.mergedHashes["db-flat"]
	hierHash := mgr.hierarchy.mergedHashes["db-fin"]
	mgr.mu.RUnlock()
	if flatHash == "" {
		t.Error("db-flat: merged_hash empty; root defaults didn't apply")
	}
	if hierHash == "" {
		t.Error("db-fin: merged_hash empty; cascading defaults didn't apply")
	}
	if flatHash == hierHash {
		t.Error("db-flat and db-fin have identical merged_hash; chains should differ")
	}
}

// ─────────────────────────────────────────────────────────────────
// Test 2 — DuplicateAcrossModes_RejectedAtLoad (issue #127, v2.8.x)
// Same tenant ID in both flat (`<root>/<id>.yaml`) and nested
// (`<root>/<dir>/<id>.yaml`). v2.8.x hardening: `Load()` must
// REJECT this misconfig hard rather than silently last-wins-merge.
//
// History (pre-v2.8.x gap, recorded for context):
//   - scanDirHierarchical correctly detected the duplicate but
//     returned a generic fmt.Errorf
//   - Load()'s populateHierarchyState call ran AFTER `m.config = &cfg`
//     and swallowed the error with WARN log
//   - Customer could deploy with a duplicate silently merged via map
//     last-wins iteration — easy to miss in production
//
// v2.8.x contract (issue #127):
//   - scanDirHierarchical returns typed *DuplicateTenantError
//   - Load() / fullDirLoad() detect the typed error and propagate it
//     wrapped, BEFORE committing flat state — so on cold start
//     m.config / m.loaded stay nil/false and no partial state leaks
//   - errors.As(err, &DuplicateTenantError{}) yields the offending
//     tenant ID + both file paths, so operators can grep / git-rm
// ─────────────────────────────────────────────────────────────────

func TestMixedMode_DuplicateAcrossModes_RejectedAtLoad(t *testing.T) {
	root := t.TempDir()
	writeTestYAML(t, filepath.Join(root, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)
	// Flat copy at root.
	writeTestYAML(t, filepath.Join(root, "shared.yaml"), `
tenants:
  shared-tenant:
    mysql_connections: "100"
`)
	// Nested copy under finance/ — simulates "forgot to delete
	// the flat copy after git mv to the new home".
	if err := os.MkdirAll(filepath.Join(root, "finance"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	writeTestYAML(t, filepath.Join(root, "finance", "shared.yaml"), `
tenants:
  shared-tenant:
    mysql_connections: "200"
`)

	// Capture log to confirm the WARN-only path is NOT taken (post-fix
	// the code returns error directly; WARN line should be absent).
	var logBuf bytes.Buffer
	origOutput := log.Writer()
	log.SetOutput(&logBuf)
	t.Cleanup(func() { log.SetOutput(origOutput) })

	mgr := NewConfigManager(root)
	err := mgr.Load()

	// v2.8.x contract: Load must return error.
	if err == nil {
		t.Fatal("expected Load to reject mixed-mode duplicate tenant, got nil")
	}

	// errors.As must yield the typed *DuplicateTenantError exposing
	// the offending tenant ID + both file paths.
	var dupErr *DuplicateTenantError
	if !errors.As(err, &dupErr) {
		t.Fatalf("expected error to wrap *DuplicateTenantError, got %T: %v", err, err)
	}
	if dupErr.TenantID != "shared-tenant" {
		t.Errorf("DuplicateTenantError.TenantID = %q, want %q", dupErr.TenantID, "shared-tenant")
	}
	// Both paths must be populated and distinct.
	if dupErr.PathA == "" || dupErr.PathB == "" {
		t.Errorf("DuplicateTenantError paths empty: A=%q B=%q", dupErr.PathA, dupErr.PathB)
	}
	if dupErr.PathA == dupErr.PathB {
		t.Errorf("DuplicateTenantError paths identical: %q", dupErr.PathA)
	}
	// Each path must end in shared.yaml so operators can grep / git rm.
	if !strings.HasSuffix(dupErr.PathA, "shared.yaml") {
		t.Errorf("PathA does not end in shared.yaml: %q", dupErr.PathA)
	}
	if !strings.HasSuffix(dupErr.PathB, "shared.yaml") {
		t.Errorf("PathB does not end in shared.yaml: %q", dupErr.PathB)
	}

	// State invariant: on hard reject, m.config / m.loaded stay at
	// pre-Load values. Caller observes "Load returned error" without
	// any partial state being committed.
	if mgr.loaded {
		t.Error("manager.loaded=true after rejected Load — partial state leak")
	}
	if mgr.config != nil {
		t.Error("manager.config != nil after rejected Load — partial state leak")
	}

	// Error message contract: includes "duplicate tenant ID" + tenant ID
	// (so operator log search works without unwrapping the typed error).
	if !strings.Contains(err.Error(), "duplicate tenant ID") {
		t.Errorf("error message should contain 'duplicate tenant ID': %v", err)
	}
	if !strings.Contains(err.Error(), "shared-tenant") {
		t.Errorf("error message should name offending tenant 'shared-tenant': %v", err)
	}

	// The pre-v2.8.x WARN line must NOT appear — the new path returns
	// hard error before reaching the log.Printf branch.
	logOutput := logBuf.String()
	if strings.Contains(logOutput, "WARN: hierarchical scan during Load failed") {
		t.Errorf("WARN-and-continue path leaked; should be hard error now. Log:\n%s", logOutput)
	}
}

// ─────────────────────────────────────────────────────────────────
// Test 2b — DuplicateAcrossModes_RejectedAtFullDirLoad (issue #127)
// fullDirLoad is the hot-reload path (called from IncrementalLoad
// when file-hash cache misses). Same v2.8.x contract as Load: hard
// reject on *DuplicateTenantError + don't trash prior known-good
// state.
//
// Sequence:
//  1. Cold start with a clean tree — Load succeeds, m.config holds
//     the clean state.
//  2. Hot-introduce a duplicate (write the same tenant ID under a
//     nested subdir) to simulate a customer git-pushing a bad
//     commit during ops hours.
//  3. fullDirLoad must return error wrapping *DuplicateTenantError.
//  4. Manager state must still hold the PRE-duplicate config (not
//     the half-merged new one) — running service stays serving.
// ─────────────────────────────────────────────────────────────────

func TestMixedMode_DuplicateAcrossModes_RejectedAtFullDirLoad(t *testing.T) {
	root := t.TempDir()
	writeTestYAML(t, filepath.Join(root, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)
	writeTestYAML(t, filepath.Join(root, "shared.yaml"), `
tenants:
  shared-tenant:
    mysql_connections: "100"
`)

	mgr := NewConfigManager(root)
	if err := mgr.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}
	priorConfig := mgr.config
	priorHash := mgr.lastHash
	if priorConfig == nil {
		t.Fatal("prior Load did not set m.config")
	}

	// Hot-introduce the duplicate — same tenant ID under a nested dir.
	if err := os.MkdirAll(filepath.Join(root, "finance"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	writeTestYAML(t, filepath.Join(root, "finance", "shared.yaml"), `
tenants:
  shared-tenant:
    mysql_connections: "200"
`)

	// fullDirLoad is the path IncrementalLoad uses when cache misses.
	err := mgr.fullDirLoad()
	if err == nil {
		t.Fatal("expected fullDirLoad to reject mixed-mode duplicate, got nil")
	}

	var dupErr *DuplicateTenantError
	if !errors.As(err, &dupErr) {
		t.Fatalf("expected error to wrap *DuplicateTenantError, got %T: %v", err, err)
	}
	if dupErr.TenantID != "shared-tenant" {
		t.Errorf("DuplicateTenantError.TenantID = %q, want %q", dupErr.TenantID, "shared-tenant")
	}

	// Critical invariant: prior known-good state must be preserved so
	// the running service keeps serving.
	if mgr.config != priorConfig {
		t.Error("manager.config swapped despite reload rejection — running service would have flipped to bad state")
	}
	if mgr.lastHash != priorHash {
		t.Errorf("manager.lastHash mutated despite reload rejection: pre=%q post=%q", priorHash, mgr.lastHash)
	}
}

// ─────────────────────────────────────────────────────────────────
// Test 3 — MidLevelDefaultsBlastRadiusScope
// When `<root>/finance/_defaults.yaml` changes, blast-radius must
// ONLY count nested tenants under finance/. Flat tenants at root
// must not appear in the affected count — their defaults chain
// doesn't include finance/.
//
// This is the "scope correctness" invariant from planning §B-5.
// Without it, an alert dashboard reading
// `da_config_blast_radius_tenants_affected{scope="domain"}` would
// over-count flat tenants every time a domain-level defaults change
// happened — false alarm bait.
// ─────────────────────────────────────────────────────────────────

func TestMixedMode_MidLevelDefaultsBlastRadiusScope(t *testing.T) {
	fresh, _ := withIsolatedMetrics(t)
	root := t.TempDir()
	writeMixedModeFixture(t, root)

	mgr := NewConfigManager(root)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	// Mutate a key in finance/_defaults.yaml that neither flat nor
	// nested tenants override — guaranteed effect=applied for the
	// dependent tenant (db-fin), while db-flat shouldn't even
	// register a defaults change (its chain doesn't include finance).
	writeTestYAML(t, filepath.Join(root, "finance", "_defaults.yaml"), `
defaults:
  region_alert_schedule: 17
`)

	if _, _, err := mgr.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload: %v", err)
	}

	// Domain-scoped applied: should equal exactly 1 (db-fin only).
	domainCount, domainSum := blastRadiusSample(t, fresh, ReloadReasonDefaults, "domain", "applied")
	if domainCount != 1 {
		t.Errorf("blast-radius{reason=defaults, scope=domain, effect=applied}: count=%d, want 1 (db-fin only)", domainCount)
	}
	if domainSum != 1 {
		t.Errorf("blast-radius{...domain/applied}: sum=%v, want 1 affected tenant", domainSum)
	}

	// Global-scoped applied: should NOT see this event — the
	// changed file is at depth=1 (domain), not root.
	globalCount, _ := blastRadiusSample(t, fresh, ReloadReasonDefaults, "global", "applied")
	if globalCount != 0 {
		t.Errorf("blast-radius{...global/applied}: count=%d, want 0 (event was domain-level, not root)", globalCount)
	}
}

// ─────────────────────────────────────────────────────────────────
// Test 4 — HotMigrationFlatToHierarchical
// Simulates the cutover for one tenant during the migration:
//
//	t=0 : conf.d/db-mig.yaml (flat-only, tenant present)
//	t=1 : mv conf.d/db-mig.yaml → conf.d/finance/db-mig.yaml
//	      + add conf.d/finance/_defaults.yaml
//	t=2 : reload — tenant must still be present, defaults chain
//	      now includes finance/_defaults.yaml, no duplicate error.
//
// This covers the realistic git-sync cadence where a `git mv`
// surfaces as one delete event + one add event in close succession.
// The transient state (file at neither location, then file at new
// location) must not crash the manager or lose the tenant.
// ─────────────────────────────────────────────────────────────────

func TestMixedMode_HotMigrationFlatToHierarchical(t *testing.T) {
	root := t.TempDir()
	writeTestYAML(t, filepath.Join(root, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)
	writeTestYAML(t, filepath.Join(root, "db-mig.yaml"), `
tenants:
  db-mig:
    mysql_connections: "100"
`)

	mgr := NewConfigManager(root)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load (pre-migration): %v", err)
	}

	mgr.mu.RLock()
	preChain := append([]string(nil), mgr.hierarchy.graph.TenantDefaults["db-mig"]...)
	preHash := mgr.hierarchy.mergedHashes["db-mig"]
	mgr.mu.RUnlock()
	if len(preChain) != 1 {
		t.Fatalf("pre-migration: expected 1-element chain (root only), got %d: %v", len(preChain), preChain)
	}
	if preHash == "" {
		t.Fatal("pre-migration: merged_hash empty")
	}

	// Cutover: move flat → nested + introduce mid-level defaults.
	if err := os.MkdirAll(filepath.Join(root, "finance"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.Rename(
		filepath.Join(root, "db-mig.yaml"),
		filepath.Join(root, "finance", "db-mig.yaml"),
	); err != nil {
		t.Fatalf("rename: %v", err)
	}
	writeTestYAML(t, filepath.Join(root, "finance", "_defaults.yaml"), `
defaults:
  region_alert_schedule: 9
`)

	if _, _, err := mgr.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload (post-migration): %v", err)
	}

	mgr.mu.RLock()
	postChain := mgr.hierarchy.graph.TenantDefaults["db-mig"]
	postHash := mgr.hierarchy.mergedHashes["db-mig"]
	mgr.mu.RUnlock()

	// Tenant still present.
	if postHash == "" {
		t.Fatal("post-migration: tenant lost (merged_hash empty)")
	}

	// Defaults chain extended.
	if len(postChain) != 2 {
		t.Errorf("post-migration: expected 2-element chain (root + finance), got %d: %v", len(postChain), postChain)
	}

	// merged_hash should have moved (new region_alert_schedule key
	// in the merged dict that wasn't there pre-migration).
	if postHash == preHash {
		t.Error("merged_hash unchanged after introducing finance/_defaults.yaml; cascading didn't apply")
	}
}

// ─────────────────────────────────────────────────────────────────
// Test 5 — StickyHierarchicalMode
// Once `hierarchicalMode` flips to true (via any `_defaults.yaml`
// being detected), it must stay true even after a mid-level
// `_defaults.yaml` is deleted. The manager must NOT revert to the
// flat scan path — `scanDirHierarchical` must continue to walk
// subdirectories so nested tenants stay discoverable.
//
// Why one-way: oscillating between scan strategies on every
// add/remove of a defaults file would (a) thrash the WatchLoop
// branch selection and (b) silently drop nested tenants if a sloppy
// `git rm conf.d/<dir>/_defaults.yaml` reverted the manager to
// flat scanning. The one-way switch is a guardrail against that
// footgun.
//
// Setup uses a flat root tenant alongside the nested one — that
// way `fullDirLoad` (which `diffAndReload` invokes at its tail to
// keep the flat-reader cache hot, per archive S#27 finding 1)
// still has at least one root-level YAML to discover and won't
// raise the "no .yaml files found" error. This matches the
// realistic mixed-mode steady state where SOME flat tenants linger
// while migration is in progress.
// ─────────────────────────────────────────────────────────────────

func TestMixedMode_StickyHierarchicalMode(t *testing.T) {
	root := t.TempDir()
	writeTestYAML(t, filepath.Join(root, "_defaults.yaml"), `
defaults:
  mysql_connections: 80
`)
	// A flat tenant at root — keeps fullDirLoad happy (it scans the
	// flat root only, errors if zero yaml files exist there).
	writeTestYAML(t, filepath.Join(root, "db-flat.yaml"), `
tenants:
  db-flat:
    mysql_connections: "111"
`)
	if err := os.MkdirAll(filepath.Join(root, "finance"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	writeTestYAML(t, filepath.Join(root, "finance", "_defaults.yaml"), `
defaults:
  region_alert_schedule: 9
`)
	writeTestYAML(t, filepath.Join(root, "finance", "db-fin.yaml"), `
tenants:
  db-fin:
    mysql_connections: "777"
`)

	mgr := NewConfigManager(root)
	if err := mgr.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	mgr.mu.RLock()
	hier := mgr.hierarchy.enabled
	mgr.mu.RUnlock()
	if !hier {
		t.Fatal("hierarchicalMode should flip to true on first Load (root _defaults.yaml present)")
	}

	// Delete the mid-level finance/_defaults.yaml. Root defaults
	// stays so fullDirLoad keeps working at the tail; nested
	// tenant file stays so we can verify it's still resolvable.
	if err := os.Remove(filepath.Join(root, "finance", "_defaults.yaml")); err != nil {
		t.Fatalf("remove finance defaults: %v", err)
	}

	if _, _, err := mgr.diffAndReload(); err != nil {
		t.Fatalf("diffAndReload (post-delete): %v", err)
	}

	mgr.mu.RLock()
	stillHier := mgr.hierarchy.enabled
	finStillThere := mgr.hierarchy.mergedHashes["db-fin"] != ""
	flatStillThere := mgr.hierarchy.mergedHashes["db-flat"] != ""
	finChain := mgr.hierarchy.graph.TenantDefaults["db-fin"]
	mgr.mu.RUnlock()

	if !stillHier {
		t.Error("hierarchicalMode flipped back to false after deleting mid-level defaults; expected sticky one-way switch")
	}
	if !finStillThere {
		t.Error("nested tenant db-fin lost after deleting finance defaults; flat scan can't reach it")
	}
	if !flatStillThere {
		t.Error("flat tenant db-flat lost; root scan should still see it")
	}
	// db-fin's chain should shrink to just root — the deleted
	// finance/_defaults.yaml is no longer in the chain.
	if len(finChain) != 1 {
		t.Errorf("db-fin chain after mid-level delete: expected 1 (root only), got %d: %v", len(finChain), finChain)
	}
}

