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
// Its one production caller discards the returned error (`_ = mgr.Reload()` in
// handler/state.go's reloadState) and the handler answers anyway, so the
// observer is the only signal that the manager went stale until a WatchLoop
// tick fails again.
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
// immediately — otherwise the gauge sits at the assumed-current default until
// the next load (a WatchLoop tick later, or forever for a watcher run without
// one), while the config is already stale (verifier finding #1).
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

// recordingObserver keeps every recorded outcome in order.
type recordingObserver struct {
	mu  sync.Mutex
	got []bool
}

func (r *recordingObserver) RecordReload(_ string, ok bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.got = append(r.got, ok)
}

func (r *recordingObserver) outcomes() []bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return append([]bool(nil), r.got...)
}

// A panic in a reload's parse is recovered by loadLocked on every path — the
// synchronous Reload, a WatchLoop tick's load and a ReloadAsync goroutine —
// and recorded ONCE, as a failure (not a success and then a correction). The
// last-good snapshot stays, loadMu is released (a later Reload returns) and
// ReloadAsync's pending flag is clear again.
func TestReload_PanicIsRecoveredOnEveryPath(t *testing.T) {
	paths := map[string]func(w *Watcher[testConfig]){
		"Reload":         func(w *Watcher[testConfig]) { _ = w.Reload() },
		"WatchLoop load": func(w *Watcher[testConfig]) { _ = w.load() },
		"ReloadAsync":    func(w *Watcher[testConfig]) { w.ReloadAsync() },
	}
	for name, trigger := range paths {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			var mu sync.Mutex
			payload := []byte("foo: good\n")
			read := func() ([]byte, bool, error) {
				mu.Lock()
				defer mu.Unlock()
				return payload, true, nil
			}
			set := func(b string) {
				mu.Lock()
				payload = []byte(b)
				mu.Unlock()
			}
			parse := func(data []byte) (*testConfig, error) {
				if strings.Contains(string(data), "panic") {
					panic("synthetic parse panic")
				}
				return parseTestConfig(data)
			}
			w, err := NewWithReader("test source", "panicky", read, parse, emptyTestConfig)
			if err != nil {
				t.Fatal(err)
			}
			good := w.Get()
			obs := &recordingObserver{}
			w.SetReloadObserver(obs) // replays the first load: [true]

			set("foo: panic\n")
			trigger(w)

			deadline := time.Now().Add(2 * time.Second)
			for len(obs.outcomes()) < 2 {
				if time.Now().After(deadline) {
					t.Fatalf("the panicking reload recorded nothing (outcomes %v)", obs.outcomes())
				}
				time.Sleep(5 * time.Millisecond)
			}
			time.Sleep(50 * time.Millisecond) // a stray second record would land here
			if got := obs.outcomes(); len(got) != 2 || got[1] {
				t.Fatalf("outcomes = %v, want [true false]: one failure, recorded once", got)
			}
			if w.Get() != good {
				t.Fatal("the snapshot changed; want the last-good one kept")
			}
			if w.asyncPending.Load() {
				t.Fatal("asyncPending still set after the reload finished")
			}

			// loadMu was released: a later Reload runs, and picks up a fixed YAML.
			set("foo: fixed\n")
			done := make(chan error, 1)
			go func() { done <- w.Reload() }()
			select {
			case err := <-done:
				if err != nil {
					t.Fatalf("Reload after the panic: %v", err)
				}
			case <-time.After(2 * time.Second):
				t.Fatal("Reload after the panic did not return: loadMu is still held")
			}
			if got := w.Get().Items["foo"]; got != "fixed" {
				t.Fatalf("foo = %q after the fixing Reload, want fixed", got)
			}
		})
	}
}
