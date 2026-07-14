package configwatcher

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

// parseStrictConfig fails on any line containing "!bad", so a test can
// force load() down its parse-error branch. parseTestConfig (the shared
// helper in configwatcher_test.go) never errors.
func parseStrictConfig(data []byte) (*testConfig, error) {
	if strings.Contains(string(data), "!bad") {
		return nil, errors.New("synthetic parse failure")
	}
	return parseTestConfig(data)
}

// TestReload_RacesWatchLoop asserts that a caller-driven Reload() running
// concurrently with the WatchLoop ticker is free of data races. Only
// federation-policy has both a WatchLoop and a handler-driven Reload()
// (main.go starts its loop; PutFederationPolicy calls Reload after the
// commit), so this is a real production interleaving and not a synthetic
// one. Meaningful under `go test -race`.
func TestReload_RacesWatchLoop(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := filepath.Join(dir, "cfg.yaml")
	if err := os.WriteFile(path, []byte("a: 1\n"), 0644); err != nil {
		t.Fatalf("seed: %v", err)
	}

	w, err := New(path, "test", parseTestConfig, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	stopCh := make(chan struct{})

	// The generic Watcher exposes only a real ticker (no fake-clock seam —
	// configwatcher.WatchLoop calls time.NewTicker directly, as rbac's WatchLoop
	// tests also drive it). The loop runs concurrently with the workers below and
	// is stopped once they finish, so the test's duration is bounded by the
	// bounded workers, NOT by a wall-clock time.Sleep. The race pressure does not
	// depend on how many times the ticker happens to fire: the four concurrent
	// Reload goroutines already exercise the lastHash race against each other, and
	// the loop's own load() calls add the WatchLoop-vs-Reload dimension on top.
	var loopWg sync.WaitGroup
	loopWg.Add(1)
	go func() { defer loopWg.Done(); w.WatchLoop(time.Millisecond, stopCh) }()

	var workWg sync.WaitGroup

	// Rewriter: keeps the file's content (and therefore its hash) moving so
	// the SHA-256 dedup short-circuit does not hide the race.
	workWg.Add(1)
	go func() {
		defer workWg.Done()
		for i := 0; i < 200; i++ {
			_ = os.WriteFile(path, []byte("a: "+string(rune('A'+i%26))+"\n"), 0644)
		}
	}()

	for i := 0; i < 4; i++ {
		workWg.Add(1)
		go func() {
			defer workWg.Done()
			for j := 0; j < 200; j++ {
				_ = w.Reload()
				_ = w.Get()
				_ = w.LastHash()
			}
		}()
	}

	workWg.Wait() // bounded workers done; the loop ran concurrently throughout
	close(stopCh) // now stop the loop
	loopWg.Wait() // clean shutdown
}

// TestReload_MissingFile_StoresEmptyAndReturnsNil pins a contract that
// looks like a bug and is not: a configured path whose file has vanished
// reloads to the EMPTY snapshot and reports success.
//
// This is load-bearing. For rbac it is the mechanism behind ADR-027
// MED-8 fail-closed (an empty group set with failClosedOnEmpty denies
// everything — see internal/rbac config_reload_test.go TestLoad_DeletedFile), and for
// every manager it is how a GitOps `git rm` of the config takes effect.
// "Hardening" this into an error would keep a deleted _rbac.yaml
// granting permissions.
func TestReload_MissingFile_StoresEmptyAndReturnsNil(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := filepath.Join(dir, "cfg.yaml")
	if err := os.WriteFile(path, []byte("a: 1\n"), 0644); err != nil {
		t.Fatalf("seed: %v", err)
	}

	w, err := New(path, "test", parseTestConfig, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if got := len(w.Get().Items); got != 1 {
		t.Fatalf("seeded items = %d, want 1", got)
	}

	if err := os.Remove(path); err != nil {
		t.Fatalf("remove: %v", err)
	}
	if err := w.Reload(); err != nil {
		t.Fatalf("Reload after delete = %v, want nil (see doc comment)", err)
	}
	if got := len(w.Get().Items); got != 0 {
		t.Errorf("items after delete = %d, want 0 (empty snapshot)", got)
	}
}

// newBrokenWatcher seeds a good file, loads it, then corrupts the file
// on disk. Returns the watcher and the hash of the good content.
func newBrokenWatcher(t *testing.T) (*Watcher[testConfig], string) {
	t.Helper()
	dir := t.TempDir()
	path := filepath.Join(dir, "cfg.yaml")
	if err := os.WriteFile(path, []byte("a: 1\n"), 0644); err != nil {
		t.Fatalf("seed: %v", err)
	}
	w, err := New(path, "test", parseStrictConfig, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	goodHash := w.LastHash()
	if err := os.WriteFile(path, []byte("!bad\n"), 0644); err != nil {
		t.Fatalf("corrupt: %v", err)
	}
	return w, goodHash
}

// TestLoad_ParseFailure_KeepsLastGoodHashSoFailuresRepeat covers the
// WatchLoop path (load() without the Reload hash reset). A parse error
// leaves lastHash holding the last GOOD hash, so the next tick re-reads
// the still-broken file, sees a different hash, re-parses and re-fails
// rather than deduping the failure into a silent success.
//
// This is why a persistently broken WatchLoop-managed file makes the
// reload-failure counter climb on every tick, which in turn is why
// `increase(tenant_api_config_reload_failures_total[15m]) > 2` is a
// sound alert for those managers.
func TestLoad_ParseFailure_KeepsLastGoodHashSoFailuresRepeat(t *testing.T) {
	t.Parallel()
	w, goodHash := newBrokenWatcher(t)

	if err := w.load(); err == nil {
		t.Fatal("load of unparseable file = nil, want error")
	}
	if got := w.Get().Items["a"]; got != "1" {
		t.Errorf("snapshot after failed load = %q, want last-good %q", got, "1")
	}
	if got := w.LastHash(); got != goodHash {
		t.Errorf("lastHash after failed load = %q, want the last GOOD hash %q", got, goodHash)
	}
	// The dedup short-circuit must not swallow the repeat failure.
	if err := w.load(); err == nil {
		t.Fatal("second load of the same broken file = nil, want error")
	}
}

// TestReload_ParseFailure_KeepsLastGoodSnapshot covers the handler path.
// Reload() clears lastHash before loading, so on failure the hash is left
// empty (forcing an uncached re-read next time) while the snapshot stays
// last-good. The handler is therefore serving a config that no longer
// matches disk — the divergence this error return must not be discarded.
func TestReload_ParseFailure_KeepsLastGoodSnapshot(t *testing.T) {
	t.Parallel()
	w, _ := newBrokenWatcher(t)

	if err := w.Reload(); err == nil {
		t.Fatal("Reload of unparseable file = nil, want error")
	}
	if got := w.Get().Items["a"]; got != "1" {
		t.Errorf("snapshot after failed reload = %q, want last-good %q", got, "1")
	}
	if got := w.LastHash(); got != "" {
		t.Errorf("lastHash after failed reload = %q, want \"\" (uncached re-read next time)", got)
	}
}
