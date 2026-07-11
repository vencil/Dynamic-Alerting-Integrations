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

// fakeReloadObserver is a test double for ReloadObserver: it counts failures
// (ok=false) and remembers the last outcome per component. Concurrency-safe:
// WatchLoop calls RecordReload on its own goroutine while the test reads.
type fakeReloadObserver struct {
	mu     sync.Mutex
	fails  map[string]int
	lastOK map[string]bool
}

func (f *fakeReloadObserver) RecordReload(component string, ok bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fails == nil {
		f.fails, f.lastOK = map[string]int{}, map[string]bool{}
	}
	f.lastOK[component] = ok
	if !ok {
		f.fails[component]++
	}
}

func (f *fakeReloadObserver) failCount(component string) int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.fails[component]
}

func (f *fakeReloadObserver) last(component string) (ok bool, recorded bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	v, ok2 := f.lastOK[component]
	return v, ok2
}

// A WatchLoop reload that fails to parse must (a) record a failure outcome on
// the injected observer keyed by the watcher's label and (b) keep serving the
// last-good snapshot (the whole reason the failure is otherwise silent).
//
// Dogfood note: removing the `defer … RecordReload(w.label, err == nil)` in
// load() makes this test fail (failCount stays 0) — proving the assertion is
// load-bearing, not an article of faith (see match-existing-metric-injection).
func TestWatchLoop_ReloadFailure_RecordsOutcomeAndKeepsLastGood(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	obs := &fakeReloadObserver{}
	w.SetReloadObserver(obs) // installed before WatchLoop starts

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
		if obs.failCount("test-comp") > 0 {
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	close(stop)
	wg.Wait()

	if got := obs.failCount("test-comp"); got == 0 {
		t.Errorf("reload failure not recorded: failCount = 0, want >=1")
	}
	if ok, recorded := obs.last("test-comp"); !recorded || ok {
		t.Errorf("last outcome = (%v, recorded=%v), want (false, true)", ok, recorded)
	}
	// Last-good preserved: the failed parse must NOT have replaced the snapshot.
	if got := w.Get().Items["foo"]; got != "good" {
		t.Errorf("last-good not preserved after failed reload: foo = %q, want good", got)
	}
}

// A post-write Reload that fails to parse must ALSO record a failure outcome.
// Every production caller discards Reload's returned error (`_ = mgr.Reload()`
// in handler/group.go, handler/view.go, handler/federation/policy.go) and then
// answers 200 OK, so the observer is the only signal that the manager silently
// went stale. Without this, groups/views reload failures are invisible.
func TestReload_Failure_RecordsOutcomeAndKeepsLastGood(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	obs := &fakeReloadObserver{}
	w.SetReloadObserver(obs)

	writeYAML(t, dir, "foo: boom\n")
	if err := w.Reload(); err == nil {
		t.Fatal("Reload on an unparseable file should return an error")
	}
	if got := obs.failCount("test-comp"); got != 1 {
		t.Errorf("Reload failure not recorded: failCount = %d, want 1", got)
	}
	if ok, _ := obs.last("test-comp"); ok {
		t.Errorf("last outcome ok = true, want false after a failed Reload")
	}
	if got := w.Get().Items["foo"]; got != "good" {
		t.Errorf("last-good not preserved after failed Reload: foo = %q, want good", got)
	}
}

// A failing Reload with NO observer installed (the default) must not panic on the
// nil sink — it just returns the error and keeps last-good.
func TestReload_Failure_NilObserverNoPanic(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	// Intentionally no SetReloadObserver → nil sink.

	writeYAML(t, dir, "foo: boom\n")
	if err := w.Reload(); err == nil {
		t.Fatal("Reload on an unparseable file should return an error")
	}
	if got := w.Get().Items["foo"]; got != "good" {
		t.Errorf("last-good not preserved after failed Reload: foo = %q, want good", got)
	}
}

// A SUCCESSFUL Reload records ok=true (so the gauge recovers) but does NOT bump
// the failure count (guards against counting every post-write refresh).
func TestReload_Success_RecordsOKNotFailure(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	obs := &fakeReloadObserver{}
	w.SetReloadObserver(obs)

	writeYAML(t, dir, "foo: better\n")
	if err := w.Reload(); err != nil {
		t.Fatalf("Reload: %v", err)
	}
	if got := obs.failCount("test-comp"); got != 0 {
		t.Errorf("successful Reload recorded a failure: failCount = %d, want 0", got)
	}
	if ok, recorded := obs.last("test-comp"); !recorded || !ok {
		t.Errorf("successful Reload last outcome = (%v, recorded=%v), want (true, true)", ok, recorded)
	}
	if got := w.Get().Items["foo"]; got != "better" {
		t.Errorf("successful Reload did not apply: foo = %q, want better", got)
	}
}

// A file that is already BROKEN at startup fails the initial load() while no
// observer is installed yet (New runs before SetReloadObserver). Installing the
// observer must REPLAY that failure so the gauge reflects the stale state
// immediately — otherwise a no-WatchLoop manager (groups/views) that never gets a
// post-startup write sits at the assumed-current default forever, exactly the
// single-shot stale case the gauge is meant to catch (verifier finding #1).
//
// Dogfood note: dropping the replay call in SetReloadObserver makes this fail
// (obs.last returns recorded=false) — the assertion is load-bearing.
func TestSetReloadObserver_ReplaysStartupFailure(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: boom\n") // broken from the very first load

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err == nil {
		t.Fatal("New should surface the initial parse error for a broken file")
	}
	obs := &fakeReloadObserver{}
	w.SetReloadObserver(obs) // must replay the already-failed startup outcome

	if ok, recorded := obs.last("test-comp"); !recorded || ok {
		t.Errorf("startup failure not replayed: last = (%v, recorded=%v), want (false, true)",
			ok, recorded)
	}
}

// A HEALTHY manager must replay ok=true on observer install (gauge stays 1, no
// spurious Stale) and must NOT bump the failure counter — guards the replay
// against becoming a false-positive source.
func TestSetReloadObserver_ReplaysHealthyAsOK(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	obs := &fakeReloadObserver{}
	w.SetReloadObserver(obs)

	if ok, recorded := obs.last("test-comp"); !recorded || !ok {
		t.Errorf("healthy replay = (%v, recorded=%v), want (true, true)", ok, recorded)
	}
	if got := obs.failCount("test-comp"); got != 0 {
		t.Errorf("healthy replay bumped failCount to %d, want 0", got)
	}
}

// Open mode (empty path) never calls load() in New(); the replay must still see
// the default ok=true, not a spurious failure that would fire Stale for a
// perfectly healthy "no _rbac.yaml configured" manager.
func TestSetReloadObserver_OpenModeReplaysOK(t *testing.T) {
	t.Parallel()
	w, err := New("", "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New(open mode): %v", err)
	}
	obs := &fakeReloadObserver{}
	w.SetReloadObserver(obs)

	if ok, recorded := obs.last("test-comp"); !recorded || !ok {
		t.Errorf("open-mode replay = (%v, recorded=%v), want (true, true)", ok, recorded)
	}
}

