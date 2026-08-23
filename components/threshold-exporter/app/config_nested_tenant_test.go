package main

// The acceptance criterion for #1521, written BEFORE the fix and independent
// of which fix is chosen.
//
// ⛔ THIS FILE IS RED ON PURPOSE until #1521 lands. It is the first commit of
// that branch, not a shippable state: a tenant declared in a conf.d
// SUBDIRECTORY resolves through `Resolve()` / `/effective` but never reaches
// `GetConfig()`, so `ThresholdCollector` emits no `user_threshold` for it and
// its alerts never fire. #1526 shipped observability only — it makes the
// divergence loud, it does not close it.
//
// ⛔ WHY IT IS WRITTEN THIS WAY. The existing tests miss this defect for two
// reasons this file deliberately does not repeat:
//
//   - `TestScanDirHierarchical_MixedMode` calls `scanDirHierarchical` DIRECTLY.
//     It never runs `Load()` and never looks at `GetConfig()`, so the split
//     between the two populations is invisible to it.
//   - the golden parity suite asserts only on the `Resolve()` side
//     (`source_file` / `defaults_chain` / `merged_hash` / `effective_config`).
//
// So everything below goes through the REAL entry — `NewConfigManager` +
// `Load()` — and asserts on the OUTPUT PLANE (`GetConfig()`, and a real
// `ThresholdCollector` registered on a real registry).
//
// ⚠️ Not end-to-end HTTP. The collector is gathered through
// `prometheus.NewRegistry().Gather()`, which is what `promhttp` does behind
// the handler, but no process is started and no `/metrics` request is made.
// Stated because #1521's own honesty section states it.

import (
	"os"
	"path/filepath"
	"sort"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

// nestedSubdir is the subdirectory the nested tenant lives in. Named for its
// ROLE, not for anything tenant-shaped — dev-rule #2 bans tenant ids in
// fixtures, and a directory called `db` reads like one.
const nestedSubdir = "nested"

// writeSplitPopulationFixture builds the smallest tree that puts one tenant in
// each population: `flatTenant` at the root (both scanners see it) and
// `nestedTenant` one level down (only the hierarchical scanner sees it).
//
// The `_defaults.yaml` at BOTH levels is not decoration. #1521's "成立條件"
// section measured that hierarchical mode only turns on when the tree contains
// at least one `_defaults.yaml`; without it the flat control disappears too
// and the observation stops being about nesting at all.
func writeSplitPopulationFixture(t *testing.T, dir, flatTenant, nestedTenant string) {
	t.Helper()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), `
defaults:
  mysql_connections: 50
`)
	writeTestYAML(t, filepath.Join(dir, "flat.yaml"),
		"tenants:\n  "+flatTenant+":\n    mysql_connections: \"55\"\n")

	sub := filepath.Join(dir, nestedSubdir)
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", nestedSubdir, err)
	}
	writeTestYAML(t, filepath.Join(sub, "_defaults.yaml"), `
defaults:
  mysql_connections: 60
`)
	writeTestYAML(t, filepath.Join(sub, "nested.yaml"),
		"tenants:\n  "+nestedTenant+":\n    mysql_connections: \"88\"\n")
}

