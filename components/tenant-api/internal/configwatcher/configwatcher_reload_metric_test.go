package configwatcher

import (
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"
)

// parseErrOnBoom rejects any payload containing "boom" (so a WatchLoop tick can
// be driven into the reload-failure branch), otherwise defers to parseTestConfig.
func parseErrOnBoom(data []byte) (*testConfig, error) {
	if strings.Contains(string(data), "boom") {
		return nil, fmt.Errorf("synthetic parse error")
	}
	return parseTestConfig(data)
}

// fakeReloadRecorder is a test double for ReloadFailureRecorder counting calls
// per component. Concurrency-safe: WatchLoop calls IncReloadFailure on its own
// goroutine while the test reads via count().
type fakeReloadRecorder struct {
	mu     sync.Mutex
	counts map[string]int
}

func (f *fakeReloadRecorder) IncReloadFailure(component string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.counts == nil {
		f.counts = map[string]int{}
	}
	f.counts[component]++
}

func (f *fakeReloadRecorder) count(component string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.counts[component]
}

// A WatchLoop reload that fails to parse must (a) increment the injected
// recorder keyed by the watcher's label and (b) keep serving the last-good
// snapshot (the whole reason the failure is otherwise silent).
//
// Dogfood note: removing the `w.reloadFail.IncReloadFailure(w.label)` line in
// WatchLoop makes this test fail (count stays 0) — proving the assertion is
// load-bearing, not an article of faith (see match-existing-metric-injection).
func TestWatchLoop_ReloadFailure_RecordsMetricAndKeepsLastGood(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	rec := &fakeReloadRecorder{}
	w.SetReloadFailureRecorder(rec) // installed before WatchLoop starts

	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.WatchLoop(20*time.Millisecond, stop)
	}()

	// Rewrite to a payload the parser rejects → reload fails on the next tick.
	writeYAML(t, dir, "foo: boom\n")

	// Poll for the recorded failure (flake guard for slow CI).
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if rec.count("test-comp") > 0 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	close(stop)
	wg.Wait()

	if got := rec.count("test-comp"); got == 0 {
		t.Errorf("reload failure not recorded: IncReloadFailure count = 0, want >=1")
	}
	// Last-good preserved: the failed parse must NOT have replaced the snapshot.
	if got := w.Get().Items["foo"]; got != "good" {
		t.Errorf("last-good not preserved after failed reload: foo = %q, want good", got)
	}
}

// A post-write Reload that fails to parse must ALSO increment the recorder.
// Every production caller discards Reload's returned error (`_ = mgr.Reload()`
// in handler/group.go, handler/view.go, handler/federation/policy.go) and then
// answers 200 OK, so the counter is the only signal that the manager silently
// went stale. Without this, groups/views reload failures are invisible.
func TestReload_Failure_RecordsMetricAndKeepsLastGood(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	rec := &fakeReloadRecorder{}
	w.SetReloadFailureRecorder(rec)

	writeYAML(t, dir, "foo: boom\n")
	if err := w.Reload(); err == nil {
		t.Fatal("Reload on an unparseable file should return an error")
	}
	if got := rec.count("test-comp"); got != 1 {
		t.Errorf("Reload failure not recorded: count = %d, want 1", got)
	}
	if got := w.Get().Items["foo"]; got != "good" {
		t.Errorf("last-good not preserved after failed Reload: foo = %q, want good", got)
	}
}

// A failing Reload with NO recorder installed (the default) must not panic on the
// nil sink — it just returns the error and keeps last-good.
func TestReload_Failure_NilRecorderNoPanic(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	// Intentionally no SetReloadFailureRecorder → nil sink.

	writeYAML(t, dir, "foo: boom\n")
	if err := w.Reload(); err == nil {
		t.Fatal("Reload on an unparseable file should return an error")
	}
	if got := w.Get().Items["foo"]; got != "good" {
		t.Errorf("last-good not preserved after failed Reload: foo = %q, want good", got)
	}
}

// A SUCCESSFUL Reload must not touch the counter (guards against counting every
// post-write refresh, which would make the alert meaningless).
func TestReload_Success_DoesNotRecord(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	rec := &fakeReloadRecorder{}
	w.SetReloadFailureRecorder(rec)

	writeYAML(t, dir, "foo: better\n")
	if err := w.Reload(); err != nil {
		t.Fatalf("Reload: %v", err)
	}
	if got := rec.count("test-comp"); got != 0 {
		t.Errorf("successful Reload recorded a failure: count = %d, want 0", got)
	}
	if got := w.Get().Items["foo"]; got != "better" {
		t.Errorf("successful Reload did not apply: foo = %q, want better", got)
	}
}

// With no recorder installed (the default), a failing reload must NOT panic on
// a nil sink — it just logs the WARN and keeps last-good.
func TestWatchLoop_ReloadFailure_NilRecorderNoPanic(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	// Intentionally no SetReloadFailureRecorder → nil sink.

	stop := make(chan struct{})
	var wg sync.WaitGroup
	wg.Add(1)
	go func() {
		defer wg.Done()
		w.WatchLoop(20*time.Millisecond, stop)
	}()

	writeYAML(t, dir, "foo: boom\n")
	time.Sleep(80 * time.Millisecond) // let a couple of failing ticks fire
	close(stop)
	wg.Wait()

	// Reaching here without a panic is the assertion; last-good still served.
	if got := w.Get().Items["foo"]; got != "good" {
		t.Errorf("last-good not preserved: foo = %q, want good", got)
	}
}
