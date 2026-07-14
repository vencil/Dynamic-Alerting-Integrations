// Package configwatcher implements the shared hot-reload-from-disk
// pattern used by all tenant-api YAML config managers (rbac /
// policy / groups / views).
//
// Pre-PR-8 each of those four packages had its own Manager type with
// near-identical scaffolding — atomic.Value cache, SHA-256-keyed
// dedup of disk reads, missing-file → empty-config fallback,
// WatchLoop for periodic re-reads (rbac / policy) or Reload for
// post-write refresh (groups / views). The boilerplate was ~80 LoC
// per package, shipping the same six-step load function four times.
//
// `Watcher[T]` is the generic core. Each domain-specific Manager
// embeds `*Watcher[ConfigType]` and contributes only its own
// per-config methods (Allowed for rbac, CheckWrite for
// policy, ListGroups for groups, etc.). Get / Reload / WatchLoop
// promote through the embed.
//
// Concurrency: reads are lock-free (atomic.Value). Writes are
// serialised by loadMu — load() can be entered concurrently by the
// WatchLoop ticker and by a caller-driven Reload (federation-policy
// has both), and the read+hash+store sequence must be atomic as a
// whole, not merely race-free: two loads that read different file
// generations outside the lock would let the older one win the store.
package configwatcher

import (
	"crypto/sha256"
	"fmt"
	"log/slog"
	"os"
	"sync"
	"sync/atomic"
	"time"
)

// ParseFunc parses raw YAML bytes into a typed config snapshot.
type ParseFunc[T any] func([]byte) (*T, error)

// EmptyFunc returns a zero-value snapshot to use when the path is
// empty (open mode), the file doesn't exist, or initial load fails.
type EmptyFunc[T any] func() *T

// ReloadObserver is an optional sink for the outcome of EVERY reload attempt.
// It is declared here (configwatcher is a leaf package importing only stdlib)
// and implemented in the handler package, which owns /metrics exposition —
// mirroring how rbac declares ScopeAuditRecorder and handler implements it
// (import direction handler → configwatcher; configwatcher never imports
// handler).
//
// Why it exists: a reload FAILURE keeps serving the LAST-GOOD snapshot (load()
// returns the error without Store-ing), so a config an admin edited with a typo
// silently stops taking effect. BOTH reload paths are silent:
//   - WatchLoop (periodic tick) logs a WARN and moves on.
//   - Reload (post-write refresh) returns the error, but every production
//     caller discards it — `_ = mgr.Reload()` in handler/group.go, view.go and
//     federation/policy.go — and then answers 200 OK, so the caller never
//     learns the snapshot went stale.
//
// The observer records the outcome (ok) of each load so the handler can drive
// TWO complementary metrics from one observation point:
//   - a monotonic failure COUNTER (increment when !ok) — reload failure RATE,
//     for dashboards/trend (Gemini #1056 disposition 3a).
//   - a per-component STATE gauge (last-reload-successful) — answers "is this
//     manager CURRENTLY serving stale config", which the counter structurally
//     cannot: a single-shot Reload failure (groups/views never retry) leaves
//     the counter at 1, below any rate threshold, yet the config IS stale.
type ReloadObserver interface {
	// RecordReload reports the outcome of one reload attempt (a WatchLoop tick
	// or a post-write Reload) for the named component (the Watcher's label,
	// e.g. "RBAC" / "policy" / "groups"). ok=false means load() returned an
	// error and kept the last-good snapshot.
	RecordReload(component string, ok bool)
}

// Watcher caches a parsed YAML config plus its SHA-256, reloads on
// demand or via a periodic ticker, and serves snapshots lock-free.
//
// Path semantics:
//   - "" (empty path) — open mode. Get returns empty(), WatchLoop
//     is a no-op, load is a no-op. rbac uses this for "no
//     _rbac.yaml configured".
//   - non-empty + file missing — empty config stored, no error.
//   - non-empty + file present + parse error — returned to caller.
type Watcher[T any] struct {
	path  string
	label string // log tag, e.g. "rbac" / "policy" / "groups"
	parse ParseFunc[T]
	empty EmptyFunc[T]

	value atomic.Value // stores *T

	// loadMu guards lastHash and serialises the whole read+parse+store
	// sequence in load(). Get() does not take it — reads stay lock-free
	// off atomic.Value — so a slow (FUSE-stalled) ReadFile under this
	// lock delays only other reloads, never request-path reads.
	loadMu   sync.Mutex
	lastHash string

	// lastOK is the outcome (err == nil) of the most recent load(), captured
	// unconditionally (even before an observer is installed). SetReloadObserver
	// replays it so a startup parse failure — which happens while reloadObs is
	// still nil — is reflected on the gauge the moment the observer is wired,
	// instead of the gauge sitting at its assumed-current default forever for a
	// no-WatchLoop manager that may never reload again. Defaults true (open mode
	// / pre-load are healthy).
	//
	// Concurrency: both the WRITE (loadLocked's defer) and the READ
	// (SetReloadObserver's replay) hold loadMu, so lastOK is fully serialised with
	// lastHash and with itself — no race and no dependence on startup ordering.
	lastOK bool

	// reloadObs is the optional reload-outcome metric sink (instance-method DI,
	// mirroring rbac.Manager.scopeAudit). nil (the default) means no recording —
	// WatchLoop still logs the WARN and keeps last-good, it just emits no metric.
	// Installed once at startup via SetReloadObserver, BEFORE WatchLoop is
	// launched AND before the server serves (handlers reach it via Reload), then
	// read-only; the goroutine-start / write-before-serve provides the
	// happens-before edge so no atomic is needed (matches SetScopeAuditor).
	reloadObs ReloadObserver
}

