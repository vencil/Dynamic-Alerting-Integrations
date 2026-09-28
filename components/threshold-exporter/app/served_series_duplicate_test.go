package main

// #2115: `da-guard served-values` exits 2 when its private Gather of the
// collector's series fails, on the premise that the real /metrics then fails
// the whole scrape. This pins that premise against the real handler.

import (
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

func scrapeTree(t *testing.T, tenantYAML string) (int, string) {
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
	collector := NewThresholdCollector(newTestManager(cfg))
	srv := httptest.NewServer(collector.MetricsHandler())
	defer srv.Close()
	resp, err := http.Get(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, string(body)
}

func TestMetrics_TwoKeysOneSeries_FailsTheScrape(t *testing.T) {
	t.Parallel()
	// `X: "n:critical"` and `X_critical` both emit severity="critical" for X.
	code, body := scrapeTree(t, "tenants:\n  tenant-a:\n"+
		"    mysql_connections: \"70:critical\"\n    mysql_connections_critical: 95\n")
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500 (the premise of served-values' duplicate check); body:\n%s", code, body)
	}
	if !strings.Contains(body, "was collected before with the same name and label values") {
		t.Errorf("500 for another reason than a duplicate series:\n%s", body)
	}
}

func TestMetrics_RegexAndExactLabelOfTheSameName_FailsTheScrape(t *testing.T) {
	t.Parallel()
	// A regex label `q` is exported as `q_re`, so it collides with an exact
	// label spelled `q_re`.
	code, body := scrapeTree(t, "tenants:\n  tenant-a:\n"+
		"    redis_queue_length{q=~\"a\"}: 2\n    redis_queue_length{q_re=\"a\"}: 3\n")
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500; body:\n%s", code, body)
	}
}

func TestMetrics_Control_NoDuplicate_Serves(t *testing.T) {
	t.Parallel()
	code, body := scrapeTree(t, "tenants:\n  tenant-a:\n"+
		"    mysql_connections: \"70:critical\"\n    redis_queue_length{q=~\"a\"}: 2\n")
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body:\n%s", code, body)
	}
}
