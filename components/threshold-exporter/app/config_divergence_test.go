package main

// Tests for the conf.d dual-scanner divergence audit (#1521).
//
// The audit is observability-only: it must be LOUD when a tenant is
// visible to the hierarchical scanner but invisible to the collector, and
// completely SILENT when the same tenant content lives at the conf.d
// root. Both halves are asserted here — the silent (control) half is what
// makes the loud half mean something.
//
// Injection discipline (CLAUDE.md §測試注入 Seam): metrics via SetMetrics
// + freshMetrics, logger via SetLogger. No global swaps.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"
)

const divergenceTenantYAML = "tenants:\n  hier-tenant:\n    mysql_connections: \"95\"\n"

// newAuditedManager wires a ConfigManager with an isolated metrics
// instance and a capturing logger, both through the documented seams.
func newAuditedManager(t *testing.T, dir string) (*ConfigManager, *configMetrics, *bytes.Buffer) {
	t.Helper()
	fresh, _ := freshMetrics(t)
	var logBuf bytes.Buffer
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(&logBuf, "", 0))
	t.Cleanup(m.Close)
	return m, fresh, &logBuf
}

// writeNestedTenant drops a tenant file one directory below the conf.d
// root — the exact shape from #1521 (`conf.d/db/hier-tenant.yaml`).
func writeNestedTenant(t *testing.T, dir string) string {
	t.Helper()
	sub := filepath.Join(dir, "db")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatalf("mkdir db: %v", err)
	}
	path := filepath.Join(sub, "hier-tenant.yaml")
	writeTestYAML(t, path, divergenceTenantYAML)
	return path
}

// writeDivergenceRoot lays down the always-present root files: a
// _defaults.yaml (so hierarchical mode activates) plus one healthy
// root-level tenant that must never be reported as divergent.
func writeDivergenceRoot(t *testing.T, dir string) {
	t.Helper()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  root-tenant:\n    mysql_connections: \"90\"\n")
}

// TestDivergenceAudit_ColdStart_NestedTenantIsLoud drives the real entry
// point (NewConfigManager → Load) against a mixed flat+nested tree and
// asserts the tenant is named, the consequence is spelled out, and the
// gauge reads 1.
func TestDivergenceAudit_ColdStart_NestedTenantIsLoud(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	nestedPath := writeNestedTenant(t, dir)

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load must NOT fail on divergence (option C is not fail-closed): %v", err)
	}

	// The defect itself, pinned: /effective sees it, /metrics does not.
	if _, ok := m.Resolve("hier-tenant"); !ok {
		t.Fatalf("precondition: hierarchical scanner should resolve hier-tenant")
	}
	if _, ok := m.GetConfig().Tenants["hier-tenant"]; ok {
		t.Fatalf("precondition changed: hier-tenant now reaches the collector config — " +
			"the #1521 defect appears fixed, this audit test needs revisiting")
	}

	logs := logBuf.String()
	if !strings.Contains(logs, "ERROR: conf.d scanner divergence") {
		t.Errorf("expected divergence ERROR log, got:\n%s", logs)
	}
	if !strings.Contains(logs, "hier-tenant") {
		t.Errorf("ERROR log must name the affected tenant, got:\n%s", logs)
	}
	if !strings.Contains(logs, nestedPath) {
		t.Errorf("ERROR log must name the source file %s, got:\n%s", nestedPath, logs)
	}
	if !strings.Contains(logs, "user_threshold") {
		t.Errorf("ERROR log must state the consequence (no user_threshold series), got:\n%s", logs)
	}
	if !strings.Contains(logs, "1521") {
		t.Errorf("ERROR log must point at the tracking issue, got:\n%s", logs)
	}
	// The healthy root-level tenant must not be swept into the report.
	if strings.Contains(logs, "root-tenant (") {
		t.Errorf("root-level tenant must not be reported as divergent, got:\n%s", logs)
	}

	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Errorf("da_config_hierarchy_divergent_tenants = %v, want 1", got)
	}
}

