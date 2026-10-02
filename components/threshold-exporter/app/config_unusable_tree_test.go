package main

// #2592: the configuration mistakes whose signal did not match the state.
//
//   §1 a root that cannot be listed, or a tree emptied of config files: the
//      tree was already frozen, but the scan counted as clean (the last-scan
//      gauge kept moving, so ConfigScanFailing could not fire) and the
//      hierarchical reload counted every tenant as reload_trigger{delete}.
//      Now scan_failures{root_unreadable|empty_tree}, gauge untouched, no
//      reload scheduled.
//   §2 an unreadable tenant file / unlistable subdirectory: the tenant
//      disappeared with no series. Now da_config_unreadable_files{reason}.
//   §3 a broken or unreadable `_defaults.yaml`: every series fed by it
//      disappeared while the parse-failure counter stopped moving after one
//      reload (and an unreadable one never moved it). Now
//      da_config_defaults_unusable{reason}, held for as long as it lasts.
//
// Permission shapes cannot be built as root (root reads mode-0 files and
// lists mode-0 directories), so those tests skip under root — CI's Go job
// runs as a non-root user. Each section also has a shape every user can
// build: a dangling symlink is stat_error for root too, and an emptied tree
// or a synthetic TreeScan needs no permission at all.
//
// Seams: freshMetrics + SetMetrics and a discarding logger per test
// (test-map.md §測試注入 Seam), so every test runs in parallel.

import (
	"errors"
	"io"
	"io/fs"
	"log"
	"os"
	"path/filepath"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func skipAsRoot(t *testing.T) {
	t.Helper()
	if os.Geteuid() == 0 {
		t.Skip("running as root: chmod does not stop root; run the test binary as a non-root user")
	}
}

// chmodRestore chmods p to mode and restores restore at cleanup, so
// t.TempDir can remove the tree.
func chmodRestore(t *testing.T, p string, mode, restore fs.FileMode) {
	t.Helper()
	if err := os.Chmod(p, mode); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(p, restore) })
}

// newUnusableTreeManager loads a tree through a synchronous-debounce manager
// wired to fresh metrics, after write fills it. hier=true is the
// hierarchical fixture (root _defaults.yaml + team-a/tenant-a.yaml) plus a
// root tenant-b.yaml; hier=false is two flat tenant files.
func newUnusableTreeManager(t *testing.T, hier bool) (*ConfigManager, *configMetrics, string) {
	t.Helper()
	dir := t.TempDir()
	if hier {
		writeHierarchicalFixture(t, dir, "90")
		writeTestYAML(t, filepath.Join(dir, "tenant-b.yaml"), "tenants:\n  tenant-b:\n    mysql_connections: \"21\"\n")
	} else {
		writeTestYAML(t, filepath.Join(dir, "tenant-a.yaml"), "tenants:\n  tenant-a:\n    mysql_connections: \"11\"\n")
		writeTestYAML(t, filepath.Join(dir, "tenant-b.yaml"), "tenants:\n  tenant-b:\n    mysql_connections: \"21\"\n")
	}
	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(dir, 0)
	t.Cleanup(m.Close)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	m.mu.RLock()
	got := m.hierarchy.enabled
	m.mu.RUnlock()
	if got != hier {
		t.Fatalf("fixture mode: hierarchy.enabled = %v, want %v", got, hier)
	}
	return m, fresh, dir
}

func modeName(hier bool) string {
	if hier {
		return "hierarchical"
	}
	return "flat"
}

// assertFrozenScanFailure is §1's contract after `ticks` failing ticks: the
// one reason counted once per tick, every other reason 0, the last-scan
// gauge still at the sentinel, no reload of any reason counted, and the
// config the manager serves is still the one from before.
func assertFrozenScanFailure(t *testing.T, m *ConfigManager, fresh *configMetrics, before *ThresholdConfig, reason string, ticks int) {
	t.Helper()
	for _, r := range scanFailureReasons {
		want := 0.0
		if r == reason {
			want = float64(ticks)
		}
		if got := scanFailures(fresh, r); got != want {
			t.Errorf("scan_failures{%s} = %v after %d ticks, want %v", r, got, ticks, want)
		}
	}
	if got := testutil.ToFloat64(fresh.lastScanComplete); got != 1 {
		t.Errorf("last_scan_complete = %v, want it untouched (1): the tree is not scannable", got)
	}
	if n := reloadTriggerSeries(fresh); n != 0 {
		t.Errorf("da_config_reload_trigger_total has %d series (delete=%v), want 0: nothing was reloaded or deleted",
			n, testutil.ToFloat64(fresh.reloadTriggers.WithLabelValues(ReloadReasonDelete)))
	}
	if m.GetConfig() != before {
		t.Error("a config was committed; want the tree frozen at the last good config")
	}
}

