package main

// #2452: a watch-path scan that fails (a tenant id declared in two files, or
// a config directory that cannot be walked) applies nothing — the exporter
// keeps the last good config and /ready stays 200 — and before this counter
// it left no series at all: da_config_reload_trigger_total only moves on a
// reload that ran. These tests pin da_config_scan_failures_total{reason} and
// that the reload counter stays out of it.
//
// Seams: every test injects its own *configMetrics (freshMetrics +
// SetMetrics) and a discarding logger, so they run in parallel without
// touching the package-level singleton (test-map.md §測試注入 Seam).

import (
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/jonboulle/clockwork"
	"github.com/prometheus/client_golang/prometheus/testutil"
)

// newScanFailureManager loads the hierarchical fixture (root _defaults.yaml +
// team-a/tenant-a.yaml) through a synchronous-debounce manager wired to a
// fresh metrics instance.
func newScanFailureManager(t *testing.T) (*ConfigManager, *configMetrics, string) {
	t.Helper()
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")
	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(dir, 0)
	t.Cleanup(m.Close)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	return m, fresh, dir
}

func scanFailures(cm *configMetrics, reason string) float64 {
	return testutil.ToFloat64(cm.scanFailures.WithLabelValues(reason))
}

// reloadTriggerSeries is the number of da_config_reload_trigger_total series
// that exist — 0 means no reload of any reason was ever counted.
func reloadTriggerSeries(cm *configMetrics) int {
	return testutil.CollectAndCount(cm.reloadTriggers)
}

func tenantAThreshold(m *ConfigManager) string {
	cfg := m.GetConfig()
	if cfg == nil {
		return "<nil config>"
	}
	return fmt.Sprint(cfg.Tenants["tenant-a"]["mysql_connections"])
}

// The ticket's shape: a second file declaring an existing tenant appears
// under a running exporter. Every tick fails its scan; each one must count
// once under duplicate_tenant, and none may reach reload_trigger_total.
func TestTickOnce_DuplicateTenant_CountsScanFailureNotReloadTrigger(t *testing.T) {
	t.Parallel()
	m, fresh, dir := newScanFailureManager(t)
	// Sentinel for the last-scan gauge (see the flat-mode test below).
	const sentinel = 1
	fresh.lastScanComplete.Set(sentinel)

	writeTestYAML(t, filepath.Join(dir, "tenant-a-copy.yaml"),
		"tenants:\n  tenant-a:\n    mysql_connections: \"91\"\n")
	// An edit made while the duplicate is present is not applied (the
	// frozen tree the ticket describes) — pinned here so the counter is
	// shown counting exactly that state.
	writeTestYAML(t, filepath.Join(dir, "team-a", "tenant-a.yaml"),
		"tenants:\n  tenant-a:\n    mysql_connections: \"77\"\n")

	const ticks = 3
	for i := 0; i < ticks; i++ {
		m.tickOnce()
	}

	if got := scanFailures(fresh, ScanFailureReasonDuplicateTenant); got != ticks {
		t.Errorf("scan_failures{duplicate_tenant} = %v after %d failing ticks, want %d", got, ticks, ticks)
	}
	if got := scanFailures(fresh, ScanFailureReasonWalkError); got != 0 {
		t.Errorf("scan_failures{walk_error} = %v, want 0 (the walk itself succeeded)", got)
	}
	if n := reloadTriggerSeries(fresh); n != 0 {
		t.Errorf("da_config_reload_trigger_total has %d series after scan failures, want 0 (nothing was reloaded)", n)
	}
	if got := tenantAThreshold(m); got != "90" {
		t.Errorf("tenant-a mysql_connections = %s while the duplicate is present, want the last good 90", got)
	}
	if got := testutil.ToFloat64(fresh.lastScanComplete); got != sentinel {
		t.Errorf("last_scan_complete = %v while the duplicate is present, want it untouched (%d)", got, sentinel)
	}

	// Removing the second file unfreezes the tree: the next tick reloads
	// (the pending edit lands) and the failure count stops rising.
	if err := os.Remove(filepath.Join(dir, "tenant-a-copy.yaml")); err != nil {
		t.Fatal(err)
	}
	m.tickOnce()
	if got := scanFailures(fresh, ScanFailureReasonDuplicateTenant); got != ticks {
		t.Errorf("scan_failures{duplicate_tenant} = %v after the duplicate was removed, want it to stay %d", got, ticks)
	}
	if got := testutil.ToFloat64(fresh.reloadTriggers.WithLabelValues(ReloadReasonSource)); got != 1 {
		t.Errorf("reload_trigger_total{source} = %v after recovery, want 1", got)
	}
	if got := tenantAThreshold(m); got != "77" {
		t.Errorf("tenant-a mysql_connections = %s after recovery, want 77", got)
	}
	if got := testutil.ToFloat64(fresh.lastScanComplete); got == sentinel {
		t.Error("last_scan_complete not stamped by the first clean tick after recovery")
	}
}

