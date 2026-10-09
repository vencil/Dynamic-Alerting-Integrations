package main

// #2115: `da-guard served-values` exits 2 when its private Gather of the
// collector's series fails, on the premise that the real /metrics then fails
// the whole scrape. This pins that premise against the real handler, and
// (#2031) which shapes no longer reach it.

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
	return scrapeTreeWith(t, "defaults:\n  mysql_connections: 80\n", tenantYAML)
}

func scrapeTreeWith(t *testing.T, defaultsYAML, tenantYAML string) (int, string) {
	t.Helper()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": defaultsYAML,
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

// #2031: two expired overrides whose da_config_event reasons render alike
// (`container_cpu` with reason `a: b`, `container_cpu: a` with reason `b`)
// used to be one series and failed the scrape; the metric_key label tells
// them apart.
func TestMetrics_ConfigEventSameReasonText_TwoSeries(t *testing.T) {
	t.Parallel()
	code, body := scrapeTreeWith(t,
		"defaults:\n  mysql_connections: 80\n  container_cpu: 75\n  \"container_cpu: a\": 50\n",
		"tenants:\n  tenant-a:\n    container_cpu:\n      default: \"95\"\n      expires: \"2026-06-01T00:00:00Z\"\n      reason: \"a: b\"\n"+
			"    \"container_cpu: a\":\n      default: \"96\"\n      expires: \"2026-06-01T00:00:00Z\"\n      reason: \"b\"\n")
	if code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body:\n%s", code, body)
	}
	for _, want := range []string{
		`da_config_event{event="threshold_expired",metric_key="container_cpu",reason="container_cpu: a: b",target_severity="",tenant="tenant-a"} 1`,
		`da_config_event{event="threshold_expired",metric_key="container_cpu: a",reason="container_cpu: a: b",target_severity="",tenant="tenant-a"} 1`,
	} {
		if !strings.Contains(body, want) {
			t.Errorf("missing %s; body:\n%s", want, body)
		}
	}
}

// #2031: two spellings of one dimensional key — labels in another order,
// or quoted differently — are one threshold, in one file or across layers:
// one series, the winning layer's value, 200.
func TestMetrics_DimensionalKeySpellings_OneSeries(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		defaults, tenant string
		want             string
	}{
		"label order, one file": {"defaults:\n  redis_queue_length: 10\n",
			"tenants:\n  tenant-a:\n    'redis_queue_length{queue=\"a\", priority=\"high\"}': 5\n" +
				"    'redis_queue_length{priority=\"high\", queue=\"a\"}': 6\n",
			`user_threshold{component="redis",metric="queue_length",priority="high",queue="a",severity="warning",tenant="tenant-a"} 6`},
		"quotes, one file": {"defaults:\n  redis_queue_length: 10\n",
			"tenants:\n  tenant-a:\n    'redis_queue_length{queue=\"a\"}': 5\n    \"redis_queue_length{queue='a'}\": 6\n",
			`user_threshold{component="redis",metric="queue_length",queue="a",severity="warning",tenant="tenant-a"} 5`},
		"label order, profile and tenant": {"defaults:\n  pg_connections: 100\nprofiles:\n  std:\n    'pg_connections{r=\"x\",env=\"prod\"}': 30\n",
			"tenants:\n  tenant-a:\n    _profile: std\n    'pg_connections{env=\"prod\",r=\"x\"}': 31\n",
			`user_threshold{component="pg",env="prod",metric="connections",r="x",severity="warning",tenant="tenant-a"} 31`},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			code, body := scrapeTreeWith(t, tc.defaults, tc.tenant)
			if code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body:\n%s", code, body)
			}
			if !strings.Contains(body, tc.want) {
				t.Errorf("want %s; body:\n%s", tc.want, body)
			}
		})
	}
}

// #2031: a `_custom_alerts` entry repeating an earlier entry's series — a
// custom alert with the same name and shape, or a slo_burn_rate alert of the
// same shape (one user_slo_objective) — is dropped: 200, the first entry's
// series, and the drop counted on da_custom_alert_parse_errors.
func TestMetrics_CustomAlertDuplicateSeries_DropsTheLaterEntry(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		list, want string
	}{
		"same name and shape": {
			"      - {recipe: threshold, name: q_high, metric: qd, op: \">\", window: 5m, threshold: \"100:warning\"}\n" +
				"      - {recipe: threshold, name: q_high, metric: qd, op: \">\", window: 5m, threshold: \"200:warning\"}\n",
			`severity="warning",tenant="tenant-a"} 100`},
		"slo, same shape": {
			"      - {recipe: slo_burn_rate, name: avail1, metric: err_total, denominator_metric: req_total, objective: \"99.9\"}\n" +
				"      - {recipe: slo_burn_rate, name: avail2, metric: err_total, denominator_metric: req_total, objective: \"99.5\"}\n",
			`tenant="tenant-a"} 99.9`},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			code, body := scrapeTree(t, "tenants:\n  tenant-a:\n    _custom_alerts:\n"+tc.list)
			if code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body:\n%s", code, body)
			}
			if !strings.Contains(body, tc.want) {
				t.Errorf("want %s (the first entry); body:\n%s", tc.want, body)
			}
			if !strings.Contains(body, `da_custom_alert_parse_errors{tenant="tenant-a"} 1`) {
				t.Errorf("the dropped entry is not counted; body:\n%s", body)
			}
		})
	}
}
