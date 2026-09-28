package main

// #2115: `da-guard served-values` refuses a tree where two rows share one
// user_threshold series, on the premise that the real /metrics then fails the
// whole scrape. This pins that premise against the real collector, and that
// the identity served-values uses (config.ResolvedThreshold.SeriesLabels) is
// the one that collides.

import (
	"io"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

func scrapeTree(t *testing.T, tenantYAML string) (int, string, *config.ThresholdConfig) {
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
	return resp.StatusCode, string(body), cfg
}

func TestMetrics_TwoKeysOneSeries_FailsTheScrape(t *testing.T) {
	t.Parallel()
	// `X: "n:critical"` and `X_critical` both emit severity="critical" for X.
	code, body, cfg := scrapeTree(t, "tenants:\n  tenant-a:\n"+
		"    mysql_connections: \"70:critical\"\n    mysql_connections_critical: 95\n")
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500 (the premise of served-values' duplicate check); body:\n%s", code, body)
	}
	if !strings.Contains(body, "was collected before with the same name and label values") {
		t.Errorf("500 for another reason than a duplicate series:\n%s", body)
	}
	// The identity served-values checks is the one that collides here.
	keyed, _, err := cfg.ResolveAtWithKeys(time.Now())
	if err != nil {
		t.Fatal(err)
	}
	seen := map[string]string{}
	var dup []string
	for _, k := range keyed {
		names, values := k.SeriesLabels()
		pairs := make([]string, len(names))
		for i := range names {
			pairs[i] = names[i] + "=" + values[i]
		}
		sort.Strings(pairs)
		id := strings.Join(pairs, ",")
		if first, ok := seen[id]; ok {
			dup = append(dup, first, k.Key)
		}
		seen[id] = k.Key
	}
	sort.Strings(dup)
	if strings.Join(dup, ",") != "mysql_connections,mysql_connections_critical" {
		t.Errorf("SeriesLabels collisions = %v, want the two keys", dup)
	}
}

func TestMetrics_RegexAndExactLabelOfTheSameName_FailsTheScrape(t *testing.T) {
	t.Parallel()
	// A regex label `q` is exported as `q_re`, so it collides with an exact
	// label spelled `q_re`.
	code, body, _ := scrapeTree(t, "tenants:\n  tenant-a:\n"+
		"    redis_queue_length{q=~\"a\"}: 2\n    redis_queue_length{q_re=\"a\"}: 3\n")
	if code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500; body:\n%s", code, body)
	}
}

func TestMetrics_Control_NoDuplicate_Serves(t *testing.T) {
	t.Parallel()
	code, body, _ := scrapeTree(t, "tenants:\n  tenant-a:\n"+
		"    mysql_connections: \"70:critical\"\n    redis_queue_length{q=~\"a\"}: 2\n")
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body:\n%s", code, body)
	}
}