// §1, empty tree — every user. Both modes: removing every config file
// counts empty_tree once per tick, and putting one back recovers.
func TestTickOnce_EmptyTree_CountsScanFailure(t *testing.T) {
	t.Parallel()
	for _, hier := range []bool{false, true} {
		hier := hier
		t.Run(modeName(hier), func(t *testing.T) {
			t.Parallel()
			m, fresh, dir := newUnusableTreeManager(t, hier)
			before := m.GetConfig()
			fresh.lastScanComplete.Set(1)
			err := filepath.WalkDir(dir, func(p string, d fs.DirEntry, err error) error {
				if err == nil && !d.IsDir() {
					return os.Remove(p)
				}
				return err
			})
			if err != nil {
				t.Fatal(err)
			}

			const ticks = 3
			for i := 0; i < ticks; i++ {
				m.tickOnce()
			}
			assertFrozenScanFailure(t, m, fresh, before, ScanFailureReasonEmptyTree, ticks)

			writeTestYAML(t, filepath.Join(dir, "tenant-c.yaml"), "tenants:\n  tenant-c:\n    mysql_connections: \"5\"\n")
			m.tickOnce()
			if got := scanFailures(fresh, ScanFailureReasonEmptyTree); got != ticks {
				t.Errorf("scan_failures{empty_tree} = %v after recovery, want it to stay %d", got, ticks)
			}
			if got := testutil.ToFloat64(fresh.lastScanComplete); got == 1 {
				t.Error("last_scan_complete not stamped by the first clean tick after recovery")
			}
			if _, ok := m.GetConfig().Tenants["tenant-c"]; !ok {
				t.Error("tenant-c not served after recovery")
			}
		})
	}
}

// §1, empty tree where every file is still there but none can be used — a
// dangling symlink is unusable for every user, root included. The walk's
// drops are published even though the verdict rejects the scan (§2), so
// the operator sees why the tree is empty.
func TestTickOnce_AllFilesUnusable_EmptyTreeAndUnreadableGauge(t *testing.T) {
	t.Parallel()
	m, fresh, dir := newUnusableTreeManager(t, false)
	before := m.GetConfig()
	fresh.lastScanComplete.Set(1)
	for _, n := range []string{"tenant-a.yaml", "tenant-b.yaml"} {
		p := filepath.Join(dir, n)
		if err := os.Remove(p); err != nil {
			t.Fatal(err)
		}
		if err := os.Symlink(filepath.Join(dir, "missing-"+n), p); err != nil {
			t.Fatal(err)
		}
	}
	m.tickOnce()
	m.tickOnce()
	assertFrozenScanFailure(t, m, fresh, before, ScanFailureReasonEmptyTree, 2)
	if got := testutil.ToFloat64(fresh.unreadableFiles[config.UnreadableStatError]); got != 2 {
		t.Errorf("unreadable_files{stat_error} = %v, want 2 (both dangling links)", got)
	}
}

// §1, unlistable root — non-root only. The hierarchical shape the ticket
// measured: before the fix every tick counted all tenants as deletes.
func TestTickOnce_UnlistableRoot_CountsRootUnreadable(t *testing.T) {
	skipAsRoot(t)
	t.Parallel()
	for _, hier := range []bool{false, true} {
		hier := hier
		t.Run(modeName(hier), func(t *testing.T) {
			t.Parallel()
			m, fresh, dir := newUnusableTreeManager(t, hier)
			before := m.GetConfig()
			fresh.lastScanComplete.Set(1)
			chmodRestore(t, dir, 0o300, 0o755) // searchable, not listable

			const ticks = 3
			for i := 0; i < ticks; i++ {
				m.tickOnce()
			}
			assertFrozenScanFailure(t, m, fresh, before, ScanFailureReasonRootUnreadable, ticks)

			if err := os.Chmod(dir, 0o755); err != nil {
				t.Fatal(err)
			}
			m.tickOnce()
			if got := testutil.ToFloat64(fresh.lastScanComplete); got == 1 {
				t.Error("last_scan_complete not stamped once the root is listable again")
			}
			if got := scanFailures(fresh, ScanFailureReasonRootUnreadable); got != ticks {
				t.Errorf("scan_failures{root_unreadable} = %v after recovery, want it to stay %d", got, ticks)
			}
		})
	}
}