// TestDivergenceAudit_FlatControl_IsSilent is the counterfactual: the very
// same tenant content at the conf.d root must produce no ERROR and a zero
// gauge. Without this, the test above only proves "it prints something".
func TestDivergenceAudit_FlatControl_IsSilent(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	writeTestYAML(t, filepath.Join(dir, "hier-tenant.yaml"), divergenceTenantYAML)

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}

	if _, ok := m.GetConfig().Tenants["hier-tenant"]; !ok {
		t.Fatalf("control precondition: root-level tenant must reach the collector config")
	}
	if logs := logBuf.String(); strings.Contains(logs, "conf.d scanner divergence") {
		t.Errorf("flat layout must NOT trigger the divergence ERROR, got:\n%s", logs)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("da_config_hierarchy_divergent_tenants = %v, want 0 for a flat layout", got)
	}
}

// TestDivergenceAudit_GaugeRecoversToZero proves the gauge is state-coded
// (the gauge-over-counter argument): remove the misplaced file, reload,
// and the gauge must fall back to 0.
func TestDivergenceAudit_GaugeRecoversToZero(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)
	nestedPath := writeNestedTenant(t, dir)

	m, fresh, _ := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Fatalf("setup: gauge = %v, want 1", got)
	}

	if err := os.Remove(nestedPath); err != nil {
		t.Fatalf("remove nested tenant: %v", err)
	}
	if err := m.fullDirLoad(); err != nil {
		t.Fatalf("reload after fix: %v", err)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("gauge did not recover after the layout was fixed: %v, want 0", got)
	}
}

// TestDivergenceAudit_HotReload_HierarchicalPath is the load-bearing one:
// #1521 explicitly left "is the hot-reload path also broken?" unverified.
// It drives the real detect→debounce→diffAndReload chain via tickOnce
// (debounce window 0 = synchronous, the seam config_debounce.go documents)
// and answers both halves:
//
//	(a) does a nested tenant added AFTER startup reach m.config?  → no
//	(b) does the audit fire on that path?                         → yes
func TestDivergenceAudit_HotReload_HierarchicalPath(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeDivergenceRoot(t, dir)

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Fatalf("baseline: gauge = %v, want 0 before the nested file exists", got)
	}
	logBuf.Reset()

	// Now introduce the nested tenant into a RUNNING exporter.
	nestedPath := writeNestedTenant(t, dir)
	// mtime-guard safety window is 2s for the flat scanner; the
	// hierarchical detect path re-hashes unconditionally, so tickOnce sees
	// the new file immediately.
	m.tickOnce()

	// (a) Is the hot-reload path itself broken the same way?
	if _, ok := m.GetConfig().Tenants["hier-tenant"]; ok {
		t.Errorf("FINDING CHANGED: hot-reload now admits the nested tenant into the collector config; " +
			"#1521 hot-reload divergence would be fixed and this test needs revisiting")
	}
	if _, ok := m.Resolve("hier-tenant"); !ok {
		t.Errorf("hot-reload should have made the nested tenant resolvable via /effective")
	}

	// (b) Does option C speak up on this path?
	logs := logBuf.String()
	if !strings.Contains(logs, "ERROR: conf.d scanner divergence") {
		t.Errorf("hot-reload path emitted no divergence ERROR, got:\n%s", logs)
	}
	if !strings.Contains(logs, nestedPath) {
		t.Errorf("hot-reload ERROR must name the source file %s, got:\n%s", nestedPath, logs)
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 1 {
		t.Errorf("da_config_hierarchy_divergent_tenants after hot reload = %v, want 1", got)
	}
}

