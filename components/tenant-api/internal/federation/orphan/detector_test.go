package orphan

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"sync/atomic"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/federation/token"
)

func TestScanOrphans(t *testing.T) {
	known := map[string]struct{}{"db-a": {}, "db-b": {}}
	records := []token.Record{
		{TokenID: "ftk_live_a", TenantID: "db-a"},
		{TokenID: "ftk_zombie", TenantID: "db-gone"},
		{TokenID: "ftk_live_b", TenantID: "db-b"},
	}
	subsets := []string{"db-b", "db-stale", "db-a"}

	rep := scanOrphans(known, records, subsets)

	if !reflect.DeepEqual(rep.Tokens, []string{"ftk_zombie"}) {
		t.Errorf("orphaned tokens = %v, want [ftk_zombie]", rep.Tokens)
	}
	if !reflect.DeepEqual(rep.Subsets, []string{"db-stale"}) {
		t.Errorf("orphaned subsets = %v, want [db-stale]", rep.Subsets)
	}
}

func TestScanOrphans_AllKnown(t *testing.T) {
	known := map[string]struct{}{"db-a": {}}
	rep := scanOrphans(known,
		[]token.Record{{TokenID: "t1", TenantID: "db-a"}},
		[]string{"db-a"})
	if !rep.empty() {
		t.Errorf("expected no orphans, got %+v", rep)
	}
}

func TestScanOrphans_Empty(t *testing.T) {
	rep := scanOrphans(map[string]struct{}{}, nil, nil)
	if !rep.empty() {
		t.Errorf("expected empty report, got %+v", rep)
	}
}

// All tenants absent (the conf.d-wiped disaster). The detector reports
// everything orphaned — a true, alarming signal, not damage: it only
// observes and never revokes, so there is no misfire to guard against.
func TestScanOrphans_AllOrphaned(t *testing.T) {
	rep := scanOrphans(map[string]struct{}{},
		[]token.Record{{TokenID: "t1", TenantID: "db-a"}, {TokenID: "t2", TenantID: "db-b"}},
		[]string{"db-a"})
	if len(rep.Tokens) != 2 || len(rep.Subsets) != 1 {
		t.Errorf("expected 2 tokens + 1 subset orphaned, got %+v", rep)
	}
}