// §1 on the debounced reload's own scan: a reload already scheduled when
// the tree empties fails its scan and counts once — and must not reach
// classifyAndCount, which counted every tenant as a delete.
func TestDiffAndReload_EmptyTree_CountsScanFailureNotDeletes(t *testing.T) {
	t.Parallel()
	m, fresh, dir := newUnusableTreeManager(t, true)
	for _, p := range []string{"_defaults.yaml", "tenant-b.yaml", filepath.Join("team-a", "tenant-a.yaml")} {
		if err := os.Remove(filepath.Join(dir, p)); err != nil {
			t.Fatal(err)
		}
	}
	if _, _, err := m.diffAndReload(); err == nil {
		t.Fatal("diffAndReload on an empty tree: want an error")
	}
	if got := scanFailures(fresh, ScanFailureReasonEmptyTree); got != 1 {
		t.Errorf("scan_failures{empty_tree} = %v, want 1", got)
	}
	if n := reloadTriggerSeries(fresh); n != 0 {
		t.Errorf("da_config_reload_trigger_total has %d series (delete=%v), want 0",
			n, testutil.ToFloat64(fresh.reloadTriggers.WithLabelValues(ReloadReasonDelete)))
	}
}

// scanVerdict and TreeScan.Usable (the last-scan stamp's condition) must
// judge every shape alike, and the verdict's error must classify to the
// reason it names. The root_unreadable shape is synthetic here, so this
// covers its classification for root too.
func TestScanVerdict_AgreesWithUsable(t *testing.T) {
	t.Parallel()
	oneFile := map[string]*treeFile{"a.yaml": {}}
	cases := []struct {
		name   string
		scan   *treeScan
		reason string // "" = usable
	}{
		{"clean", &treeScan{Files: oneFile}, ""},
		{"duplicate", &treeScan{Files: oneFile, Conflict: &DuplicateTenantError{TenantID: "tx"}}, ScanFailureReasonDuplicateTenant},
		{"root unlistable, nothing seen", &treeScan{RootWalkErr: fs.ErrPermission}, ScanFailureReasonRootUnreadable},
		{"root unlistable, part seen", &treeScan{Files: oneFile, RootWalkErr: fs.ErrPermission}, ScanFailureReasonRootUnreadable},
		{"empty", &treeScan{}, ScanFailureReasonEmptyTree},
	}
	for _, c := range cases {
		err := scanVerdict(c.scan, "/conf.d")
		if (err == nil) != c.scan.Usable() {
			t.Errorf("%s: scanVerdict err = %v but Usable() = %v", c.name, err, c.scan.Usable())
		}
		if c.reason == "" {
			if err != nil {
				t.Errorf("%s: scanVerdict = %v, want nil", c.name, err)
			}
			continue
		}
		if err == nil {
			t.Errorf("%s: scanVerdict = nil, want %s", c.name, c.reason)
			continue
		}
		if got := classifyScanFailure(err); got != c.reason {
			t.Errorf("%s: classifyScanFailure = %q, want %q", c.name, got, c.reason)
		}
		if got := classifyScanFailure(errors.Join(errors.New("hierarchical scan"), err)); got != c.reason {
			t.Errorf("%s: wrapped: classifyScanFailure = %q, want %q", c.name, got, c.reason)
		}
	}
}

func unreadableGauge(cm *configMetrics, reason string) float64 {
	return testutil.ToFloat64(cm.unreadableFiles[reason])
}

func defaultsUnusableGauge(cm *configMetrics, reason string) float64 {
	return testutil.ToFloat64(cm.defaultsUnusable[reason])
}

