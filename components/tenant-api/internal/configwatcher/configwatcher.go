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
// per-config methods (HasPermission for rbac, CheckWrite for
// policy, ListGroups for groups, etc.). Get / Reload / WatchLoop
// promote through the embed.
//
// Concurrency: reads are lock-free (atomic.Value). Writes happen
// only inside load() (single-flight via the WatchLoop ticker or the
// caller-driven Reload).
package configwatcher

import (
	"crypto/sha256"
	"fmt"
	"log/slog"
	"os"
	"sync/atomic"
	"time"
)

// ParseFunc parses raw YAML bytes into a typed config snapshot.
type ParseFunc[T any] func([]byte) (*T, error)

// EmptyFunc returns a zero-value snapshot to use when the path is
// empty (open mode), the file doesn't exist, or initial load fails.
type EmptyFunc[T any] func() *T

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

	value    atomic.Value // stores *T
	lastHash string
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
		path:  path,
		label: label,
		parse: parse,
		empty: empty,
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
func (w *Watcher[T]) Reload() error {
	w.lastHash = ""
	return w.load()
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
				slog.Warn("config reload failed", "component", w.label, "error", err)
			}
		}
	}
}

// load is the single-flight read+parse+store. Empty path or missing
// file → store empty(). SHA-256 dedup avoids re-parsing unchanged
// files on every WatchLoop tick.
func (w *Watcher[T]) load() error {
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
func (w *Watcher[T]) LastHash() string { return w.lastHash }

// Override stores cfg as the current snapshot and clears the dedup
// hash so the next disk-driven load() runs uncached. Intended for
// tests that exercise downstream logic against a synthetic
// snapshot without writing a temp YAML file. Not meaningful for
// production code (the next WatchLoop tick or Reload() will
// overwrite anything Override stored).
func (w *Watcher[T]) Override(cfg *T) {
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