// Flat mode (no _defaults.yaml anywhere) freezes on a duplicate too, and
// its detectChange fails on the Conflict just like hierarchical mode's, so
// each tick takes tickOnce's WARN + IncScanFailure path: one count per tick
// under duplicate_tenant, and no reload scheduled — reload_trigger_total
// never moves.
//
// Also pins the premise ConfigScanFailing rests on: the last-scan gauge is
// NOT stamped while the duplicate is present (it is set to a sentinel after
// Load, and must still read it), and the first clean tick stamps it again.
func TestTickOnce_FlatMode_DuplicateTenant_CountsScanFailure(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "t-a.yaml"), "tenants:\n  t-a:\n    mysql_connections: \"11\"\n")
	writeTestYAML(t, filepath.Join(dir, "t-b.yaml"), "tenants:\n  t-b:\n    mysql_connections: \"21\"\n")
	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(dir, 0)
	t.Cleanup(m.Close)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	m.mu.RLock()
	hier := m.hierarchy.enabled
	m.mu.RUnlock()
	if hier {
		t.Fatal("fixture must stay in flat mode (no _defaults.yaml)")
	}
	const sentinel = 1
	fresh.lastScanComplete.Set(sentinel)

	before := m.GetConfig()
	writeTestYAML(t, filepath.Join(dir, "t-a-copy.yaml"), "tenants:\n  t-a:\n    mysql_connections: \"12\"\n")
	// An edit to an innocent tenant made while the duplicate is present.
	writeTestYAML(t, filepath.Join(dir, "t-b.yaml"), "tenants:\n  t-b:\n    mysql_connections: \"23\"\n")
	const ticks = 3
	for i := 0; i < ticks; i++ {
		m.tickOnce()
	}

	if got := scanFailures(fresh, ScanFailureReasonDuplicateTenant); got != ticks {
		t.Errorf("flat: scan_failures{duplicate_tenant} = %v after %d failing ticks, want %d", got, ticks, ticks)
	}
	if got := scanFailures(fresh, ScanFailureReasonWalkError); got != 0 {
		t.Errorf("flat: scan_failures{walk_error} = %v, want 0", got)
	}
	if n := reloadTriggerSeries(fresh); n != 0 {
		t.Errorf("flat: da_config_reload_trigger_total has %d series, want 0", n)
	}
	if got := testutil.ToFloat64(fresh.lastScanComplete); got != sentinel {
		t.Errorf("flat: last_scan_complete = %v while the duplicate is present, want it untouched (%d)", got, sentinel)
	}
	if m.GetConfig() != before {
		t.Error("flat: a config was committed while the duplicate is present; want the tree frozen at the last good config")
	}

	if err := os.Remove(filepath.Join(dir, "t-a-copy.yaml")); err != nil {
		t.Fatal(err)
	}
	m.tickOnce()
	if got := scanFailures(fresh, ScanFailureReasonDuplicateTenant); got != ticks {
		t.Errorf("flat: scan_failures{duplicate_tenant} = %v after recovery, want it to stay %d", got, ticks)
	}
	if got := testutil.ToFloat64(fresh.lastScanComplete); got == sentinel {
		t.Error("flat: last_scan_complete not stamped by the first clean tick after recovery")
	}
	if m.GetConfig() == before {
		t.Error("flat: no config committed by the first clean tick after recovery (the pending t-b edit should land)")
	}
}

// The CodeRabbit shape on #2581: flat mode with -scan-debounce longer than
// -reload-interval. When flat detectChange ignored the Conflict, every tick
// saw the tree "changed" and re-armed the debounce timer before it could
// fire, so the reload whose scan would have counted the duplicate never ran
// and the counter sat at 0 for as long as the duplicate stayed. The ticks
// here advance a fake clock by the interval, exactly as WatchLoop does.
func TestTickOnce_FlatMode_DuplicateTenant_DebounceLongerThanInterval(t *testing.T) {
	t.Parallel()
	const (
		window   = 60 * time.Second
		interval = 30 * time.Second
		ticks    = 5
	)
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "t-a.yaml"), "tenants:\n  t-a:\n    mysql_connections: \"11\"\n")
	writeTestYAML(t, filepath.Join(dir, "t-b.yaml"), "tenants:\n  t-b:\n    mysql_connections: \"21\"\n")
	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(dir, window)
	t.Cleanup(m.Close)
	fc := clockwork.NewFakeClock()
	m.SetClock(fc)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	m.mu.RLock()
	hier := m.hierarchy.enabled
	m.mu.RUnlock()
	if hier {
		t.Fatal("fixture must stay in flat mode (no _defaults.yaml)")
	}

	writeTestYAML(t, filepath.Join(dir, "t-a-copy.yaml"), "tenants:\n  t-a:\n    mysql_connections: \"12\"\n")
	for i := 0; i < ticks; i++ {
		m.tickOnce()
		fc.Advance(interval)
	}

	if got := scanFailures(fresh, ScanFailureReasonDuplicateTenant); got != ticks {
		t.Errorf("flat, debounce %v > interval %v: scan_failures{duplicate_tenant} = %v after %d ticks, want %d",
			window, interval, got, ticks, ticks)
	}
	m.debounce.mu.Lock()
	armed := m.debounce.timer != nil
	m.debounce.mu.Unlock()
	if armed {
		t.Error("flat: a debounced reload is armed while the duplicate is present; a failing check must not schedule one")
	}
	if n := reloadTriggerSeries(fresh); n != 0 {
		t.Errorf("flat: da_config_reload_trigger_total has %d series, want 0", n)
	}
}

