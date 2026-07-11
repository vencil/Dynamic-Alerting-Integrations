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
// a configwatcher.Watcher, or that manager's reload failures are silently DROPPED
// — IncReloadFailure ignores an unrecognized label by design (so a stray label can
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
					"its reload failures would be silently dropped by IncReloadFailure", p, label)
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

// The recorder returned by NewConfigReloadFailureRecorder satisfies the
// configwatcher sink, counts per component, and becomes the instance /metrics
// renders.
func TestConfigReloadFailureRecorder_IncAndExposition(t *testing.T) {
	// NOT t.Parallel: mutates the package-level activeConfigReloadFailure
	// pointer that MetricsHandler reads. Kept serial so the exposition
	// assertion below reads this test's own store.
	rec := NewConfigReloadFailureRecorder()
	rec.IncReloadFailure("RBAC")
	rec.IncReloadFailure("RBAC")
	rec.IncReloadFailure("policy")
	rec.IncReloadFailure("garbage-component-ignored") // unknown component → no-op

	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()

	for _, want := range []string{
		`tenant_api_config_reload_failures_total{component="RBAC"} 2`,
		`tenant_api_config_reload_failures_total{component="policy"} 1`,
		// The never-incremented components still emit at 0 (stable metric shape).
		`tenant_api_config_reload_failures_total{component="tenantorg"} 0`,
		`tenant_api_config_reload_failures_total{component="federation-policy"} 0`,
		`tenant_api_config_reload_failures_total{component="groups"} 0`,
		`tenant_api_config_reload_failures_total{component="views"} 0`,
		`# TYPE tenant_api_config_reload_failures_total counter`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("/metrics missing line:\n  %s\n--- body ---\n%s", want, body)
		}
	}
}

// The store type directly satisfies configwatcher.ReloadFailureRecorder and
// Snapshot reflects IncReloadFailure — the configwatcher seam test relies on
// this contract.
func TestConfigReloadFailureMetrics_Snapshot(t *testing.T) {
	t.Parallel()
	var m ConfigReloadFailureMetrics
	var _ configwatcher.ReloadFailureRecorder = &m
	m.IncReloadFailure("policy")
	m.IncReloadFailure("policy")
	snap := m.Snapshot()
	if snap["policy"] != 2 {
		t.Errorf("snapshot[policy] = %d, want 2", snap["policy"])
	}
	// Every known component is present even when zero (stable metric shape).
	if len(snap) != len(configReloadComponents) {
		t.Errorf("snapshot has %d keys, want %d (one per configReloadComponents)",
			len(snap), len(configReloadComponents))
	}
}

// With no recorder ever installed, the series still renders at 0 (stable shape).
// Runs serial and resets the pointer to nil to model the never-wired default.
func TestConfigReloadFailureMetrics_DisabledRendersZero(t *testing.T) {
	activeConfigReloadFailure.Store(nil)
	req := httptest.NewRequest("GET", "/metrics", nil)
	w := httptest.NewRecorder()
	MetricsHandler(w, req)
	body := w.Body.String()
	if !strings.Contains(body, `tenant_api_config_reload_failures_total{component="RBAC"} 0`) {
		t.Errorf("/metrics with recorder unwired should still emit RBAC=0; body:\n%s", body)
	}
}