// New constructs a Watcher and runs an initial load. The initial
// load result is returned so callers can decide whether to fail
// fast (rbac.NewManager: fatal) or log-and-continue (others).
//
// path: full filesystem path. Empty = open mode.
// label: log line prefix (e.g. "rbac", "policy"). Used as
//
//	"<label>: loaded ... from <path>" / "WARN: <label> reload failed: %v".
//
// parse: bytes → *T. Caller can normalise nil maps inside (matches
//
//	what each previous load() did).
//
// empty: the zero-value snapshot. Stored on missing-file and on
//
//	initial-load-failure paths so Get is never nil.
func New[T any](path, label string, parse ParseFunc[T], empty EmptyFunc[T]) (*Watcher[T], error) {
	w := &Watcher[T]{
		path:   path,
		label:  label,
		parse:  parse,
		empty:  empty,
		lastOK: true, // healthy until a load() proves otherwise (open mode stays true)
	}
	// Always start with empty so Get is non-nil even before load().
	w.value.Store(empty())
	if path == "" {
		slog.Info("config: no path provided, running with empty config", "component", label)
		return w, nil
	}
	if err := w.load(); err != nil {
		return w, err
	}
	return w, nil
}

// Get returns the current config snapshot. Lock-free; never nil.
func (w *Watcher[T]) Get() *T {
	if v := w.value.Load(); v != nil {
		if t, ok := v.(*T); ok {
			return t
		}
	}
	return w.empty()
}

// Reload forces a re-read on the next call (clears the dedup hash).
// Used after writes to pick up the just-written file before the
// WatchLoop ticker fires.
//
// The outcome is recorded on the reload observer (by load()) as well as
// returned: every production caller discards the error (`_ = mgr.Reload()` —
// handler/group.go, handler/view.go, handler/federation/policy.go) and still
// answers 200 OK, so without the metric a post-write reload failure is just as
// silent as a WatchLoop tick failure and leaves the manager serving the stale
// snapshot.
func (w *Watcher[T]) Reload() error {
	w.loadMu.Lock()
	defer w.loadMu.Unlock()
	w.lastHash = ""
	// loadLocked (not load) — we already hold loadMu; load() would re-lock and
	// self-deadlock. loadLocked's defer records the outcome (lastOK + observer)
	// under the lock (a cheap atomic update in config_reload_metrics.go, no
	// lock-ordering risk), so Reload's post-write failures are observed too.
	return w.loadLocked()
}

// SetReloadObserver installs the optional reload-outcome metric sink. In
// practice main.go calls it once at startup before `go …WatchLoop(…)` and long
// before ListenAndServe, but correctness no longer DEPENDS on that ordering: the
// body takes loadMu, so the reloadObs write, the lastOK read, and the replay are
// atomic with respect to a concurrent loadLocked (which also holds loadMu). A
// future refactor that reloads earlier therefore cannot race the replay or read a
// torn lastOK (Gemini #1072 review pt3 — codify the assumption instead of relying
// on temporal coupling). Passing nil leaves recording disabled (the reload still
// logs the WARN and keeps last-good). Mirrors rbac.Manager.SetScopeAuditor.
// Promoted through the embed so every domain Manager exposes it.
//
// On install it REPLAYS the initial-load outcome (w.lastOK): the initial load in
// New() ran while reloadObs was nil, so without this replay a file that was
// already broken at startup would leave the gauge at its assumed-current default
// until the next reload — which, for a no-WatchLoop manager (groups/views) that
// gets no write, may never come. Healthy and open-mode managers replay ok=true
// (gauge stays 1), so there is no false positive. The replay goes through
// RecordReload, so a replayed startup FAILURE also increments the failure counter
// once — a real load failure counted once; it stays below the
// TenantApiConfigReloadFailing rate threshold, and the == 0 gauge is what fires.
func (w *Watcher[T]) SetReloadObserver(o ReloadObserver) {
	// loadMu makes {reloadObs write, lastOK read, replay} atomic vs a concurrent
	// loadLocked — no reliance on startup ordering. No self-deadlock: this path
	// never calls load()/loadLocked, and RecordReload is a lock-free atomic update.
	w.loadMu.Lock()
	defer w.loadMu.Unlock()
	w.reloadObs = o
	if o != nil {
		o.RecordReload(w.label, w.lastOK)
	}
}