// SetReloadObserver takes loadMu, so installing the observer is race-free even
// against an in-flight load() — a future main.go refactor that reloads before the
// observer is wired must not race the reloadObs write / lastOK read / replay
// (Gemini #1072 pt3). Dogfood: drop the loadMu Lock/Unlock in SetReloadObserver
// and `go test -race` flags the reloadObs (and lastOK) access here.
func TestSetReloadObserver_RaceWithConcurrentLoad(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")
	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	stop := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < 4; i++ { // hammer Reload() throughout
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
					_ = w.Reload()
				}
			}
		}()
	}
	obs := &fakeReloadObserver{}
	// (Re)install repeatedly WHILE loads run, to make the write/read overlap
	// actually occur — the race detector only flags a race that happens, so a
	// single install could slip between ticks and hide an unlocked bug.
	for i := 0; i < 2000; i++ {
		w.SetReloadObserver(obs)
	}
	close(stop)
	wg.Wait()

	// Sanity: after wiring, a subsequent reload records on the live observer.
	if err := w.Reload(); err != nil {
		t.Fatalf("Reload after wiring: %v", err)
	}
	if _, recorded := obs.last("test-comp"); !recorded {
		t.Error("observer did not record after being wired")
	}
}

// With no observer installed (the default), a failing WatchLoop reload must NOT
// panic on a nil sink — it just logs the WARN and keeps last-good.
func TestWatchLoop_ReloadFailure_NilObserverNoPanic(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	path := writeYAML(t, dir, "foo: good\n")

	w, err := New(path, "test-comp", parseErrOnBoom, emptyTestConfig)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	// Intentionally no SetReloadObserver → nil sink.

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
