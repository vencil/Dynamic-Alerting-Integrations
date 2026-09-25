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
	"strings"
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

// TestAParseFailureIsCountedOncePerScan pins da_config_parse_failure_total's
// unit (#1957): one increment per failed tenant file per scan, whichever
// plane consumes the scan. The walker judges the file and counts; the flat
// plane reads TreeFile.ParseFailed and does not count again.
//
// ⛔ BOTH ERROR KINDS, because they double-counted for different reasons.
// A syntax error was rejected by both decodes before #1957, so the walker
// and the flat plane each counted it (2 per cold load). A type error (a
// scalar tenant body) was accepted by the walker's lighter decode and
// counted by the flat plane alone — until the decode was unified, after
// which it too would have been counted by both.
//
// Each leg drives exactly ONE scan (a cold Load, one diffAndReload, one
// IncrementalLoad), so the expected delta is 1. tickOnce is not used: a tick
// is a detectChange scan plus a reload scan, i.e. two scans.
func TestAParseFailureIsCountedOncePerScan(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct{ name, body, rewrite string }{
		{"syntax error", "tenants:\n  t-x: [unclosed\n", "tenants:\n  t-x: [still unclosed\n"},
		{"type error", "tenants:\n  t-x: \"70\"\n", "tenants:\n  t-x: \"71\"\n"},
	} {
		t.Run(tc.name+"/hierarchical cold load then reload", func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 50\n")
			writeTestYAML(t, filepath.Join(dir, "ok.yaml"), "tenants:\n  t-ok: {}\n")
			writeTestYAML(t, filepath.Join(dir, "x.yaml"), tc.body)
			m, fresh, logBuf := newAuditedManager(t, dir)

			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			if got := parseFailureCount(fresh, "x.yaml"); got != 1 {
				t.Errorf("cold load counted x.yaml %v time(s), want 1", got)
			}
			if got := strings.Count(logBuf.String(), "skip unparseable file"); got != 1 {
				t.Errorf("cold load logged x.yaml %d time(s), want 1:\n%s", got, logBuf.String())
			}

			logBuf.Reset()
			if _, _, err := m.diffAndReload(); err != nil {
				t.Fatalf("diffAndReload: %v", err)
			}
			if got := parseFailureCount(fresh, "x.yaml"); got != 2 {
				t.Errorf("after one reload the count is %v, want 2 (one per scan)", got)
			}
			if got := strings.Count(logBuf.String(), "skip unparseable file"); got != 1 {
				t.Errorf("the reload logged x.yaml %d time(s), want 1:\n%s", got, logBuf.String())
			}
		})
		t.Run(tc.name+"/flat incremental reload of the broken file", func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeTestYAML(t, filepath.Join(dir, "ok.yaml"), "tenants:\n  t-ok: {}\n")
			writeTestYAML(t, filepath.Join(dir, "x.yaml"), tc.body)
			m, fresh, _ := newAuditedManager(t, dir)

			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			before := parseFailureCount(fresh, "x.yaml")
			// Still broken, different bytes: the incremental path takes it
			// as a changed file.
			writeTestYAML(t, filepath.Join(dir, "x.yaml"), tc.rewrite)
			if err := m.IncrementalLoad(); err != nil {
				t.Fatalf("IncrementalLoad: %v", err)
			}
			if got := parseFailureCount(fresh, "x.yaml") - before; got != 1 {
				t.Errorf("one incremental reload counted x.yaml %v time(s), want 1", got)
			}
		})
	}
}