// Control: an ordinary edit reloads as before — reload_trigger_total{source}
// moves and both scan-failure series exist at 0.
func TestTickOnce_NormalEdit_NoScanFailure(t *testing.T) {
	t.Parallel()
	m, fresh, dir := newScanFailureManager(t)

	writeTestYAML(t, filepath.Join(dir, "team-a", "tenant-a.yaml"),
		"tenants:\n  tenant-a:\n    mysql_connections: \"77\"\n")
	m.tickOnce()

	if got := testutil.ToFloat64(fresh.reloadTriggers.WithLabelValues(ReloadReasonSource)); got != 1 {
		t.Errorf("reload_trigger_total{source} = %v, want 1", got)
	}
	if n := testutil.CollectAndCount(fresh.scanFailures); n != 2 {
		t.Errorf("da_config_scan_failures_total has %d series, want 2 (both reasons pre-initialised)", n)
	}
	for _, r := range []string{ScanFailureReasonDuplicateTenant, ScanFailureReasonWalkError} {
		if got := scanFailures(fresh, r); got != 0 {
			t.Errorf("scan_failures{%s} = %v on a clean reload, want 0", r, got)
		}
	}
	if got := tenantAThreshold(m); got != "77" {
		t.Errorf("tenant-a mysql_connections = %s, want 77", got)
	}
}

// The config directory disappearing under a running exporter is the other
// way ScanDirTree fails; it counts as walk_error.
func TestTickOnce_MissingRoot_CountsWalkError(t *testing.T) {
	t.Parallel()
	m, fresh, dir := newScanFailureManager(t)

	if err := os.RemoveAll(dir); err != nil {
		t.Fatal(err)
	}
	m.tickOnce()

	if got := scanFailures(fresh, ScanFailureReasonWalkError); got != 1 {
		t.Errorf("scan_failures{walk_error} = %v, want 1", got)
	}
	if got := scanFailures(fresh, ScanFailureReasonDuplicateTenant); got != 0 {
		t.Errorf("scan_failures{duplicate_tenant} = %v, want 0", got)
	}
	if n := reloadTriggerSeries(fresh); n != 0 {
		t.Errorf("da_config_reload_trigger_total has %d series, want 0", n)
	}
}

// The debounced reload scans again; a duplicate that appears between the
// tick's check and that scan fails there, and is counted there — once.
func TestDiffAndReload_DuplicateTenant_CountsScanFailure(t *testing.T) {
	t.Parallel()
	m, fresh, dir := newScanFailureManager(t)

	writeTestYAML(t, filepath.Join(dir, "tenant-a-copy.yaml"),
		"tenants:\n  tenant-a:\n    mysql_connections: \"91\"\n")
	if _, _, err := m.diffAndReload(); err == nil {
		t.Fatal("diffAndReload with a duplicate tenant: want an error")
	}

	if got := scanFailures(fresh, ScanFailureReasonDuplicateTenant); got != 1 {
		t.Errorf("scan_failures{duplicate_tenant} = %v, want 1", got)
	}
	if n := reloadTriggerSeries(fresh); n != 0 {
		t.Errorf("da_config_reload_trigger_total has %d series, want 0", n)
	}
}

// The label is a closed set: whatever the error, the classifier answers one
// of the two constants — a wrapped duplicate is still duplicate_tenant, and
// anything else (including an error that merely mentions "duplicate") is
// walk_error.
func TestClassifyScanFailure_ClosedSet(t *testing.T) {
	t.Parallel()
	dup := &DuplicateTenantError{TenantID: "tx", PathA: "/a.yaml", PathB: "/b.yaml"}
	cases := []struct {
		name string
		err  error
		want string
	}{
		{"bare duplicate", dup, ScanFailureReasonDuplicateTenant},
		{"wrapped duplicate", fmt.Errorf("hierarchical scan: %w", dup), ScanFailureReasonDuplicateTenant},
		{"stat error", fmt.Errorf("stat %q: %w", "/x", os.ErrNotExist), ScanFailureReasonWalkError},
		{"text mentions duplicate", errors.New("duplicate tenant ID \"tx\""), ScanFailureReasonWalkError},
	}
	for _, c := range cases {
		if got := classifyScanFailure(c.err); got != c.want {
			t.Errorf("%s: classifyScanFailure = %q, want %q", c.name, got, c.want)
		}
	}
}

// Nil-receiver safe, like the other scan-side methods.
func TestIncScanFailure_NilReceiver(t *testing.T) {
	t.Parallel()
	var cm *configMetrics
	cm.IncScanFailure(errors.New("x")) // must not panic
}
