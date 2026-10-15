package handler

import (
	"bytes"
	"context"
	"net"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/testutil"
)

// NOT t.Parallel: installs the package-level generator source /metrics
// reads (the golden test renders it unset). #2903: tenant_api_policy_available
// is the generator container's `ready`, read at scrape time; unreachable is 0.
func TestPolicyAvailabilityMetrics(t *testing.T) {
	t.Cleanup(func() { SetGeneratorSource(nil) })
	render := func() string {
		var b bytes.Buffer
		writePolicyAvailabilityMetrics(context.Background(), &b)
		return b.String()
	}
	if got := render(); !strings.Contains(got, "\ntenant_api_policy_available 0\n") {
		t.Fatalf("no generator installed: %s", got)
	}
	if strings.Contains(render(), "unavailable_open") {
		t.Error("tenant_api_policy_unavailable_open_total is still rendered (#2903 removes it)")
	}
	fake := testutil.StartFakeGenerator(t)
	SetGeneratorSource(generator.NewGate(generator.NewClient(fake.Socket), ""))
	if got := render(); !strings.Contains(got, "\ntenant_api_policy_available 1\n") {
		t.Errorf("ready generator: %s", got)
	}
	for name, body := range map[string]map[string]any{
		"policy unusable": {"policy": map[string]any{"configured": true, "available": false}, "amtool_available": true, "ready": false},
		"amtool missing":  {"policy": map[string]any{"configured": false, "available": true}, "amtool_available": false, "ready": false},
	} {
		fake.SetStatus(func() (int, any) { return 200, body })
		if got := render(); !strings.Contains(got, "\ntenant_api_policy_available 0\n") {
			t.Errorf("%s: %s", name, got)
		}
	}
	fake.SetStatus(func() (int, any) { return 500, map[string]any{} })
	if got := render(); !strings.Contains(got, "\ntenant_api_policy_available 0\n") {
		t.Errorf("status 500: %s", got)
	}
	SetGeneratorSource(generator.NewGate(generator.NewClient(filepath.Join(t.TempDir(), "none.sock")), ""))
	if got := render(); !strings.Contains(got, "\ntenant_api_policy_available 0\n") {
		t.Errorf("unreachable generator: %s", got)
	}
}

// NOT t.Parallel (the generator source, as above). #2903 G7: the
// container's own /metrics is appended after tenant-api's, with
// tenant_api_generator_scrape_success saying whether it was; the rejection
// counter renders every reason.
func TestGeneratorMetrics_AppendedAndScrapeSuccess(t *testing.T) {
	t.Cleanup(func() { SetGeneratorSource(nil) })
	render := func() string {
		var b bytes.Buffer
		writeGeneratorMetrics(context.Background(), &b)
		return b.String()
	}
	got := render()
	if !strings.Contains(got, "\ntenant_api_generator_scrape_success 0\n") || strings.Contains(got, "\nda_generator_") {
		t.Fatalf("no generator installed: %s", got)
	}
	for _, reason := range generator.RejectionReasons {
		if !strings.Contains(got, `tenant_api_generator_rejections_total{reason="`+reason+`"} 0`) {
			t.Errorf("reason %s not rendered: %s", reason, got)
		}
	}
	fake := testutil.StartFakeGenerator(t)
	fake.SetMetrics(func() (int, string) {
		return 200, "# TYPE da_generator_judgement_peak_rss_bytes gauge\nda_generator_judgement_peak_rss_bytes 123"
	})
	gate := generator.NewGate(generator.NewClient(fake.Socket), "")
	SetGeneratorSource(gate)
	got = render()
	if !strings.Contains(got, "\ntenant_api_generator_scrape_success 1\n") ||
		!strings.HasSuffix(got, "\nda_generator_judgement_peak_rss_bytes 123\n") {
		t.Errorf("reachable generator: %s", got)
	}
	fake.SetMetrics(func() (int, string) { return 500, "boom" })
	got = render()
	if !strings.Contains(got, "\ntenant_api_generator_scrape_success 0\n") || strings.Contains(got, "boom") {
		t.Errorf("generator /metrics 500: %s", got)
	}
	// A refusal is counted by reason.
	fake.SetValidate(func(int, testutil.FakeValidateRequest) (int, any) { return 200, testutil.RejectedVerdict("busy") })
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	if err := gate.ValidateWrites(ctx, strings.Repeat("a", 40), []gitops.ProposedFile{{Path: "t.yaml", Content: []byte("x")}}); err == nil {
		t.Fatal("busy: no error")
	}
	if got := render(); !strings.Contains(got, `tenant_api_generator_rejections_total{reason="busy"} 1`) {
		t.Errorf("busy not counted: %s", got)
	}
}

// NOT t.Parallel (the generator source, as above). A generator container
// that accepts the connection and never answers must not hold /metrics:
// both reads are bounded by generator.ScrapeTimeout, and the scrape answers
// with tenant_api_policy_available 0 and tenant_api_generator_scrape_success 0.
func TestMetrics_HangingGeneratorIsBoundedByScrapeTimeout(t *testing.T) {
	t.Cleanup(func() { SetGeneratorSource(nil) })
	sock := filepath.Join(t.TempDir(), "hang.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Skipf("unix socket: %v", err)
	}
	var (
		mu    sync.Mutex
		conns []net.Conn
	)
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			mu.Lock()
			conns = append(conns, c) // held open, never read or answered
			mu.Unlock()
		}
	}()
	t.Cleanup(func() {
		_ = ln.Close()
		mu.Lock()
		defer mu.Unlock()
		for _, c := range conns {
			_ = c.Close()
		}
	})
	SetGeneratorSource(generator.NewGate(generator.NewClient(sock), ""))

	done := make(chan string, 1)
	start := time.Now()
	go func() {
		rec := httptest.NewRecorder()
		MetricsHandler(rec, httptest.NewRequest("GET", "/metrics", nil))
		done <- rec.Body.String()
	}()
	// Two reads, each within ScrapeTimeout; one more as slack.
	bound := 3 * generator.ScrapeTimeout
	select {
	case got := <-done:
		if !strings.Contains(got, "\ntenant_api_policy_available 0\n") ||
			!strings.Contains(got, "\ntenant_api_generator_scrape_success 0\n") {
			t.Errorf("hanging generator after %v:\n%s", time.Since(start), got)
		}
	case <-time.After(bound):
		t.Fatalf("/metrics did not answer within %v with a generator that never responds", bound)
	}
}