// §2, every user: a dangling symlink with a tenant-file name is stat_error.
// The gauge is per walk — it drops back to 0 once the link is gone — and
// every reason exists at 0 from the start.
func TestUnreadableFilesGauge_DanglingSymlink(t *testing.T) {
	t.Parallel()
	m, fresh, dir := newUnusableTreeManager(t, true)
	if n := testutil.CollectAndCount(fresh.set.UnreadableFiles); n != len(unreadableFileReasons) {
		t.Errorf("da_config_unreadable_files has %d series, want %d (every reason pre-created)", n, len(unreadableFileReasons))
	}
	link := filepath.Join(dir, "tenant-x.yaml")
	if err := os.Symlink(filepath.Join(dir, "nowhere.yaml"), link); err != nil {
		t.Fatal(err)
	}
	m.tickOnce()
	if got := unreadableGauge(fresh, config.UnreadableStatError); got != 1 {
		t.Errorf("unreadable_files{stat_error} = %v, want 1", got)
	}
	for _, r := range []string{config.UnreadableReadError, config.UnreadableWalkError} {
		if got := unreadableGauge(fresh, r); got != 0 {
			t.Errorf("unreadable_files{%s} = %v, want 0", r, got)
		}
	}
	if err := os.Remove(link); err != nil {
		t.Fatal(err)
	}
	m.tickOnce()
	if got := unreadableGauge(fresh, config.UnreadableStatError); got != 0 {
		t.Errorf("unreadable_files{stat_error} = %v after the link was removed, want 0", got)
	}
}

