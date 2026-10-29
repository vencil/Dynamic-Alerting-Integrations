package main

// cardinality_rows_test.go — #3380: the cardinality check counts the
// user_threshold rows the exporter's resolver produces for a tenant (before
// its own max_metrics_per_tenant cut, at the busiest minute of the day), not
// the keys of the tenant's effective config.
//
// The oracle is /metrics: the user_threshold series of the tenant, gathered
// through the exporter's own collector (internal/scrape) over the same tree,
// with the cap lifted so no series is cut. The prediction is what
// buildCheckInput hands the guard (CheckInput.PredictedRowCounts).

import (
	"encoding/json"
	"io"
	"log"
	"strings"
	"testing"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/internal/scrape"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// metricsRows is the number of tenant's user_threshold series /metrics
// serves at at, gathered from cfg through the exporter's collector. With
// uncut the cap is lifted first (cfg itself is not modified).
func metricsRows(t *testing.T, cfg *config.ThresholdConfig, at time.Time, tenant string, uncut bool) int {
	t.Helper()
	if uncut {
		c := *cfg
		c.MaxMetricsPerTenant = -1
		cfg = &c
	}
	reg := prometheus.NewRegistry()
	scrape.Register(reg, scrape.NewCollectorWithHooks(staticSource{cfg, at}, scrape.Hooks{}), scrape.NewConfigMetrics())
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatal(err)
	}
	n := 0
	for _, mf := range mfs {
		if mf.GetName() != "user_threshold" {
			continue
		}
		for _, m := range mf.GetMetric() {
			for _, lp := range m.GetLabel() {
				if lp.GetName() == "tenant" && lp.GetValue() == tenant {
					n++
				}
			}
		}
	}
	return n
}

// peakMetricsRows is the most user_threshold series of tenant /metrics
// serves (cap lifted) at any minute of fixedNow's UTC day.
func peakMetricsRows(t *testing.T, cfg *config.ThresholdConfig, tenant string) int {
	t.Helper()
	day := time.Date(fixedNow.Year(), fixedNow.Month(), fixedNow.Day(), 0, 0, 0, 0, time.UTC)
	peak := 0
	for _, m := range cfg.ScheduleCuts() {
		// Between two cuts no value changes (config.ScheduleCuts), so one
		// gather per cut is a gather of every minute.
		if n := metricsRows(t, cfg, day.Add(time.Duration(m)*time.Minute), tenant, true); n > peak {
			peak = n
		}
	}
	return peak
}

