package handler

import (
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/federation/orphan"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/ws"
)

// metricsGoldenPath is the byte-exact snapshot of the full /metrics body.
// Regenerate (after an INTENTIONAL exposition change) with:
//
//	UPDATE_GOLDEN=1 go test ./internal/handler/ -run TestMetricsHandler_Golden
//
// then review the golden diff like code — regenerating to mask an accidental
// spacing / float-format / ordering drift defeats the test's purpose.
var metricsGoldenPath = filepath.Join("testdata", "metrics.golden")

// uptimeLineRe pins the exact rendered shape of the one wall-clock-dependent
// line (%.1f in MetricsHandler). The VALUE is normalized before the golden
// compare, but the FORMAT stays asserted: a %.1f→%f (or unit) drift stops
// matching and fails loudly instead of being normalized away.
var uptimeLineRe = regexp.MustCompile(`(?m)^tenant_api_uptime_seconds \d+\.\d$`)

// TestMetricsHandler_Golden byte-compares the FULL /metrics exposition body
// against testdata/metrics.golden. The existing per-family tests assert via
// strings.Contains, which cannot see spacing, float-format, HELP/TYPE wording,
// or series-ordering drift; this snapshot covers every family the test can
// render deterministically.
//
// NOT parallel: /metrics reads process-global state. Every global in THIS
// package is pinned to a fixed value via Swap and restored in Cleanup; globals
// in other packages that the test cannot pin (orphan counts, forge
// circuit/conflict registries, SSE hub) are asserted to still be in their
// fresh-process default below, so their families render as the golden expects
// (orphan gauges at 0; circuit/conflict/SSE families absent — their
// presence-branches are covered by conditional tests, e.g.
// TestMetrics_HumanSocketGauge_PresenceAndValue for the analogous gauge).
func TestMetricsHandler_Golden(t *testing.T) {
	// Pin every handler-package global the exposition reads. Swap returns the
	// previous value, so pin + save is one atomic step each.
	prevReq := Metrics.requestsTotal.Swap(42)
	prevErr := Metrics.errorsTotal.Swap(7)
	prevWr := Metrics.writesTotal.Swap(3)
	prevBypass := devBypassActive.Swap(false)
	prevHSCfg := humanSocketConfigured.Swap(true)
	prevHSUp := humanSocketUp.Swap(true)
	prevLimiter := activeLimiter.Swap(nil)
	prevAudit := activeIdentityAudit.Swap(nil)
	prevScope := activeScopeWouldDeny.Swap(nil)
	prevReload := activeConfigReloadMetrics.Swap(nil)
	t.Cleanup(func() {
		Metrics.requestsTotal.Store(prevReq)
		Metrics.errorsTotal.Store(prevErr)
		Metrics.writesTotal.Store(prevWr)
		devBypassActive.Store(prevBypass)
		humanSocketConfigured.Store(prevHSCfg)
		humanSocketUp.Store(prevHSUp)
		activeLimiter.Store(prevLimiter)
		activeIdentityAudit.Store(prevAudit)
		activeScopeWouldDeny.Store(prevScope)
		activeConfigReloadMetrics.Store(prevReload)
	})

	// Globals in OTHER packages have no seam this test can pin, so require the
	// fresh-process default instead. If one of these fires, an earlier test in
	// this binary now leaks that state — isolate that test (save/restore, like
	// this one does for handler globals), don't regenerate the golden around it.
	if tok, sub := orphan.OrphanCounts(); tok != 0 || sub != 0 {
		t.Fatalf("precondition: orphan counts = (%d, %d), want (0, 0) — an earlier test left detector state behind", tok, sub)
	}
	if snap := platform.CircuitSnapshot(); len(snap) != 0 {
		t.Fatalf("precondition: forge circuit registry not empty (%v) — an earlier test built a real forge client", snap)
	}
	if snap := platform.ConflictSnapshot(); len(snap) != 0 {
		t.Fatalf("precondition: forge conflict registry not empty (%v) — an earlier test recorded a tracker sync", snap)
	}
	if _, ok := ws.ClientCountSnapshot(); ok {
		t.Fatalf("precondition: an SSE hub is installed — an earlier test constructed ws.NewHub")
	}

	w := httptest.NewRecorder()
	MetricsHandler(w, httptest.NewRequest("GET", "/metrics", nil))
	if w.Code != http.StatusOK {
		t.Fatalf("MetricsHandler() status = %d, want %d", w.Code, http.StatusOK)
	}
	if ct := w.Header().Get("Content-Type"); ct != "text/plain; version=0.0.4; charset=utf-8" {
		t.Errorf("Content-Type = %q, want the exact Prometheus 0.0.4 value", ct)
	}
	got := w.Body.String()

	// Normalize the one wall-clock value, format-checked first (see uptimeLineRe).
	if n := len(uptimeLineRe.FindAllString(got, -1)); n != 1 {
		t.Fatalf("uptime line matched %d times, want exactly 1 — %%.1f format or line drifted\nbody:\n%s", n, got)
	}
	got = uptimeLineRe.ReplaceAllString(got, "tenant_api_uptime_seconds 0.0")

	// Guard against an empty-vs-empty false green before any byte compare.
	if !strings.Contains(got, "tenant_api_up 1") {
		t.Fatalf("rendered body lost the tenant_api_up anchor — refusing to golden-compare\nbody:\n%s", got)
	}

	if os.Getenv("UPDATE_GOLDEN") != "" {
		if err := os.MkdirAll(filepath.Dir(metricsGoldenPath), 0o755); err != nil {
			t.Fatalf("mkdir testdata: %v", err)
		}
		if err := os.WriteFile(metricsGoldenPath, []byte(got), 0o644); err != nil {
			t.Fatalf("write golden: %v", err)
		}
		t.Logf("golden updated: %s (%d bytes) — review the diff before committing", metricsGoldenPath, len(got))
		return
	}

	wantBytes, err := os.ReadFile(metricsGoldenPath)
	if err != nil {
		t.Fatalf("read golden: %v (regenerate with UPDATE_GOLDEN=1)", err)
	}
	want := string(wantBytes)
	if len(want) == 0 {
		t.Fatal("golden file is empty — regenerate with UPDATE_GOLDEN=1")
	}
	if got == want {
		return
	}

	// Byte mismatch: report the first differing line so the drift is readable
	// without eyeballing two full expositions.
	gotLines, wantLines := strings.Split(got, "\n"), strings.Split(want, "\n")
	for i := 0; i < len(gotLines) || i < len(wantLines); i++ {
		var g, wl string
		if i < len(gotLines) {
			g = gotLines[i]
		}
		if i < len(wantLines) {
			wl = wantLines[i]
		}
		if g != wl {
			t.Fatalf("/metrics drifted from golden at line %d:\n  got:  %q\n  want: %q\n(%d vs %d lines total; if the change is intentional, regenerate with UPDATE_GOLDEN=1 and review the diff)",
				i+1, g, wl, len(gotLines), len(wantLines))
		}
	}
	t.Fatalf("/metrics drifted from golden (lines equal, bytes differ — likely trailing-newline or CR drift): got %d bytes, want %d bytes", len(got), len(want))
}