// §2, non-root: a mode-0 tenant file is read_error, a mode-0 subdirectory
// walk_error; the other tenants keep applying, and both return to 0.
func TestUnreadableFilesGauge_PermissionDenied(t *testing.T) {
	skipAsRoot(t)
	t.Parallel()
	m, fresh, dir := newUnusableTreeManager(t, true)
	chmodRestore(t, filepath.Join(dir, "tenant-b.yaml"), 0, 0o600)
	chmodRestore(t, filepath.Join(dir, "team-a"), 0, 0o755)
	writeTestYAML(t, filepath.Join(dir, "tenant-c.yaml"), "tenants:\n  tenant-c:\n    mysql_connections: \"5\"\n")
	m.tickOnce()

	if got := unreadableGauge(fresh, config.UnreadableReadError); got != 1 {
		t.Errorf("unreadable_files{read_error} = %v, want 1 (tenant-b.yaml)", got)
	}
	if got := unreadableGauge(fresh, config.UnreadableWalkError); got != 1 {
		t.Errorf("unreadable_files{walk_error} = %v, want 1 (team-a/)", got)
	}
	if _, ok := m.GetConfig().Tenants["tenant-c"]; !ok {
		t.Error("tenant-c not served: an unreadable file must not freeze the rest of the tree")
	}
	if got := scanFailures(fresh, ScanFailureReasonEmptyTree) + scanFailures(fresh, ScanFailureReasonRootUnreadable); got != 0 {
		t.Errorf("an unreadable file below the root counted %v scan failures, want 0", got)
	}

	if err := os.Chmod(filepath.Join(dir, "tenant-b.yaml"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(filepath.Join(dir, "team-a"), 0o755); err != nil {
		t.Fatal(err)
	}
	m.tickOnce()
	for _, r := range unreadableFileReasons {
		if got := unreadableGauge(fresh, r); got != 0 {
			t.Errorf("unreadable_files{%s} = %v once readable again, want 0", r, got)
		}
	}
}

// §3, every user: a root `_defaults.yaml` that does not parse. The counter
// moves on the one reload that reads it; the gauge must read 1 on every
// tick after that, for as long as the file stays broken, and 0 once fixed.
func TestDefaultsUnusableGauge_ParseFailureHeldUntilFixed(t *testing.T) {
	t.Parallel()
	m, fresh, dir := newUnusableTreeManager(t, true)
	if n := testutil.CollectAndCount(fresh.set.DefaultsUnusable); n != len(defaultsUnusableReasons) {
		t.Errorf("da_config_defaults_unusable has %d series, want %d (every reason pre-created)", n, len(defaultsUnusableReasons))
	}
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults: [unclosed\n")
	m.tickOnce()
	for i := 0; i < 3; i++ {
		m.tickOnce()
		if got := defaultsUnusableGauge(fresh, DefaultsUnusableReasonParseFailure); got != 1 {
			t.Errorf("tick %d: defaults_unusable{parse_failure} = %v while the file is broken, want 1", i, got)
		}
	}
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	m.tickOnce()
	if got := defaultsUnusableGauge(fresh, DefaultsUnusableReasonParseFailure); got != 0 {
		t.Errorf("defaults_unusable{parse_failure} = %v after the fix, want 0", got)
	}
}

// §3, cold start: a broken root `_defaults.yaml` at Load is counted too.
func TestDefaultsUnusableGauge_ColdLoad(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeHierarchicalFixture(t, dir, "90")
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults: [unclosed\n")
	fresh, _ := freshMetrics(t)
	m := NewConfigManagerWithDebounce(dir, 0)
	t.Cleanup(m.Close)
	m.SetMetrics(fresh)
	m.SetLogger(log.New(io.Discard, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := defaultsUnusableGauge(fresh, DefaultsUnusableReasonParseFailure); got != 1 {
		t.Errorf("defaults_unusable{parse_failure} = %v after a cold load, want 1", got)
	}
}

// §3, every user: the root `_defaults.yaml` replaced by a dangling symlink
// is unreadable (the shape the ticket's comment found quieter than a broken
// file: no counter moved at all). Restoring it returns the gauge to 0.
func TestDefaultsUnusableGauge_UnreadableDanglingLink(t *testing.T) {
	t.Parallel()
	m, fresh, dir := newUnusableTreeManager(t, true)
	p := filepath.Join(dir, "_defaults.yaml")
	if err := os.Remove(p); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(dir, "gone.yaml"), p); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 2; i++ {
		m.tickOnce()
		if got := defaultsUnusableGauge(fresh, DefaultsUnusableReasonUnreadable); got != 1 {
			t.Errorf("tick %d: defaults_unusable{unreadable} = %v, want 1", i, got)
		}
	}
	if got := defaultsUnusableGauge(fresh, DefaultsUnusableReasonParseFailure); got != 0 {
		t.Errorf("defaults_unusable{parse_failure} = %v, want 0", got)
	}
	if err := os.Remove(p); err != nil {
		t.Fatal(err)
	}
	writeTestYAML(t, p, "defaults:\n  mysql_connections: 80\n")
	m.tickOnce()
	if got := defaultsUnusableGauge(fresh, DefaultsUnusableReasonUnreadable); got != 0 {
		t.Errorf("defaults_unusable{unreadable} = %v after restore, want 0", got)
	}
}

// §3, non-root: the ticket comment's exact shape, a mode-0 root
// `_defaults.yaml`.
func TestDefaultsUnusableGauge_PermissionDenied(t *testing.T) {
	skipAsRoot(t)
	t.Parallel()
	m, fresh, dir := newUnusableTreeManager(t, true)
	chmodRestore(t, filepath.Join(dir, "_defaults.yaml"), 0, 0o600)
	m.tickOnce()
	m.tickOnce()
	if got := defaultsUnusableGauge(fresh, DefaultsUnusableReasonUnreadable); got != 1 {
		t.Errorf("defaults_unusable{unreadable} = %v, want 1", got)
	}
	if got := unreadableGauge(fresh, config.UnreadableReadError); got != 1 {
		t.Errorf("unreadable_files{read_error} = %v, want 1", got)
	}
}

// The counting rules, on synthetic input: only `_defaults` names count
// (either spelling, any level), and a walk_error directory is never a
// defaults file even when it is named like one.
func TestSetDefaultsUnusable_CountsOnlyDefaultsFiles(t *testing.T) {
	t.Parallel()
	fresh, _ := freshMetrics(t)
	fresh.SetDefaultsUnusable(
		[]string{"_defaults.yaml", "team/_defaults.yml", "tenant-a.yaml", "_profiles.yaml"},
		[]config.UnreadableFile{
			{RelKey: "team/_defaults.yaml", Reason: config.UnreadableReadError},
			{RelKey: "_defaults.yml", Reason: config.UnreadableStatError},
			{RelKey: "_defaults.yaml", Reason: config.UnreadableWalkError},
			{RelKey: "tenant-b.yaml", Reason: config.UnreadableReadError},
		})
	if got := defaultsUnusableGauge(fresh, DefaultsUnusableReasonParseFailure); got != 2 {
		t.Errorf("parse_failure = %v, want 2", got)
	}
	if got := defaultsUnusableGauge(fresh, DefaultsUnusableReasonUnreadable); got != 2 {
		t.Errorf("unreadable = %v, want 2", got)
	}
}

// Nil-receiver safe, like the other scan-side methods (scanDirTree calls
// SetUnreadableFiles with whatever metrics it was handed).
func TestUnusableTreeSetters_NilReceiver(t *testing.T) {
	t.Parallel()
	var cm *configMetrics
	cm.SetUnreadableFiles([]config.UnreadableFile{{RelKey: "a.yaml", Reason: config.UnreadableReadError}})
	cm.SetDefaultsUnusable([]string{"_defaults.yaml"}, nil)
	if _, err := scanDirTree(t.TempDir(), nil, nil, log.New(io.Discard, "", 0)); err != nil {
		t.Fatalf("scanDirTree with nil metrics: %v", err)
	}
}
