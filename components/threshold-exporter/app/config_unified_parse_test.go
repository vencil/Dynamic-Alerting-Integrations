package main

// config_unified_parse_test.go — the exporter-side pins of #1957: /metrics and
// /effective hold the SAME tenant set, because both are judged by one decode
// (config.ParseConfigFile; the walker's verdict, handed to the flat plane
// through TreeScan.Partials).
//
// The per-file differential against a plain yaml.Unmarshal lives in
// pkg/config/config_file_test.go. This file asks the question one level up,
// on the manager's published state, on every path that publishes it: a cold
// Load, the hierarchical hot reload (detect → debounce → diffAndReload) and
// the flat incremental reload.
//
// Injection discipline (test-map.md): metrics via SetMetrics + freshMetrics,
// logger via SetLogger. No global swaps.

import (
	"path/filepath"
	"reflect"
	"sort"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// unifiedParseVariants are tenant-file bodies for tenant "t-x". The ones the
// full decode rejects are exactly the ones the walker's pre-#1957 decode
// accepted (see pkg/config lighterDecodeWouldAccept) plus a syntax error,
// which both decodes always rejected.
var unifiedParseVariants = map[string]string{
	"valid":                   "tenants:\n  t-x:\n    mysql_connections: \"70\"\n",
	"tenant body is a scalar": "tenants:\n  t-x: \"70\"\n",
	"defaults block of the wrong shape": "defaults:\n  mysql_connections:\n    nested: map\n" +
		"tenants:\n  t-x:\n    mysql_connections: \"66\"\n",
	"max_metrics_per_tenant non-int": "max_metrics_per_tenant: lots\ntenants:\n  t-x: {}\n",
	"profiles is a list":             "profiles:\n  - gold\ntenants:\n  t-x: {}\n",
	"syntax error":                   "tenants:\n  t-x: {}\n  \tbroken: [\n",
}

// tenantSets returns the two planes' populations: the merged config's
// tenants (/metrics) and the hierarchy's tenantSources (/effective).
func tenantSets(m *ConfigManager) (metrics, effective []string) {
	metrics = keysOfTenants(m.GetConfig())
	m.mu.RLock()
	for tid := range m.hierarchy.tenantSources {
		effective = append(effective, tid)
	}
	m.mu.RUnlock()
	sort.Strings(metrics)
	sort.Strings(effective)
	return metrics, effective
}

// assertOneTenantSet fails when the two planes disagree, and checks the
// subject tenant against the one decode's own verdict on the variant.
func assertOneTenantSet(t *testing.T, m *ConfigManager, body string, wantServed bool) {
	t.Helper()
	metrics, effective := tenantSets(m)
	if !reflect.DeepEqual(metrics, effective) {
		t.Errorf("the planes disagree about the tenant set:\n  /metrics   %v\n  /effective %v", metrics, effective)
	}
	_, served := m.GetConfig().Tenants["t-x"]
	if served != wantServed {
		_, perr := config.ParseConfigFile([]byte(body))
		t.Errorf("t-x served = %v, want %v (the one decode's verdict on the file: err=%v)", served, wantServed, perr)
	}
	if _, ok := m.Resolve("t-x"); ok != served {
		t.Errorf("Resolve(t-x) ok = %v while /metrics serves it = %v", ok, served)
	}
}

func TestOneTenantSet_ColdLoad(t *testing.T) {
	t.Parallel()
	for name, body := range unifiedParseVariants {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 50\n")
			writeTestYAML(t, filepath.Join(dir, "ok.yaml"), "tenants:\n  t-ok: {}\n")
			writeTestYAML(t, filepath.Join(dir, "x.yaml"), body)

			m, _, _ := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			_, perr := config.ParseConfigFile([]byte(body))
			assertOneTenantSet(t, m, body, perr == nil)
		})
	}
}

// TestOneTenantSet_HierarchicalHotReload drives the production reload path
// (tickOnce → diffAndReload → commitFlatFrom), which rebuilds the merged
// config in full, so a rejected file's tenant leaves BOTH planes.
func TestOneTenantSet_HierarchicalHotReload(t *testing.T) {
	t.Parallel()
	for name, body := range unifiedParseVariants {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 50\n")
			writeTestYAML(t, filepath.Join(dir, "ok.yaml"), "tenants:\n  t-ok: {}\n")
			writeTestYAML(t, filepath.Join(dir, "x.yaml"), unifiedParseVariants["valid"]+"    mysql_slow_queries: \"1\"\n")

			m, _, _ := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			writeTestYAML(t, filepath.Join(dir, "x.yaml"), body)
			m.tickOnce()
			_, perr := config.ParseConfigFile([]byte(body))
			assertOneTenantSet(t, m, body, perr == nil)
		})
	}
}

// TestOneTenantSet_FlatIncrementalReload drives IncrementalLoad on a tree
// with no `_defaults.yaml`. A tenant-only change takes patchTenants, which
// keeps a now-rejected file's last good values (a deliberate fail-safe of
// that path), so the subject stays served — and must then stay resolvable.
func TestOneTenantSet_FlatIncrementalReload(t *testing.T) {
	t.Parallel()
	for name, body := range unifiedParseVariants {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeTestYAML(t, filepath.Join(dir, "ok.yaml"), "tenants:\n  t-ok: {}\n")
			writeTestYAML(t, filepath.Join(dir, "x.yaml"), unifiedParseVariants["valid"]+"    mysql_slow_queries: \"1\"\n")

			m, _, _ := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			writeTestYAML(t, filepath.Join(dir, "x.yaml"), body)
			if err := m.IncrementalLoad(); err != nil {
				t.Fatalf("IncrementalLoad: %v", err)
			}
			assertOneTenantSet(t, m, body, true)
		})
	}
}
