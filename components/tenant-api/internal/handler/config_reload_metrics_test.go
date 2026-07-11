package handler

import (
	"io/fs"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/configwatcher"
)

// configReloadComponents must contain the label of EVERY manager that constructs
// a configwatcher.Watcher, or that manager's reload outcomes are silently DROPPED
// — RecordReload ignores an unrecognized label by design (so a stray label can
// never panic a reload goroutine), which makes the omission invisible. The set's
// doc comment says "a new manager appends its label here"; this test makes that
// mechanical instead of aspirational.
//
// NewForTest is excluded on purpose: it is test-only and path-less (WatchLoop and
// Reload are no-ops), and rbac's uses a lowercase "rbac" label.
func TestConfigReloadComponents_CoversEveryWatcherLabel(t *testing.T) {
	t.Parallel()
	// Test runs in internal/handler; ".." is internal/.
	// reCall finds EVERY construction site; reLabel finds only those whose label is
	// a string literal. If the two counts differ, some call site passes a const or
	// variable label that reLabel cannot see — the guard would be silently blind to
	// exactly the manager it is meant to protect, so that is a hard failure.
	// `(\[[^]]*\])?` also matches explicit generic instantiation
	// (`configwatcher.New[T](…)`), so a future call site written that way cannot
	// slip past the calls==found invariant below with an unchecked label.
	reCall := regexp.MustCompile(`configwatcher\.New(\[[^]]*\])?\(`)
	reLabel := regexp.MustCompile(`configwatcher\.New(\[[^]]*\])?\(\s*[A-Za-z0-9_.]+\s*,\s*"([^"]+)"`)

	known := make(map[string]bool, len(configReloadComponents))
	for _, c := range configReloadComponents {
		known[c] = true
	}

	var found []string
	calls := 0
	err := filepath.WalkDir("..", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || !strings.HasSuffix(p, ".go") || strings.HasSuffix(p, "_test.go") {
			return nil
		}
		b, readErr := os.ReadFile(p)
		if readErr != nil {
			return readErr
		}
		src := string(b)
		calls += len(reCall.FindAllString(src, -1))
		for _, m := range reLabel.FindAllStringSubmatch(src, -1) {
			label := m[2] // group 1 is the optional [TypeArgs]; group 2 is the label
			found = append(found, label)
			if !known[label] {
				t.Errorf("%s: configwatcher.New label %q is NOT in configReloadComponents — "+
					"its reload outcomes would be silently dropped by RecordReload", p, label)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walk internal/: %v", err)
	}
	if calls != len(found) {
		t.Fatalf("found %d configwatcher.New call site(s) but only %d with a string-literal label "+
			"(%v) — a non-literal label is invisible to this guard; extend reLabel or inline the literal",
			calls, len(found), found)
	}
	// Guard the guard: a regexp that stopped matching would make this vacuous.
	if len(found) < len(configReloadComponents) {
		t.Fatalf("expected >=%d configwatcher.New call sites, found %d (%v) — regexp likely stale",
			len(configReloadComponents), len(found), found)
	}
}

// The observer returned by NewConfigReloadObserver satisfies the configwatcher
// sink, drives BOTH families (failure counter + last-reload-successful gauge),
// and becomes the instance /metrics renders.
func TestConfigReloadObserver_RecordAndExposition(t *testing.T) {
	// NOT t.Parallel: mutates the package-level activeConfigReloadMetrics
	// pointer that MetricsHandler reads. Kept serial so the exposition
	// assertion below reads this test's own store.
	obs := NewConfigReloadObserver()
	obs.RecordReload("RBAC", false)              // failure: counter 1, gauge 0
	obs.RecordReload("RBAC", false)              // failure: counter 2, gauge 0
	obs.RecordReload("policy", false)            // single failure: counter 1, gauge 0
	obs.RecordReload("tenantorg", true)          // success: counter 0, gauge stays 1
	obs.RecordReload("garbage-component", false) // unknown component → no-op

	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()

	for _, want := range []string{
		// --- failure counter ---
		`# TYPE tenant_api_config_reload_failures_total counter`,
		`tenant_api_config_reload_failures_total{component="RBAC"} 2`,
		`tenant_api_config_reload_failures_total{component="policy"} 1`,
		`tenant_api_config_reload_failures_total{component="views"} 0`,
		// --- current-state gauge (default 1; a single failure flips to 0) ---
		`# TYPE tenant_api_config_last_reload_successful gauge`,
		`tenant_api_config_last_reload_successful{component="RBAC"} 0`,
		// policy: ONE failure flips the gauge to 0 even though the counter (1) is
		// below any rate threshold — this is exactly the single-shot gap the gauge
		// closes for groups/views.
		`tenant_api_config_last_reload_successful{component="policy"} 0`,
		`tenant_api_config_last_reload_successful{component="tenantorg"} 1`,
		`tenant_api_config_last_reload_successful{component="views"} 1`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("/metrics missing line:\n  %s\n--- body ---\n%s", want, body)
		}
	}
}

// A failed reload flips the gauge to 0; a LATER successful reload flips it back
// to 1 (recovery), while the monotonic counter stays at its accumulated value.
func TestConfigReloadObserver_GaugeRecovers(t *testing.T) {
	t.Parallel()
	var m ConfigReloadMetrics
	var _ configwatcher.ReloadObserver = &m
	// gauge starts at the zero value on a bare struct (0) — NewConfigReloadObserver
	// is what initializes to 1; here we exercise the transitions directly.
	m.RecordReload("groups", false)
	if m.StateSnapshot()["groups"] != 0 {
		t.Fatalf("after failure, state[groups] = %d, want 0", m.StateSnapshot()["groups"])
	}
	if m.Snapshot()["groups"] != 1 {
		t.Fatalf("after failure, counter[groups] = %d, want 1", m.Snapshot()["groups"])
	}
	m.RecordReload("groups", true) // recovery
	if m.StateSnapshot()["groups"] != 1 {
		t.Errorf("after recovery, state[groups] = %d, want 1", m.StateSnapshot()["groups"])
	}
	if m.Snapshot()["groups"] != 1 {
		t.Errorf("counter must not decrement on recovery: counter[groups] = %d, want 1",
			m.Snapshot()["groups"])
	}
}

// The store directly satisfies configwatcher.ReloadObserver; Snapshot /
// StateSnapshot cover every known component (stable metric shape).
func TestConfigReloadMetrics_Snapshots(t *testing.T) {
	t.Parallel()
	var m ConfigReloadMetrics
	m.RecordReload("policy", false)
	m.RecordReload("policy", false)
	if m.Snapshot()["policy"] != 2 {
		t.Errorf("snapshot[policy] = %d, want 2", m.Snapshot()["policy"])
	}
	for _, snap := range []map[string]int64{m.Snapshot(), m.StateSnapshot()} {
		if len(snap) != len(configReloadComponents) {
			t.Errorf("snapshot has %d keys, want %d (one per configReloadComponents)",
				len(snap), len(configReloadComponents))
		}
	}
}

// With no store ever installed, the counter renders at 0 and the gauge at 1
// (assumed-current) — a never-wired / never-reloaded deployment must not
// false-alarm the == 0 stale alert.
func TestConfigReloadMetrics_DisabledDefaults(t *testing.T) {
	activeConfigReloadMetrics.Store(nil)
	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()
	for _, want := range []string{
		`tenant_api_config_reload_failures_total{component="RBAC"} 0`,
		`tenant_api_config_last_reload_successful{component="RBAC"} 1`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("/metrics with store unwired missing:\n  %s\n--- body ---\n%s", want, body)
		}
	}
}
