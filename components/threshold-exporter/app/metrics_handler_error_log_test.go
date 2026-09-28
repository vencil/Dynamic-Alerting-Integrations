package main

// #2032: a /metrics Gather failure (duplicate series, shape as in
// served_series_duplicate_test.go) used to answer 500 with nothing in any
// log and no counter. MetricsHandler now wires promhttp's ErrorLog to the
// manager's logger seam and its Registry to the exporter's registry.
//
// Seams: logger via SetLogger (per-test buffer); no globals, so parallel.

import (
	"bytes"
	"io"
	"log"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"regexp"
	"strconv"
	"sync"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

const (
	gatherErrAnchor   = "error gathering metrics"
	duplicateTenant   = "tenants:\n  tenant-a:\n    mysql_connections: \"70:critical\"\n    mysql_connections_critical: 95\n"
	noDuplicateTenant = "tenants:\n  tenant-a:\n    mysql_connections: \"70:critical\"\n"
)

// syncBuffer is a bytes.Buffer safe for the handler goroutine to write while
// the test reads it.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func loadTenantTree(t *testing.T, tenantYAML string) *ThresholdConfig {
	t.Helper()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"conf.d/tenant-a.yaml":  tenantYAML,
	})
	cfg, _, err := config.LoadDir(filepath.Join(tmp, "conf.d"), nil)
	if err != nil {
		t.Fatal(err)
	}
	return cfg
}

func scrapeOnce(t *testing.T, url string) (int, string) {
	t.Helper()
	resp, err := http.Get(url)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
}

var gatheringErrLine = regexp.MustCompile(`(?m)^promhttp_metric_handler_errors_total\{cause="gathering"\} (\S+)$`)

func TestMetricsHandler_GatherFailure_IsLoggedToTheManagerLogger(t *testing.T) {
	t.Parallel()
	mgr := newTestManager(loadTenantTree(t, duplicateTenant))
	var logs syncBuffer
	mgr.SetLogger(log.New(&logs, "", 0))
	srv := httptest.NewServer(NewThresholdCollector(mgr).MetricsHandler())
	defer srv.Close()

	code, body := scrapeOnce(t, srv.URL)
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500; body:\n%s", code, body)
	}
	// The line must name the cause, so an operator reading the 500 knows
	// which series collided.
	assertLogLineWith(t, logs.String(), gatherErrAnchor,
		"was collected before with the same name and label values", `value:"tenant-a"`)
}

// The handler is built once at startup; the logger must be looked up per
// failure, so a SetLogger after MetricsHandler() still takes effect.
func TestMetricsHandler_GatherFailure_UsesTheLoggerSetAfterConstruction(t *testing.T) {
	t.Parallel()
	mgr := newTestManager(loadTenantTree(t, duplicateTenant))
	var before, after syncBuffer
	mgr.SetLogger(log.New(&before, "", 0))
	srv := httptest.NewServer(NewThresholdCollector(mgr).MetricsHandler())
	defer srv.Close()
	mgr.SetLogger(log.New(&after, "", 0))

	if code, body := scrapeOnce(t, srv.URL); code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500; body:\n%s", code, body)
	}
	if n := len(logLinesWith(after.String(), gatherErrAnchor)); n != 1 {
		t.Errorf("logger set after construction got %d %q lines, want 1; log:\n%s", n, gatherErrAnchor, after.String())
	}
	if before.String() != "" {
		t.Errorf("logger replaced before the scrape must stay empty; got:\n%s", before.String())
	}
}

func TestMetricsHandler_Control_HealthyScrape_LogsNothing(t *testing.T) {
	t.Parallel()
	mgr := newTestManager(loadTenantTree(t, noDuplicateTenant))
	var logs syncBuffer
	mgr.SetLogger(log.New(&logs, "", 0))
	srv := httptest.NewServer(NewThresholdCollector(mgr).MetricsHandler())
	defer srv.Close()

	code, body := scrapeOnce(t, srv.URL)
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body:\n%s", code, body)
	}
	if logs.String() != "" {
		t.Errorf("a healthy scrape must log nothing; got:\n%s", logs.String())
	}
	m := gatheringErrLine.FindStringSubmatch(body)
	if m == nil || m[1] != "0" {
		t.Errorf("want promhttp_metric_handler_errors_total{cause=\"gathering\"} 0 on a clean handler; got %v", m)
	}
}

// The counter is only visible on the first successful scrape after a failed
// one (a failure fails the whole body), so the test fails, recovers, and
// reads it off the same handler instance.
func TestMetricsHandler_GatherFailure_CountedAndVisibleAfterRecovery(t *testing.T) {
	t.Parallel()
	mgr := newTestManager(loadTenantTree(t, duplicateTenant))
	mgr.SetLogger(log.New(io.Discard, "", 0))
	srv := httptest.NewServer(NewThresholdCollector(mgr).MetricsHandler())
	defer srv.Close()

	if code, body := scrapeOnce(t, srv.URL); code != http.StatusInternalServerError {
		t.Fatalf("broken config: status = %d, want 500; body:\n%s", code, body)
	}

	// Swapping only m.config is a deliberate shortcut for "recovered": the
	// collector reads nothing but GetConfig(). If it ever reads more of the
	// manager's state, recover through the real load path instead.
	good := loadTenantTree(t, noDuplicateTenant)
	mgr.mu.Lock()
	mgr.config = good
	mgr.mu.Unlock()

	code, body := scrapeOnce(t, srv.URL)
	if code != http.StatusOK {
		t.Fatalf("recovered config: status = %d, want 200; body:\n%s", code, body)
	}
	m := gatheringErrLine.FindStringSubmatch(body)
	if m == nil {
		t.Fatalf("no promhttp_metric_handler_errors_total{cause=\"gathering\"} series in /metrics:\n%s", body)
	}
	v, err := strconv.ParseFloat(m[1], 64)
	if err != nil || v != 1 {
		t.Errorf("gathering error count = %q, want 1 (one failed scrape)", m[1])
	}
}