// TestDivergenceAudit_HotReload_FlatModeNeverDetects documents a boundary
// the audit cannot cover: with no _defaults.yaml anywhere, hierarchical
// mode never activates, so detectChange uses the flat top-level composite
// hash — adding a file in a sub-directory triggers no reload at all, hence
// no commitConfig and no audit. The cold-start audit still catches it on
// the next restart (asserted at the end).
func TestDivergenceAudit_HotReload_FlatModeNeverDetects(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  root-tenant:\n    mysql_connections: \"90\"\n")

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("initial Load: %v", err)
	}
	writeNestedTenant(t, dir)
	m.tickOnce()

	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("flat-mode hot reload unexpectedly re-audited: gauge = %v", got)
	}
	if logs := logBuf.String(); strings.Contains(logs, "conf.d scanner divergence") {
		t.Errorf("flat-mode tick was not expected to reload at all, got:\n%s", logs)
	}

	// Second boundary, measured rather than reasoned: even when an
	// UNRELATED root-level edit does trigger a reload, the flat path taken
	// (diffAndReload → scanAndCheckHierarchical fallback → IncrementalLoad
	// → commitConfig) never refreshes m.hierarchy.tenantSources, so the
	// audit compares against a stale hierarchy snapshot and still reports
	// 0. The audit call site covers this path; the DATA it reads does not.
	writeTestYAML(t, filepath.Join(dir, "root-tenant.yaml"), "tenants:\n  root-tenant:\n    mysql_connections: \"91\"\n")
	logBuf.Reset()
	m.tickOnce()
	if _, ok := m.GetConfig().Tenants["root-tenant"]; !ok {
		t.Fatalf("setup: the root edit should have reloaded")
	}
	if got := testutil.ToFloat64(fresh.hierarchyDivergentTenants); got != 0 {
		t.Errorf("BOUNDARY CHANGED: flat IncrementalLoad path now reports divergence (gauge=%v); "+
			"the stale-tenantSources limitation documented here would be gone", got)
	}

	// A restart (cold start) does see it, because populateHierarchyState
	// runs regardless of whether hierarchical mode is enabled.
	m2, fresh2, logBuf2 := newAuditedManager(t, dir)
	if err := m2.Load(); err != nil {
		t.Fatalf("restart Load: %v", err)
	}
	if got := testutil.ToFloat64(fresh2.hierarchyDivergentTenants); got != 1 {
		t.Errorf("cold start after restart: gauge = %v, want 1", got)
	}
	if !strings.Contains(logBuf2.String(), "hier-tenant") {
		t.Errorf("cold start after restart must name the tenant, got:\n%s", logBuf2.String())
	}
}

// TestHierarchyDivergentTenants_OneDirectionOnly pins the pure helper's
// asymmetry: tenants present only in the flat config (single-file mode,
// `_`-prefixed root files) are NOT divergence, otherwise the gauge would
// never sit at 0.
func TestHierarchyDivergentTenants_OneDirectionOnly(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{Tenants: map[string]map[string]ScheduledValue{
		"flat-only": {},
	}}
	if got := hierarchyDivergentTenants(nil, cfg); got != nil {
		t.Errorf("empty tenantSources must yield no divergence, got %v", got)
	}
	sources := map[string]string{"flat-only": "/x/flat-only.yaml", "nested": "/x/db/nested.yaml"}
	got := hierarchyDivergentTenants(sources, cfg)
	if len(got) != 1 || got[0] != "nested" {
		t.Errorf("hierarchyDivergentTenants = %v, want [nested]", got)
	}
}

// TestFormatDivergenceLog_CapsTheSample keeps a misplaced 100-tenant
// sub-tree from producing an unreadable log record.
func TestFormatDivergenceLog_CapsTheSample(t *testing.T) {
	t.Parallel()
	sources := make(map[string]string)
	var divergent []string
	for i := 0; i < divergenceLogSampleLimit+5; i++ {
		id := "t" + string(rune('a'+i))
		sources[id] = "/conf.d/db/" + id + ".yaml"
		divergent = append(divergent, id)
	}
	line := formatDivergenceLog(divergent, sources, "/conf.d", "Config loaded (directory)")
	if !strings.Contains(line, "and 5 more") {
		t.Errorf("expected truncation suffix, got:\n%s", line)
	}
	if strings.Count(line, "/conf.d/db/") != divergenceLogSampleLimit {
		t.Errorf("expected exactly %d sampled paths, got:\n%s", divergenceLogSampleLimit, line)
	}
}
