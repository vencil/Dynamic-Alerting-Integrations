package main

// #2028: `max_metrics_per_tenant` never took effect in directory mode — the
// mode the Helm chart runs — because mergePartialInto never copied it. It now
// does, from the ROOT `_defaults.yaml` only (owner decision on #2028: one
// global cap, not inherited per subtree). Every other place it can be written
// is ignored LOUDLY, and a tenant file is stripped for the security reason in
// applyBoundaryRules: the cap bounds what one tenant can make the exporter
// emit, so a tenant must not be able to raise it for itself.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// rootDefaultsWithCap has three default keys, so a cap of 2 truncates every
// tenant by exactly one metric — the end-to-end effect, not just the field.
const rootDefaultsWithCap = `defaults:
  cap_a: 1
  cap_b: 2
  cap_c: 3
max_metrics_per_tenant: 2
`

func loadCapFixture(t *testing.T, dir string) (*ConfigManager, *bytes.Buffer) {
	t.Helper()
	var logBuf bytes.Buffer
	m := NewConfigManager(dir)
	t.Cleanup(m.Close)
	m.SetLogger(log.New(&logBuf, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	return m, &logBuf
}

func TestRootDefaultsCapTakesEffectInDirectoryMode(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), rootDefaultsWithCap)
	writeFile(t, filepath.Join(dir, "tenant-a.yaml"), "tenants:\n  t-a:\n    cap_a: \"5\"\n")

	m, _ := loadCapFixture(t, dir)
	cfg := m.GetConfig()
	if cfg.MaxMetricsPerTenant != 2 {
		t.Fatalf("MaxMetricsPerTenant = %d, want 2 from the root _defaults.yaml — "+
			"directory mode is dropping the field again (#2028)", cfg.MaxMetricsPerTenant)
	}
	// End to end: the cap is what ResolveAt truncates to, not just a field.
	_, stats := cfg.ResolveAtWithStats(time.Now())
	if got := stats.PerTenantOverLimit["t-a"]; got != 1 {
		t.Errorf("PerTenantOverLimit[t-a] = %d, want 1 (3 metrics against a cap of 2)", got)
	}
}

func TestWithoutTheKeyTheBuiltInCapStillApplies(t *testing.T) {
	// Control for the test above: same tree minus the key → field stays 0,
	// i.e. DefaultMaxMetricsPerTenant, and nobody is over the limit.
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), strings.Replace(rootDefaultsWithCap, "max_metrics_per_tenant: 2\n", "", 1))
	writeFile(t, filepath.Join(dir, "tenant-a.yaml"), "tenants:\n  t-a:\n    cap_a: \"5\"\n")

	m, _ := loadCapFixture(t, dir)
	cfg := m.GetConfig()
	if cfg.MaxMetricsPerTenant != 0 {
		t.Fatalf("MaxMetricsPerTenant = %d, want 0 (unset → built-in)", cfg.MaxMetricsPerTenant)
	}
	if _, stats := cfg.ResolveAtWithStats(time.Now()); stats.PerTenantOverLimit["t-a"] != 0 {
		t.Errorf("t-a over limit without a configured cap: %v", stats.PerTenantOverLimit)
	}
}

func TestATenantFileCannotRaiseItsOwnCap(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), rootDefaultsWithCap)
	// Top-level key in a TENANT file: raise the ceiling for everyone, or
	// (negative) switch truncation off.
	writeFile(t, filepath.Join(dir, "tenant-a.yaml"), "max_metrics_per_tenant: -1\ntenants:\n  t-a:\n    cap_a: \"5\"\n")

	m, logs := loadCapFixture(t, dir)
	if got := m.GetConfig().MaxMetricsPerTenant; got != 2 {
		t.Errorf("MaxMetricsPerTenant = %d, want the root's 2 — a tenant file changed the platform cap", got)
	}
	if want := "WARN: max_metrics_per_tenant found in tenant-a.yaml — platform-scoped"; !strings.Contains(logs.String(), want) {
		t.Errorf("missing %q — the operator must learn the key is ignored.\nlogs:\n%s", want, logs.String())
	}
}

