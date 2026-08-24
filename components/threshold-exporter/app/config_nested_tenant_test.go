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

// TestASubtreeDefaultNeverLeaksIntoTheGlobalOnes guards what the acceptance
// test above cannot see. A design panel caught the gap: a subdirectory
// `_defaults.yaml` overwriting the root one for EVERY tenant in the tree.
//
// ⛔ Why the test above is blind to it: the flat merge is last-writer-wins
// over sorted keys (`flat_scanner.go`: `sort.Strings(names)` then
// `merged.Defaults[k] = v`), and with relative-path keys `_defaults.yaml`
// (`_` = 0x5F) sorts BEFORE `nested/_defaults.yaml` (`n` = 0x6E) — the subtree
// value lands last and wins globally. Both tenants in the fixture above carry
// their own override, so a poisoned default never shows through either
// assertion. Green test, broken semantics.
//
// ⚠️ THE TRIGGER IS TWO CHANGES, NOT ONE, and this was measured rather than
// argued — an earlier wording here blamed recursion alone, which is false:
//
//	recursion only                  → this test stays GREEN. `applyBoundaryRules`
//	                                  keys the underscore convention off the WHOLE
//	                                  name, so `nested/_defaults.yaml` reads as a
//	                                  tenant file and its `defaults:` is stripped
//	                                  (loudly — one WARN per nested file).
//	recursion + `path.Base` in that  → this test goes RED (`the global default is
//	convention                        60, not the root's 50`), while the
//	                                  acceptance test above goes green.
//
// The basename fix is not optional — `isTenantOnlyChange` misroutes a nested
// `_defaults.yaml` into the tenant-patch fast path without it — so any design
// that recurses arrives at the red state and has to answer for it.
//
// The property is design-independent: whatever reconciles the two scanners, a
// subtree's defaults must not silently re-price tenants in sibling subtrees.
func TestASubtreeDefaultNeverLeaksIntoTheGlobalOnes(t *testing.T) {
	const flatTenant, nestedTenant = "tenant-flat", "tenant-nested"
	const rootDefault, subtreeDefault = 50.0, 60.0

	dir := t.TempDir()
	writeSplitPopulationFixture(t, dir, flatTenant, nestedTenant)

	m := NewConfigManager(dir)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	cfg := m.GetConfig()
	if cfg == nil {
		t.Fatalf("GetConfig() is nil")
	}
	got, ok := cfg.Defaults["mysql_connections"]
	if !ok {
		t.Fatalf("premise: the root `_defaults.yaml` must reach the flat "+
			"Defaults map, or this test cannot tell leakage from absence; "+
			"got %v", cfg.Defaults)
	}
	if got != rootDefault {
		t.Errorf("the global default is %v, not the root's %v. A subtree's "+
			"`_defaults.yaml` (this fixture declares %v one level down) has "+
			"been merged into the single global Defaults map, which re-prices "+
			"every tenant in the tree that does not carry its own override — "+
			"including tenants in unrelated subtrees. `ThresholdConfig."+
			"Defaults` has no subtree scope, so a nested defaults file cannot "+
			"be admitted here as-is.",
			got, rootDefault, subtreeDefault)
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

// seriesFor returns the emitted `user_threshold` value for one tenant+metric,
// through a real collector on a real registry. The acceptance test above
// asserts a tenant is PRESENT; this is how the tests below assert it is
// present with the RIGHT NUMBER — the half that stays broken if only the
// scanner is fixed.
func seriesFor(t *testing.T, m *ConfigManager, tenant, metric string) (float64, bool) {
	t.Helper()
	reg := prometheus.NewRegistry()
	if err := reg.Register(NewThresholdCollector(m)); err != nil {
		t.Fatalf("register collector: %v", err)
	}
	families, err := reg.Gather()
	if err != nil {
		t.Fatalf("gather: %v", err)
	}
	for _, fam := range families {
		if fam.GetName() != "user_threshold" {
			continue
		}
		for _, mm := range fam.Metric {
			labels := map[string]string{}
			for _, pair := range mm.Label {
				labels[pair.GetName()] = pair.GetValue()
			}
			if labels["tenant"] == tenant && labels["metric"] == metric {
				return mm.Gauge.GetValue(), true
			}
		}
	}
	return 0, false
}

// writeInheritanceFixture is the value-plane fixture: one subtree with its own
// `_defaults.yaml` and three tenants in it that differ ONLY in what they say
// about the inherited key.
func writeInheritanceFixture(t *testing.T, dir string) {
	t.Helper()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 50\n")
	writeTestYAML(t, filepath.Join(dir, "flat.yaml"),
		"tenants:\n  tenant-flat:\n    mysql_connections: \"55\"\n")

	sub := filepath.Join(dir, nestedSubdir)
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", nestedSubdir, err)
	}
	writeTestYAML(t, filepath.Join(sub, "_defaults.yaml"), "defaults:\n  mysql_connections: 60\n")
	writeTestYAML(t, filepath.Join(sub, "inheritor.yaml"), "tenants:\n  tenant-inheritor: {}\n")
	writeTestYAML(t, filepath.Join(sub, "own.yaml"),
		"tenants:\n  tenant-own:\n    mysql_connections: \"77\"\n")
	writeTestYAML(t, filepath.Join(sub, "off.yaml"),
		"tenants:\n  tenant-off:\n    mysql_connections: \"disable\"\n")
}

// TestTheEmittedValueMatchesTheResolvedOne closes the half that survives a
// scanner-only fix.
//
// ⛔ Measured before this was wired: with the nested tenant merely PRESENT,
// `/effective` reported the subtree's 60 and the series carried the root's 50.
// That is worse than the absence it replaced in one specific way — the tenant
// now exists on both planes, so the #1526 divergence audit sees a reconciled
// tree and says nothing. Silence plus a wrong number, on the exact question
// #1447 sends operators to `diagnose --show-inheritance` to answer.
func TestTheEmittedValueMatchesTheResolvedOne(t *testing.T) {
	dir := t.TempDir()
	writeInheritanceFixture(t, dir)

	m := NewConfigManager(dir)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	// Control first: a root-level tenant is unaffected by any of this.
	if got, ok := seriesFor(t, m, "tenant-flat", "connections"); !ok || got != 55 {
		t.Fatalf("control: tenant-flat should emit its own 55, got %v (present=%v)", got, ok)
	}
	// And the global defaults map is untouched — a subtree file must never
	// re-price the tree (see the leakage guard above).
	if got := m.GetConfig().Defaults["mysql_connections"]; got != 50 {
		t.Fatalf("control: global default moved to %v; the subtree's defaults "+
			"leaked into the one global map", got)
	}

	eff, ok := m.Resolve("tenant-inheritor")
	if !ok {
		t.Fatalf("premise: Resolve(tenant-inheritor) must succeed")
	}
	if got := eff.Config["mysql_connections"]; got != 60 {
		t.Fatalf("premise: /effective should report the subtree default 60, got %v", got)
	}

	got, ok := seriesFor(t, m, "tenant-inheritor", "connections")
	if !ok {
		t.Fatalf("#1521: no series at all for the inheriting tenant")
	}
	if got != 60 {
		t.Errorf("#1521: the series says %v while /effective says 60. The alert "+
			"fires at a threshold the operator was never shown, and the "+
			"divergence audit stays quiet because the tenant IS present on "+
			"both planes.", got)
	}
}

// TestATenantsOwnValueBeatsWhatItInherits and
// TestADisabledKeyIsNotRevivedByInheritance are the two ways the overlay above
// could be wrong in the dangerous direction — by overwriting something the
// tenant said. Split into two tests so a failure names which one.
func TestATenantsOwnValueBeatsWhatItInherits(t *testing.T) {
	dir := t.TempDir()
	writeInheritanceFixture(t, dir)

	m := NewConfigManager(dir)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	got, ok := seriesFor(t, m, "tenant-own", "connections")
	if !ok {
		t.Fatalf("tenant-own emitted no series")
	}
	if got != 77 {
		t.Errorf("tenant-own declared 77 and inherits 60; the series says %v. "+
			"Inherited values must fill gaps, never overwrite what a tenant "+
			"authored.", got)
	}
}

func TestADisabledKeyIsNotRevivedByInheritance(t *testing.T) {
	dir := t.TempDir()
	writeInheritanceFixture(t, dir)

	m := NewConfigManager(dir)
	defer m.Close()
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got, ok := seriesFor(t, m, "tenant-off", "connections"); ok {
		t.Errorf("tenant-off set mysql_connections to \"disable\" and inherits 60; "+
			"a series appeared anyway at %v. Silently re-enabling a threshold a "+
			"tenant switched off is the worst failure this overlay can have — it "+
			"pages someone who opted out.", got)
	}
}