// predictedRows is what da-guard hands the cardinality check for tenant
// over the tree at dir, resolved at fixedNow.
func predictedRows(t *testing.T, dir, tenant string) int {
	t.Helper()
	scoped, err := config.ScopeEffective(dir, "", fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	in := buildCheckInput(scoped, &flags{cardinalityLimit: 1}, fixedNow)
	n, ok := in.PredictedRowCounts[tenant]
	if !ok {
		t.Fatalf("no prediction for %s: %v", tenant, in.PredictedRowCounts)
	}
	return n
}

func loadTree(t *testing.T, dir string) *config.ThresholdConfig {
	t.Helper()
	cfg, pf, err := config.LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil || len(pf) > 0 {
		t.Fatalf("LoadDir: err=%v parseFailed=%v", err, pf)
	}
	return cfg
}

// cardinalityFixtureA is the #3380 report: the root declares one threshold;
// a subtree default the root does not declare is not delivered (its own
// error, subtree_default_undeliverable), so /metrics serves t1 one series.
var cardinalityFixtureA = map[string]string{
	"_defaults.yaml":         "defaults:\n  mysql_connections: 80\n",
	"finance/_defaults.yaml": "defaults:\n  redis_evicted_keys: 100\n",
	"finance/t1.yaml":        "tenants:\n  t1: {}\n",
}

func TestCardinality_PredictionIsWhatMetricsServes(t *testing.T) {
	t.Parallel()
	retired := "mysql_" + "cpu" // #1231 retired spelling (forbid-legacy-mysql-cpu-key)
	for _, tc := range []struct {
		name   string
		files  map[string]string
		tenant string
		want   int
	}{
		{name: "fixture A", files: cardinalityFixtureA, tenant: "t1", want: 1},
		{name: "critical key", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"tx.yaml":        "tenants:\n  tx:\n    mysql_connections_critical: \"90\"\n",
		}, tenant: "tx", want: 2},
		{name: "dimensional keys, exact and regex", files: map[string]string{
			"_defaults.yaml": "defaults:\n  redis_keys: 5\n",
			"tx.yaml": "tenants:\n  tx:\n    redis_keys{db=\"db0\"}: \"10\"\n" +
				"    redis_keys{db=~\"db[0-9]+\"}: \"20\"\n",
		}, tenant: "tx", want: 3},
		// #1231: the twin is a series of its own on /metrics, so it counts.
		{name: "legacy twin", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n",
			"tx.yaml":        "tenants:\n  tx:\n    " + retired + "_critical: \"90\"\n",
		}, tenant: "tx", want: 4},
		{name: "custom alerts", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"tx.yaml": "tenants:\n  tx:\n    _custom_alerts:\n" +
				"      - {recipe: threshold, name: a, metric: m1, op: \">\", window: 5m, threshold: \"80\"}\n" +
				"      - {recipe: threshold, name: b, metric: m2, op: \">\", window: 5m, threshold: disable}\n" +
				"      - {recipe: threshold, name: c, metric: m3, op: \">\", window: 5m, threshold: \"1\"}\n",
		}, tenant: "tx", want: 3},
		{name: "disable", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_keys: 5\n",
			"tx.yaml":        "tenants:\n  tx:\n    redis_keys: disable\n",
		}, tenant: "tx", want: 1},
		// fixedNow is 10:00, outside the window: the row the window adds
		// is still counted (the cut truncates at the busiest minute).
		{name: "schedule adds a row 02:00-04:00", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_keys: 5\n",
			"tx.yaml": "tenants:\n  tx:\n    redis_keys:\n      default: disable\n" +
				"      overrides:\n        - window: \"02:00-04:00\"\n          value: \"7\"\n",
		}, tenant: "tx", want: 2},
		// Over the exporter's own cap: predicted before the cut.
		{name: "over max_metrics_per_tenant", files: map[string]string{
			"_defaults.yaml": "max_metrics_per_tenant: 2\ndefaults:\n  m1: 1\n  m2: 2\n  m3: 3\n  m4: 4\n",
			"tx.yaml":        "tenants:\n  tx:\n    m1_critical: \"9\"\n",
		}, tenant: "tx", want: 5},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := writeTree(t, tc.files)
			cfg := loadTree(t, dir)
			got := predictedRows(t, dir, tc.tenant)
			if served := peakMetricsRows(t, cfg, tc.tenant); got != served {
				t.Errorf("predicted %d rows, /metrics serves %d series (cap lifted, busiest minute)", got, served)
			}
			if got != tc.want {
				t.Errorf("predicted %d rows, want %d", got, tc.want)
			}
		})
	}
}

// The prediction ignores the exporter's own cut: /metrics keeps 2 series of
// tx, the prediction is the 5 the tenant asks for.
func TestCardinality_OverTheExportersCapIsPredictedUncut(t *testing.T) {
	t.Parallel()
	dir := writeTree(t, map[string]string{
		"_defaults.yaml": "max_metrics_per_tenant: 2\ndefaults:\n  m1: 1\n  m2: 2\n  m3: 3\n  m4: 4\n",
		"tx.yaml":        "tenants:\n  tx:\n    m1_critical: \"9\"\n",
	})
	if served := metricsRows(t, loadTree(t, dir), fixedNow, "tx", false); served != 2 {
		t.Fatalf("control: /metrics serves %d series of tx, want 2 (the exporter's cap)", served)
	}
	code, stdout, stderr := runOnce(t, "--config-dir", dir, "--format", "md")
	if code != exitFindings || !strings.Contains(stdout, "would produce 5 user_threshold rows") ||
		!strings.Contains(stdout, "cardinality_exceeded") {
		t.Errorf("exit = %d, want %d with 5 rows over the cap of 2. stdout=%q stderr=%q", code, exitFindings, stdout, stderr)
	}
}