func TestANonCarrierPlatformFileCannotSetTheCap(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), rootDefaultsWithCap)
	// `_profiles.yaml` sorts AFTER `_defaults.yaml`; were it merged, it would win.
	writeFile(t, filepath.Join(dir, "_profiles.yaml"), "max_metrics_per_tenant: 9999\nprofiles:\n  gold:\n    cap_a: \"7\"\n")
	writeFile(t, filepath.Join(dir, "tenant-a.yaml"), "tenants:\n  t-a:\n    cap_a: \"5\"\n")

	m, logs := loadCapFixture(t, dir)
	if got := m.GetConfig().MaxMetricsPerTenant; got != 2 {
		t.Errorf("MaxMetricsPerTenant = %d, want the root carrier's 2", got)
	}
	if want := "WARN: max_metrics_per_tenant found in _profiles.yaml — not a defaults carrier"; !strings.Contains(logs.String(), want) {
		t.Errorf("missing %q.\nlogs:\n%s", want, logs.String())
	}
}

func TestANestedDefaultsCapIsIgnoredLoudly(t *testing.T) {
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), rootDefaultsWithCap)
	sub := filepath.Join(dir, "team-x")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	writeFile(t, filepath.Join(sub, "_defaults.yaml"), "defaults:\n  cap_a: 4\nmax_metrics_per_tenant: 9999\n")
	writeFile(t, filepath.Join(sub, "tenant-x.yaml"), "tenants:\n  t-x:\n    cap_b: \"6\"\n")

	m, logs := loadCapFixture(t, dir)
	if got := m.GetConfig().MaxMetricsPerTenant; got != 2 {
		t.Errorf("MaxMetricsPerTenant = %d, want the ROOT's 2 — a subtree carrier must not set the global cap", got)
	}
	if want := "WARN: max_metrics_per_tenant found in"; !strings.Contains(logs.String(), want) ||
		!strings.Contains(logs.String(), filepath.Join("team-x", "_defaults.yaml")) {
		t.Errorf("a nested cap must be reported, not dropped in silence.\nlogs:\n%s", logs.String())
	}
}

func TestTheCapSurvivesATenantOnlyIncrementalReload(t *testing.T) {
	// patchTenants copies platform-scoped fields from the previous config one
	// by one; a field it forgets is correct after Load and gone after the
	// first tenant edit — the full-vs-incremental drift its header warns about.
	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "_defaults.yaml"), rootDefaultsWithCap)
	tenant := filepath.Join(dir, "tenant-a.yaml")
	writeFile(t, tenant, "tenants:\n  t-a:\n    cap_a: \"5\"\n")

	m, _ := loadCapFixture(t, dir)
	if got := m.GetConfig().MaxMetricsPerTenant; got != 2 {
		t.Fatalf("premise: MaxMetricsPerTenant after Load = %d, want 2", got)
	}

	writeFile(t, tenant, "tenants:\n  t-a:\n    cap_a: \"6\"\n")
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad: %v", err)
	}
	cfg := m.GetConfig()
	if got := cfg.Tenants["t-a"]["cap_a"].Default; got != "6" {
		t.Fatalf("premise: the tenant edit did not land (cap_a = %q), so this run proves nothing", got)
	}
	if cfg.MaxMetricsPerTenant != 2 {
		t.Errorf("MaxMetricsPerTenant = %d after a tenant-only reload, want 2 — patchTenants dropped it", cfg.MaxMetricsPerTenant)
	}
}

func TestRemovingTheKeyRestoresTheBuiltInCap(t *testing.T) {
	dir := t.TempDir()
	defaults := filepath.Join(dir, "_defaults.yaml")
	writeFile(t, defaults, rootDefaultsWithCap)
	writeFile(t, filepath.Join(dir, "tenant-a.yaml"), "tenants:\n  t-a:\n    cap_a: \"5\"\n")

	m, _ := loadCapFixture(t, dir)
	writeFile(t, defaults, strings.Replace(rootDefaultsWithCap, "max_metrics_per_tenant: 2\n", "", 1))
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad: %v", err)
	}
	if got := m.GetConfig().MaxMetricsPerTenant; got != 0 {
		t.Errorf("MaxMetricsPerTenant = %d after removing the key, want 0 (built-in)", got)
	}
}
