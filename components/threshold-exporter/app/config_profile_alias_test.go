package main

// config_profile_alias_test.go — pins for the partial-cache aliasing fix in
// reclaimTenantFrom (#1568 round 2, G1).
//
// The shape: a tenant-only reload patches tenant X through
// reclaimTenantFrom; if that hands back the parsed partial's OWN map,
// ApplyProfiles materialises the profile value INTO the cached partial.
// Every later rebuild that reuses the partial then merges the stale value
// back in ahead of the profile, so a `_profiles.yaml` edit never reaches X.
// Two consumers of the cached partial are pinned: the full-load path
// (commitFlatFrom reuses an unchanged partial) and the incremental
// full-rebuild path (mergePartialConfigs on a `_` file change).
//
// Seams: fresh manager per test, no package global touched; t.Parallel().

import (
	"os"
	"path/filepath"
	"testing"
)

// profileAliasTree writes a flat tree with one profiled tenant and one plain
// sibling, and returns the root. Tenant IDs are fixture-local.
func profileAliasTree(t *testing.T, profileValue string) string {
	t.Helper()
	dir := t.TempDir()
	writeProfile(t, dir, profileValue)
	writeTestFile(t, dir, "t-a.yaml", "tenants:\n  t-a:\n    _profile: gold\n    container_cpu: \"1\"\n")
	writeTestFile(t, dir, "t-b.yaml", "tenants:\n  t-b:\n    container_cpu: \"1\"\n")
	return dir
}

func writeProfile(t *testing.T, dir, value string) {
	t.Helper()
	writeTestFile(t, dir, "_profiles.yaml", "profiles:\n  gold:\n    mysql_connections: \""+value+"\"\n")
}

func profiledValue(t *testing.T, m *ConfigManager) string {
	t.Helper()
	cfg := m.GetConfig()
	if cfg == nil {
		t.Fatal("no config committed")
	}
	return cfg.Tenants["t-a"]["mysql_connections"].Default
}

// patchTenantA runs one tenant-only incremental reload that touches t-a's
// own file, which is the reload that routes t-a through reclaimTenantFrom.
func patchTenantA(t *testing.T, m *ConfigManager, dir, cpu string) {
	t.Helper()
	writeTestFile(t, dir, "t-a.yaml", "tenants:\n  t-a:\n    _profile: gold\n    container_cpu: \""+cpu+"\"\n")
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("tenant-only IncrementalLoad: %v", err)
	}
	if got := profiledValue(t, m); got != "95" {
		t.Fatalf("after the tenant-only patch t-a mysql_connections = %q, want the profile's 95", got)
	}
}

// TestProfileEditSurvivesAReusedPartial pins the full-load consumer: after
// a tenant-only patch of t-a, a `_profiles.yaml` edit that lands together
// with a nested key (→ fullDirLoadFrom on the same scan, t-a's partial
// reused because its hash did not move) must reach t-a; so must a later
// edit taken through Load().
func TestProfileEditSurvivesAReusedPartial(t *testing.T) {
	t.Parallel()
	dir := profileAliasTree(t, "95")
	m := NewConfigManagerWithDebounce(dir, 0)
	t.Cleanup(m.Close)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := profiledValue(t, m); got != "95" {
		t.Fatalf("cold load: t-a mysql_connections = %q, want 95", got)
	}

	patchTenantA(t, m, dir, "2")

	// Profile edit + a nested key in the same reload → the nested key sends
	// IncrementalLoad to fullDirLoadFrom, which reuses t-a's unchanged partial.
	writeProfile(t, dir, "97")
	if err := os.MkdirAll(filepath.Join(dir, "nested"), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	writeTestFile(t, filepath.Join(dir, "nested"), "t-c.yaml", "tenants:\n  t-c:\n    container_cpu: \"1\"\n")
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad with a nested key: %v", err)
	}
	if got := profiledValue(t, m); got != "97" {
		t.Errorf("full load reused a partial carrying the OLD profile value: t-a mysql_connections = %q, want 97", got)
	}

	writeProfile(t, dir, "99")
	if err := m.Load(); err != nil {
		t.Fatalf("Load after the profile edit: %v", err)
	}
	if got := profiledValue(t, m); got != "99" {
		t.Errorf("Load() reused a partial carrying the OLD profile value: t-a mysql_connections = %q, want 99", got)
	}
}

// TestProfileEditReachesAPatchedTenantOnIncrementalReload is the
// comparison measurement for the SAME aliasing on the pre-#1568 path: a
// pure `_profiles.yaml` edit is a `_` file change, so IncrementalLoad
// rebuilds via mergePartialConfigs from the cached partials — and a partial
// polluted by an earlier tenant-only patch carried the old value there too.
// Whether this was red before the copy landed is answered by the report's
// intentional-break run, not by this comment.
func TestProfileEditReachesAPatchedTenantOnIncrementalReload(t *testing.T) {
	t.Parallel()
	dir := profileAliasTree(t, "95")
	m := NewConfigManagerWithDebounce(dir, 0)
	t.Cleanup(m.Close)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	patchTenantA(t, m, dir, "2")

	writeProfile(t, dir, "97")
	if err := m.IncrementalLoad(); err != nil {
		t.Fatalf("IncrementalLoad after the profile edit: %v", err)
	}
	if got := profiledValue(t, m); got != "97" {
		t.Errorf("incremental full rebuild merged a partial carrying the OLD profile value: t-a mysql_connections = %q, want 97", got)
	}
}