// End to end on fixture A: the one series t1 is served is not over a limit
// of 1 (before #3380: cardinality_exceeded, the subtree key counted as a
// row) and is no finding at a limit of 2 (before: cardinality_warning). The
// run's only error is the subtree key's own subtree_default_undeliverable,
// so the cardinality check adds no error and changes no exit code.
func TestCardinality_FixtureA_EndToEnd(t *testing.T) {
	t.Parallel()
	dir := writeTree(t, cardinalityFixtureA)
	type finding struct {
		Severity string `json:"severity"`
		Kind     string `json:"kind"`
		TenantID string `json:"tenant_id"`
		Message  string `json:"message"`
	}
	kinds := func(limit string) (int, map[string]string) {
		t.Helper()
		code, stdout, stderr := runOnce(t, "--config-dir", dir, "--cardinality-limit", limit, "--format", "json")
		var doc struct {
			Report struct {
				Findings []finding `json:"findings"`
			} `json:"report"`
		}
		if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
			t.Fatalf("limit %s: %v\nstdout=%s\nstderr=%s", limit, err, stdout, stderr)
		}
		out := map[string]string{}
		for _, f := range doc.Report.Findings {
			out[f.Kind] = f.Severity
		}
		return code, out
	}
	baseCode, base := kinds("0") // 0 = no cardinality check
	if base["subtree_default_undeliverable"] != "error" || len(base) != 1 || baseCode != exitFindings {
		t.Fatalf("control (limit 0): exit %d, findings %v; want exit %d with subtree_default_undeliverable only",
			baseCode, base, exitFindings)
	}
	code, got := kinds("1")
	if code != baseCode || got["cardinality_exceeded"] != "" || got["cardinality_warning"] != "warn" || len(got) != 2 {
		t.Errorf("limit 1: exit %d, findings %v; want exit %d, the control's findings plus a cardinality_warning (1 row = 100%% of 1)",
			code, got, baseCode)
	}
	code, got = kinds("2")
	if code != baseCode || len(got) != 1 {
		t.Errorf("limit 2: exit %d, findings %v; want the control's (no cardinality finding)", code, got)
	}
}

// Exit 0 end to end: the same shape with the subtree key declared at the
// root, so nothing else is reported. Two rows at a limit of 2 is the warn
// tier (100%), which does not change the exit code.
func TestCardinality_DeclaredSubtreeKey_ExitsZero(t *testing.T) {
	t.Parallel()
	dir := writeTree(t, map[string]string{
		"_defaults.yaml":         "defaults:\n  mysql_connections: 80\n  redis_evicted_keys: 50\n",
		"finance/_defaults.yaml": "defaults:\n  redis_evicted_keys: 100\n",
		"finance/t1.yaml":        "tenants:\n  t1: {}\n",
	})
	if served := metricsRows(t, loadTree(t, dir), fixedNow, "t1", true); served != 2 {
		t.Fatalf("control: /metrics serves %d series of t1, want 2", served)
	}
	code, stdout, stderr := runOnce(t, "--config-dir", dir, "--cardinality-limit", "2", "--format", "md")
	if code != exitOK || strings.Contains(stdout, "cardinality_exceeded") {
		t.Errorf("exit = %d, want %d. stdout=%q stderr=%q", code, exitOK, stdout, stderr)
	}
	code, stdout, _ = runOnce(t, "--config-dir", dir, "--cardinality-limit", "1", "--format", "md")
	if code != exitFindings || !strings.Contains(stdout, "would produce 2 user_threshold rows") {
		t.Errorf("must-trigger: limit 1 exit = %d, want %d with 2 rows: %q", code, exitFindings, stdout)
	}
}
