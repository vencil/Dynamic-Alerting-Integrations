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
	"fmt"
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
	graph := mgr.inheritanceGraph
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
	flatHash := mgr.mergedHashes["db-flat"]
	hierHash := mgr.mergedHashes["db-fin"]
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
// Test 2 — DuplicateAcrossModes_DetectedButNotPropagated
// Same tenant ID in both flat (`<root>/<id>.yaml`) and nested
// (`<root>/<dir>/<id>.yaml`) — track the CURRENT behavior so a
// future hardening doesn't regress silently:
//
//   * scanDirHierarchical DOES detect the duplicate (returns a
//     `duplicate tenant ID` error naming both paths). Locked by
//     TestScanDirHierarchical_DuplicateTenant in config_hierarchy_
//     test.go for the scan primitive.
//   * BUT `Load()` runs flat-mode `loadDir()` first (which silently
//     last-wins-merges the duplicate via `map[tid]=...`), THEN
//     calls `populateHierarchyState` whose error is "log-and-
//     ignore" per config.go L191-195 ("scan failure is logged-and-
//     ignored — the flat path is already live").
//
// Result: Load() returns nil, manager has the tenant from one of
// the duplicate files (last-wins on map iteration order), and the
// only signal of the duplicate is a WARN line — easy to miss in
// production.
//
// **This is a known production gap from B-5 plan**, documented
// in `docs/scenarios/flat-to-conf-d-cutover-decision.md` §
// "Known gaps". This test locks the current observable behavior
// (no error from Load + WARN log line emitted) so a future
// hardening PR — which should propagate the duplicate error to a
// hard error — can drop this test in favor of a stricter one.
// ─────────────────────────────────────────────────────────────────

func TestMixedMode_DuplicateAcrossModes_DetectedButNotPropagated(t *testing.T) {
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

	// Capture log to confirm the WARN signal exists.
	var logBuf bytes.Buffer
	origOutput := log.Writer()
	log.SetOutput(&logBuf)
	t.Cleanup(func() { log.SetOutput(origOutput) })

	mgr := NewConfigManager(root)
	err := mgr.Load()

	// Lock the **current** (gap) behavior. Flip to t.Fatal-on-nil-
	// err if/when the hardening lands.
	if err != nil {
		t.Errorf(
			"current behavior: Load returns nil despite duplicate; got error %v. "+
				"If you've shipped the hardening that propagates the "+
				"`duplicate tenant ID` error, this test needs updating to "+
				"assert the new contract.",
			err,
		)
	}

	// WARN signal must be present so ops can at least grep for it
	// even though Load() silently succeeds.
	logOutput := logBuf.String()
	if !strings.Contains(logOutput, "WARN: hierarchical scan during Load failed") {
		t.Errorf(
			"expected WARN log line for hierarchical-scan duplicate detection; got log:\n%s",
			logOutput,
		)
	}
	if !strings.Contains(logOutput, "duplicate tenant ID") {
		t.Errorf("expected 'duplicate tenant ID' phrase in WARN; got log:\n%s", logOutput)
	}
	if !strings.Contains(logOutput, "shared-tenant") {
		t.Errorf("WARN must name the offending tenant ID; got log:\n%s", logOutput)
	}
	// Both source paths should appear in the WARN so operator can grep / git rm.
	if strings.Count(logOutput, "shared.yaml") < 2 {
		t.Errorf("WARN must name BOTH colliding paths; got log:\n%s", logOutput)
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
	preChain := append([]string(nil), mgr.inheritanceGraph.TenantDefaults["db-mig"]...)
	preHash := mgr.mergedHashes["db-mig"]
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
	postChain := mgr.inheritanceGraph.TenantDefaults["db-mig"]
	postHash := mgr.mergedHashes["db-mig"]
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
	hier := mgr.hierarchicalMode
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
	stillHier := mgr.hierarchicalMode
	finStillThere := mgr.mergedHashes["db-fin"] != ""
	flatStillThere := mgr.mergedHashes["db-flat"] != ""
	finChain := mgr.inheritanceGraph.TenantDefaults["db-fin"]
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

// ─────────────────────────────────────────────────────────────────
// Helper: assert nested tenant survives a multi-step mutation.
// (Currently unused by the 5 cases above but kept for future
// add-on tests — e.g. triple-step migration paths. Suppress the
// `unused` complaint by referencing it in a no-op helper test.)
// ─────────────────────────────────────────────────────────────────

func nestedTenantHash(mgr *ConfigManager, id string) string {
	mgr.mu.RLock()
	defer mgr.mu.RUnlock()
	return mgr.mergedHashes[id]
}

// Acknowledge nestedTenantHash to keep `go vet` quiet under
// future test harness expansion.
var _ = func() string { return fmt.Sprintf("%T", nestedTenantHash) }