// WatchLoop polls the file every `interval` and stores any parsed
// changes. No-op for empty path. Stops when stopCh is closed.
func (w *Watcher[T]) WatchLoop(interval time.Duration, stopCh <-chan struct{}) {
	if w.path == "" {
		return
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-stopCh:
			return
		case <-ticker.C:
			if err := w.load(); err != nil {
				// Reload failed → load() did NOT Store, so we keep serving the
				// last-good snapshot; the edited (broken) config is silently not
				// in effect. load() already records the outcome on the observer
				// (failure counter + last-reload-successful gauge); the WARN log
				// is for the operator (Gemini #1056 disposition 3a).
				slog.Warn("config reload failed", "component", w.label, "error", err)
			}
		}
	}
}

// load takes loadMu and performs one read+parse+store cycle.
func (w *Watcher[T]) load() error {
	w.loadMu.Lock()
	defer w.loadMu.Unlock()
	return w.loadLocked()
}

// loadLocked is the read+parse+store cycle. Caller must hold loadMu:
// the whole sequence is serialised, not just the lastHash write, so a
// load that reads an older file generation can never overwrite the
// snapshot stored by a concurrent load that read a newer one.
//
// Empty path or missing file → store empty(). A missing file is NOT
// an error: for rbac that empty snapshot is what makes a deleted
// _rbac.yaml fail closed (ADR-027 MED-8, see rbac.failClosedOnEmpty),
// and for every manager it is how a GitOps `git rm` of the config
// takes effect. Do not "harden" this into an error — see
// config_reload_test.go TestLoad_DeletedFile (internal/rbac).
//
// SHA-256 dedup avoids re-parsing unchanged files on every WatchLoop
// tick. On a parse failure lastHash is left untouched (holding the
// last GOOD hash), so the next tick re-reads the still-broken file
// and fails again rather than silently deduping the failure away —
// which is what lets the reload-failure counter keep climbing while
// the snapshot stays stale.
//
// The outcome (err == nil) is captured at EVERY exit via a named-return + defer,
// so success paths (dedup no-op, missing-file, parse OK) and failure paths are all
// covered from ONE point (this is the single funnel every caller reaches: New→load,
// WatchLoop→load, Reload→loadLocked), keeping the failure counter and the
// last-reload-successful gauge consistent. The defer ALWAYS records w.lastOK — held
// under loadMu, so its write is serialised with lastHash — and forwards to the
// observer when one is present. During New()'s initial load reloadObs is nil (it is
// installed post-construction), so that load emits no live metric — but its outcome
// is preserved in lastOK and REPLAYED by SetReloadObserver (matters for
// groups/views: no WatchLoop, may never reload again).
func (w *Watcher[T]) loadLocked() (err error) {
	defer func() {
		w.lastOK = err == nil
		if w.reloadObs != nil {
			w.reloadObs.RecordReload(w.label, err == nil)
		}
	}()
	if w.path == "" {
		w.value.Store(w.empty())
		return nil
	}
	data, err := os.ReadFile(w.path)
	if err != nil {
		if os.IsNotExist(err) {
			w.value.Store(w.empty())
			return nil
		}
		return fmt.Errorf("read %s: %w", w.path, err)
	}

	hash := fmt.Sprintf("%x", sha256.Sum256(data))
	if hash == w.lastHash {
		return nil
	}

	cfg, err := w.parse(data)
	if err != nil {
		return fmt.Errorf("parse %s: %w", w.path, err)
	}
	w.value.Store(cfg)
	w.lastHash = hash
	slog.Info("config loaded", "component", w.label, "path", w.path)
	return nil
}

// Path returns the configured file path (for tests / diagnostics).
func (w *Watcher[T]) Path() string { return w.path }

// LastHash returns the SHA-256 of the most recently loaded file
// content (empty string before any successful load). Intended for
// observability and tests verifying the dedup-on-reload contract;
// not part of the production hot-path.
func (w *Watcher[T]) LastHash() string {
	w.loadMu.Lock()
	defer w.loadMu.Unlock()
	return w.lastHash
}

// Override stores cfg as the current snapshot and clears the dedup
// hash so the next disk-driven load() runs uncached. Intended for
// tests that exercise downstream logic against a synthetic
// snapshot without writing a temp YAML file. Not meaningful for
// production code (the next WatchLoop tick or Reload() will
// overwrite anything Override stored).
func (w *Watcher[T]) Override(cfg *T) {
	w.loadMu.Lock()
	defer w.loadMu.Unlock()
	w.lastHash = ""
	w.value.Store(cfg)
}

// NewForTest constructs a Watcher pre-populated with `cfg`. No file
// path is configured, so WatchLoop is a no-op, Reload is a no-op
// (just clears the hash), and Get returns cfg directly. Intended
// for unit tests that exercise permission / lookup logic against
// an in-memory config without disk I/O.
//
// Caller is responsible for keeping `cfg` alive for the lifetime
// of the returned Watcher; the snapshot is stored by pointer.
func NewForTest[T any](label string, cfg *T) *Watcher[T] {
	w := &Watcher[T]{
		path:  "",
		label: label,
		// empty is a reasonable fallback even though it's unreachable
		// for path="" — Override / Get always have a value stored.
		empty: func() *T { return cfg },
	}
	w.value.Store(cfg)
	return w
}