// tenantsInUserThreshold registers a REAL ThresholdCollector against this
// manager and returns the sorted set of tenant label values that actually
// appear in `user_threshold`. This is the output plane: whatever is missing
// here is missing from the scrape, and therefore from every alert rule.
func tenantsInUserThreshold(t *testing.T, m *ConfigManager) []string {
	t.Helper()
	reg := prometheus.NewRegistry()
	if err := reg.Register(NewThresholdCollector(m)); err != nil {
		t.Fatalf("register collector: %v", err)
	}
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	seen := map[string]bool{}
	for _, fam := range families {
		if fam.GetName() != "user_threshold" {
			continue
		}
		for _, metric := range fam.Metric {
			for _, p := range metric.Label {
				if p.GetName() == "tenant" {
					seen[p.GetValue()] = true
				}
			}
		}
	}
	out := make([]string, 0, len(seen))
	for k := range seen {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func keysOfTenants(cfg *ThresholdConfig) []string {
	if cfg == nil {
		return nil
	}
	out := make([]string, 0, len(cfg.Tenants))
	for k := range cfg.Tenants {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

func contains(hay []string, needle string) bool {
	for _, s := range hay {
		if s == needle {
			return true
		}
	}
	return false
}

// TestNestedTenantReachesTheOutputPlane is #1521's acceptance criterion.
//
// It says nothing about HOW the two populations are reconciled — making
// `scanDirFileHashes` recurse, or pointing the collector at the hierarchical
// population, both satisfy it. That is deliberate: the criterion has to
// outlive the design choice.
func TestNestedTenantReachesTheOutputPlane(t *testing.T) {
	const flatTenant, nestedTenant = "tenant-flat", "tenant-nested"

	dir := t.TempDir()
	writeSplitPopulationFixture(t, dir, flatTenant, nestedTenant)

	m := NewConfigManager(dir)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	// (1) Control — the flat tenant must be present on every plane. If this
	//     fails the fixture is broken and nothing below means anything.
	if _, ok := m.Resolve(flatTenant); !ok {
		t.Fatalf("control: Resolve(%s) not found — fixture is broken", flatTenant)
	}
	if got := keysOfTenants(m.GetConfig()); !contains(got, flatTenant) {
		t.Fatalf("control: GetConfig().Tenants missing %s — fixture is broken, got %v",
			flatTenant, got)
	}
	if got := tenantsInUserThreshold(t, m); !contains(got, flatTenant) {
		t.Fatalf("control: user_threshold missing %s — fixture is broken, got %v",
			flatTenant, got)
	}

	// (2) The diagnostic plane already answers correctly for the nested
	//     tenant. Asserted, not assumed: it is the half that makes the defect
	//     invisible — an operator sent to `/effective` (or to
	//     `diagnose --show-inheritance`, which #1447 calls the ONLY way out of
	//     "config looks right but the alert never fires") is told everything
	//     is fine.
	if _, ok := m.Resolve(nestedTenant); !ok {
		t.Fatalf("premise: Resolve(%s) must succeed — without it this test is "+
			"about a tenant that does not exist, not about a split population",
			nestedTenant)
	}

	// (3) The defect. The nested tenant must reach the output plane too.
	if got := keysOfTenants(m.GetConfig()); !contains(got, nestedTenant) {
		t.Errorf("#1521: GetConfig().Tenants is missing %s, which Resolve() "+
			"just found. The flat scanner skips subdirectories, so the tenant "+
			"exists on the diagnostic plane and nowhere else.\n  got: %v",
			nestedTenant, got)
	}
	if got := tenantsInUserThreshold(t, m); !contains(got, nestedTenant) {
		t.Errorf("#1521: no user_threshold series for %s. This is the "+
			"customer-visible end of the defect: the tenant declared "+
			"thresholds, the platform reports no error, and the alert can "+
			"never fire because the series it matches on does not exist.\n"+
			"  got: %v", nestedTenant, got)
	}
}

// TestTheSameTenantFlattenedIsFine is the counterfactual for the test above:
// identical content, identical assertions, only the DEPTH changes. Without it
// a red run above could be blamed on the fixture, the collector wiring, or the
// metric name rather than on nesting.
func TestTheSameTenantFlattenedIsFine(t *testing.T) {
	const flatTenant, movedTenant = "tenant-flat", "tenant-nested"

	dir := t.TempDir()
	writeSplitPopulationFixture(t, dir, flatTenant, movedTenant)

	// Move the nested tenant's file to the root — same bytes, same tenant,
	// one directory level up. The subdirectory `_defaults.yaml` stays where
	// it is so the tree is still in hierarchical mode.
	src := filepath.Join(dir, nestedSubdir, "nested.yaml")
	body, err := os.ReadFile(src)
	if err != nil {
		t.Fatalf("read %s: %v", src, err)
	}
	if err := os.Remove(src); err != nil {
		t.Fatalf("remove %s: %v", src, err)
	}
	writeTestYAML(t, filepath.Join(dir, "nested.yaml"), string(body))

	m := NewConfigManager(dir)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	for _, tenant := range []string{flatTenant, movedTenant} {
		if _, ok := m.Resolve(tenant); !ok {
			t.Errorf("Resolve(%s) not found", tenant)
		}
		if got := keysOfTenants(m.GetConfig()); !contains(got, tenant) {
			t.Errorf("GetConfig().Tenants missing %s, got %v", tenant, got)
		}
		if got := tenantsInUserThreshold(t, m); !contains(got, tenant) {
			t.Errorf("user_threshold missing %s, got %v", tenant, got)
		}
	}
}