func TestScanKnownTenants(t *testing.T) {
	dir := t.TempDir()
	for _, f := range []string{
		"db-a.yaml", "db-b.yml", // tenants
		"_defaults.yaml", "_rbac.yaml", "_federation_policy.yaml", // "_" specials — skipped
		".hidden.yaml", ".gitkeep", // "." hidden files — skipped (regression: scanKnownTenants once skipped only "_")
		"notes.txt", // non-YAML — skipped
	} {
		if err := os.WriteFile(filepath.Join(dir, f), []byte("{}"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	// a subdirectory (e.g. _federation/) — skipped
	if err := os.Mkdir(filepath.Join(dir, federationSubsetDir), 0o700); err != nil {
		t.Fatal(err)
	}
	known, err := scanKnownTenants(dir)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string]struct{}{"db-a": {}, "db-b": {}}
	if !reflect.DeepEqual(known, want) {
		t.Errorf("known tenants = %v, want %v", known, want)
	}
}

// A read error must surface as an error — never as an empty set, which
// scanOrphans would read as "every tenant is gone".
func TestScanKnownTenants_ReadError(t *testing.T) {
	if _, err := scanKnownTenants(filepath.Join(t.TempDir(), "nope")); err == nil {
		t.Fatal("expected an error for a missing configDir, got nil")
	}
}

func TestScanSubsetTenants(t *testing.T) {
	dir := t.TempDir()
	fedDir := filepath.Join(dir, federationSubsetDir)
	if err := os.Mkdir(fedDir, 0o700); err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"db-a.yaml", "db-stale.yaml", "README.md"} {
		if err := os.WriteFile(filepath.Join(fedDir, f), []byte("{}"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	got, err := scanSubsetTenants(dir)
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(got)
	want := []string{"db-a", "db-stale"}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("subset tenants = %v, want %v", got, want)
	}
}

// A missing _federation/ directory is the common case (no tenant has
// configured a subset) — it must not be an error.
func TestScanSubsetTenants_NoDir(t *testing.T) {
	got, err := scanSubsetTenants(t.TempDir())
	if err != nil {
		t.Fatalf("missing _federation/ should not error: %v", err)
	}
	if len(got) != 0 {
		t.Errorf("expected no subset tenants, got %v", got)
	}
}

// ── Detector wiring (scanOnce / Run) ────────────────────────────────────
//
// The tests below exercise the I/O wiring around the pure scanOrphans
// diff: gauge updates and — critically — the fail-safe invariant that a
// READ ERROR anywhere in the pass (conf.d, _federation/, token store)
// skips the pass WITHOUT touching the gauges. A transient failure must
// never be published as "everything is orphaned" (detector.go:152-155).
//
// NOT parallel: orphanedTokens / orphanedSubsets are package-level
// gauges. Each test snapshots and restores them.

// saveGauges snapshots the orphan gauges and restores them on cleanup so
// these tests cannot leak state into each other (or future gauge tests).
func saveGauges(t *testing.T) {
	t.Helper()
	prevTok, prevSub := OrphanCounts()
	t.Cleanup(func() {
		orphanedTokens.Store(prevTok)
		orphanedSubsets.Store(prevSub)
	})
}

// writeTenantFile drops a minimal tenant YAML into dir.
func writeTenantFile(t *testing.T, dir, name string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte("{}"), 0o600); err != nil {
		t.Fatal(err)
	}
}

// seedConfDir builds a conf.d with one live tenant (db-a) and one stale
// federation subset file (db-stale), so a scan against a store holding a
// zombie token yields exactly (1 orphaned token, 1 orphaned subset).
func seedConfDir(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	writeTenantFile(t, dir, "db-a.yaml")
	fedDir := filepath.Join(dir, federationSubsetDir)
	if err := os.Mkdir(fedDir, 0o700); err != nil {
		t.Fatal(err)
	}
	writeTenantFile(t, fedDir, "db-stale.yaml")
	return dir
}

// liveAndZombieRecords returns a records func: one token owned by the
// live tenant, one owned by a tenant no longer in conf.d.
func liveAndZombieRecords() ([]token.Record, error) {
	return []token.Record{
		{TokenID: "ftk_live", TenantID: "db-a"},
		{TokenID: "ftk_zombie", TenantID: "db-gone"},
	}, nil
}

func TestDetector_ScanOnce_UpdatesGauges(t *testing.T) {
	saveGauges(t)
	dir := seedConfDir(t)

	d := NewDetector(dir, liveAndZombieRecords)
	d.scanOnce()

	tok, sub := OrphanCounts()
	if tok != 1 || sub != 1 {
		t.Fatalf("OrphanCounts() = (%d, %d), want (1, 1)", tok, sub)
	}

	// A later clean scan OVERWRITES the gauges back to zero (gauge, not
	// counter semantics): make the zombie's tenant reappear and drop the
	// stale subset file.
	writeTenantFile(t, dir, "db-gone.yaml")
	writeTenantFile(t, filepath.Join(dir, federationSubsetDir), "db-a.yaml")
	if err := os.Remove(filepath.Join(dir, federationSubsetDir, "db-stale.yaml")); err != nil {
		t.Fatal(err)
	}
	d.scanOnce()
	if tok, sub := OrphanCounts(); tok != 0 || sub != 0 {
		t.Errorf("clean rescan OrphanCounts() = (%d, %d), want (0, 0)", tok, sub)
	}
}

// TestDetector_ScanOnce_FailSafeSkipsPass locks the fail-safe invariant:
// every read-error path (conf.d unreadable, _federation/ unreadable,
// token store listing fails) must SKIP the pass and leave the previously
// published gauges untouched — never zero them, never spike them to
// "all orphaned".
func TestDetector_ScanOnce_FailSafeSkipsPass(t *testing.T) {
	saveGauges(t)

	// Publish a known-good baseline first: (1, 1).
	dir := seedConfDir(t)
	d := NewDetector(dir, liveAndZombieRecords)
	d.scanOnce()
	if tok, sub := OrphanCounts(); tok != 1 || sub != 1 {
		t.Fatalf("baseline OrphanCounts() = (%d, %d), want (1, 1)", tok, sub)
	}

	assertGaugesUntouched := func(t *testing.T, when string) {
		t.Helper()
		if tok, sub := OrphanCounts(); tok != 1 || sub != 1 {
			t.Errorf("%s: OrphanCounts() = (%d, %d), want the pre-error (1, 1) — a transient read error leaked into the gauges", when, tok, sub)
		}
	}

	t.Run("confd_unreadable", func(t *testing.T) {
		bad := NewDetector(filepath.Join(t.TempDir(), "nope"), liveAndZombieRecords)
		bad.scanOnce()
		assertGaugesUntouched(t, "conf.d read error")
	})

	t.Run("federation_dir_unreadable", func(t *testing.T) {
		// _federation exists but is a regular FILE: os.ReadDir fails with
		// ENOTDIR — the non-IsNotExist error branch of scanSubsetTenants.
		dir2 := t.TempDir()
		writeTenantFile(t, dir2, "db-a.yaml")
		writeTenantFile(t, dir2, federationSubsetDir) // file, not dir
		bad := NewDetector(dir2, liveAndZombieRecords)
		bad.scanOnce()
		assertGaugesUntouched(t, "_federation/ read error")
	})

	t.Run("token_store_error", func(t *testing.T) {
		bad := NewDetector(dir, func() ([]token.Record, error) {
			return nil, errors.New("configmap get: transient apiserver timeout")
		})
		bad.scanOnce()
		assertGaugesUntouched(t, "token store list error")
	})

	// And the recovery: once the store answers again, the next pass
	// publishes fresh values.
	d.scanOnce()
	if tok, sub := OrphanCounts(); tok != 1 || sub != 1 {
		t.Errorf("recovery scan OrphanCounts() = (%d, %d), want (1, 1)", tok, sub)
	}
}

// TestDetector_Run_ScansImmediatelyTicksAndStops drives the Run loop:
// one scan fires immediately (before any tick), the ticker fires at
// least one more, and closing stopCh terminates the goroutine.
func TestDetector_Run_ScansImmediatelyTicksAndStops(t *testing.T) {
	saveGauges(t)
	dir := seedConfDir(t)

	var scans atomic.Int64
	d := NewDetector(dir, func() ([]token.Record, error) {
		scans.Add(1)
		return liveAndZombieRecords()
	})

	stopCh := make(chan struct{})
	done := make(chan struct{})
	go func() {
		defer close(done)
		d.Run(2*time.Millisecond, stopCh)
	}()

	// Wait for the immediate scan plus at least one ticker-driven scan.
	deadline := time.After(5 * time.Second)
	for scans.Load() < 2 {
		select {
		case <-deadline:
			t.Fatalf("Run performed %d scans within 5s, want >= 2 (immediate + tick)", scans.Load())
		case <-time.After(time.Millisecond):
		}
	}

	close(stopCh)
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not return within 5s of stopCh closing")
	}

	if tok, sub := OrphanCounts(); tok != 1 || sub != 1 {
		t.Errorf("after Run, OrphanCounts() = (%d, %d), want (1, 1)", tok, sub)
	}
}
