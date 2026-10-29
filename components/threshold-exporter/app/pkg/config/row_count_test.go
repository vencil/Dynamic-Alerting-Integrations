package config

// row_count_test.go — PeakTenantRowCounts (#3380): da-guard's cardinality
// prediction is the resolver's own row count, before the cut, at the busiest
// minute of the UTC day. The oracle is ResolveAt itself, read at every one of
// the day's 1440 minutes with the cap lifted; each row also pins the count by
// hand, so the oracle and the prediction cannot agree on a wrong number.

import (
	"io"
	"log"
	"reflect"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// rowsEveryMinute is, per tenant, the most rows ResolveAt returns for it at
// any minute of fixedNow's UTC day, with max_metrics_per_tenant lifted so no
// row is cut.
func rowsEveryMinute(c *ThresholdConfig) map[string]int {
	uncut := *c
	uncut.MaxMetricsPerTenant = -1
	day := time.Date(fixedNow.Year(), fixedNow.Month(), fixedNow.Day(), 0, 0, 0, 0, time.UTC)
	peak := map[string]int{}
	for m := 0; m < MinutesPerDay; m++ {
		n := map[string]int{}
		for _, r := range uncut.ResolveAt(day.Add(time.Duration(m) * time.Minute)) {
			n[r.Tenant]++
		}
		for t, k := range n {
			if k > peak[t] {
				peak[t] = k
			}
		}
	}
	return peak
}

func loadRowCountTree(t *testing.T, files map[string]string) *ThresholdConfig {
	t.Helper()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, files)
	cfg, pf, err := LoadDir(tmp, log.New(io.Discard, "", 0))
	if err != nil || len(pf) > 0 {
		t.Fatalf("LoadDir: err=%v parseFailed=%v", err, pf)
	}
	return cfg
}

func TestPeakTenantRowCounts_IsTheResolversRowCount(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name  string
		files map[string]string
		want  int // tenant tx
	}{
		// The #3380 report: a subtree default the root does not declare is
		// not delivered, so it is no row — the old key count said 2.
		{name: "undeclared subtree default", files: map[string]string{
			"_defaults.yaml":         "defaults:\n  mysql_connections: 80\n",
			"finance/_defaults.yaml": "defaults:\n  redis_evicted_keys: 100\n",
			"finance/tx.yaml":        "tenants:\n  tx: {}\n",
		}, want: 1},
		{name: "critical key", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"tx.yaml":        "tenants:\n  tx:\n    mysql_connections_critical: \"90\"\n",
		}, want: 2},
		// A dimensional key is a row of its own, an exact and a regex one
		// alike: one row each, beside the base.
		{name: "dimensional keys", files: map[string]string{
			"_defaults.yaml": "defaults:\n  redis_keys: 5\n",
			"tx.yaml": "tenants:\n  tx:\n    redis_keys{db=\"db0\"}: \"10\"\n" +
				"    redis_keys{db=~\"db[0-9]+\"}: \"20\"\n",
		}, want: 3},
		// #1231: every mysql_threads_running row ships with its legacy twin,
		// whichever spelling wrote it — two rows per threshold.
		{name: "legacy twin", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_threads_running: 30\n",
			"tx.yaml":        "tenants:\n  tx:\n    " + retiredCPU + "_critical: \"90\"\n",
		}, want: 4},
		{name: "custom alerts", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"tx.yaml": "tenants:\n  tx:\n    _custom_alerts:\n" +
				"      - {recipe: threshold, name: a, metric: m1, op: \">\", window: 5m, threshold: \"80\"}\n" +
				"      - {recipe: threshold, name: b, metric: m2, op: \">\", window: 5m, threshold: disable}\n" +
				"      - {recipe: threshold, name: c, metric: m3, op: \">\", window: 5m, threshold: \"1\"}\n",
		}, want: 3},
		{name: "disable", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_keys: 5\n",
			"tx.yaml":        "tenants:\n  tx:\n    redis_keys: disable\n",
		}, want: 1},
		// A row only a window turns on counts: the cut truncates at the
		// busiest minute, whichever minute the run is at.
		{name: "schedule adds a row 02:00-04:00", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_keys: 5\n",
			"tx.yaml": "tenants:\n  tx:\n    redis_keys:\n      default: disable\n" +
				"      overrides:\n        - window: \"02:00-04:00\"\n          value: \"7\"\n",
		}, want: 2},
		{name: "schedule adds a critical row 22:00-06:00", files: map[string]string{
			"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
			"tx.yaml": "tenants:\n  tx:\n    mysql_connections_critical:\n      default: disable\n" +
				"      overrides:\n        - window: \"22:00-06:00\"\n          value: \"95\"\n",
		}, want: 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			cfg := loadRowCountTree(t, tc.files)
			got := cfg.PeakTenantRowCounts(fixedNow)["tx"]
			if oracle := rowsEveryMinute(cfg)["tx"]; got != oracle {
				t.Errorf("PeakTenantRowCounts = %d, ResolveAt's most rows over the day = %d", got, oracle)
			}
			if got != tc.want {
				t.Errorf("PeakTenantRowCounts = %d, want %d", got, tc.want)
			}
		})
	}
}

// The schedule's rows are counted whatever minute the run is at: inside the
// window (where the resolve at now already sees them) and outside it.
func TestPeakTenantRowCounts_SameAtEveryInstant(t *testing.T) {
	t.Parallel()
	cfg := loadRowCountTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_keys: 5\n",
		"tx.yaml": "tenants:\n  tx:\n    redis_keys:\n      default: disable\n" +
			"      overrides:\n        - window: \"02:00-04:00\"\n          value: \"7\"\n",
		"ty.yaml": "tenants:\n  ty: {}\n",
	})
	day := time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC)
	for _, at := range []time.Time{day, day.Add(150 * time.Minute), day.Add(4 * time.Hour), day.Add(23*time.Hour + 59*time.Minute)} {
		got := cfg.PeakTenantRowCounts(at)
		if got["tx"] != 2 || got["ty"] != 2 {
			t.Errorf("at %s: %v, want tx 2, ty 2", at.Format("15:04"), got)
		}
	}
}

// The prediction is the count before the cut: a tenant over the exporter's
// own max_metrics_per_tenant is predicted at what it asks for, not at what
// /metrics keeps.
func TestPeakTenantRowCounts_CountsBeforeTheCut(t *testing.T) {
	t.Parallel()
	cfg := loadRowCountTree(t, map[string]string{
		"_defaults.yaml": "max_metrics_per_tenant: 2\ndefaults:\n  m1: 1\n  m2: 2\n  m3: 3\n  m4: 4\n",
		"tx.yaml":        "tenants:\n  tx:\n    m1_critical: \"9\"\n",
	})
	served := 0
	for _, r := range cfg.ResolveAt(fixedNow) {
		if r.Tenant == "tx" {
			served++
		}
	}
	if served != 2 {
		t.Fatalf("control: the exporter serves %d rows of tx, want 2 (its cap)", served)
	}
	got := cfg.PeakTenantRowCounts(fixedNow)["tx"]
	if oracle := rowsEveryMinute(cfg)["tx"]; got != oracle || got != 5 {
		t.Errorf("PeakTenantRowCounts = %d, want 5 (uncut ResolveAt = %d)", got, oracle)
	}
}

func TestPeakTenantRowCounts_DoesNotModifyTheConfig(t *testing.T) {
	t.Parallel()
	cfg := loadRowCountTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  redis_keys: 5\n",
		"tx.yaml": "tenants:\n  tx:\n    redis_keys:\n      default: \"6\"\n" +
			"      overrides:\n        - window: \"02:00-04:00\"\n          value: \"7\"\n",
	})
	before := len(cfg.Tenants["tx"]["redis_keys"].Overrides)
	cfg.PeakTenantRowCounts(fixedNow)
	if after := len(cfg.Tenants["tx"]["redis_keys"].Overrides); after != before || before != 1 {
		t.Errorf("schedule overrides %d → %d; the config must not change", before, after)
	}
}

// PeakTenantRowCountsOf resolves only the tenants asked for, and gives each
// the entry the full table gives it — schedules and all.
func TestPeakTenantRowCountsOf_IsTheFullTablesEntry(t *testing.T) {
	t.Parallel()
	cfg := loadRowCountTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_keys: 5\n",
		"ta.yaml": "tenants:\n  ta:\n    redis_keys:\n      default: disable\n" +
			"      overrides:\n        - window: \"02:00-04:00\"\n          value: \"7\"\n",
		"tb.yaml": "tenants:\n  tb:\n    mysql_connections_critical:\n      default: disable\n" +
			"      overrides:\n        - window: \"22:00-06:00\"\n          value: \"95\"\n",
		"tc.yaml": "tenants:\n  tc:\n    redis_keys: disable\n",
	})
	full := cfg.PeakTenantRowCounts(fixedNow)
	if want := map[string]int{"ta": 2, "tb": 3, "tc": 1}; !reflect.DeepEqual(full, want) {
		t.Fatalf("full table %v, want %v", full, want)
	}
	for _, subset := range [][]string{{"ta"}, {"tb", "tc"}, {"ta", "tb", "tc"}, {"tc", "nobody"}, nil} {
		got := cfg.PeakTenantRowCountsOf(subset, fixedNow)
		want := map[string]int{}
		for _, id := range subset {
			if n, ok := full[id]; ok {
				want[id] = n
			}
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("subset %v: %v, want %v (the full table's entries, no other tenant)", subset, got, want)
		}
	}
}

// `expires:` is read at now: a tenant's time-boxed "disable" switches the
// default's row off until it expires, and the row is back after — at every
// minute of the day. (A time-boxed `_critical` value does not change the
// count: the resolver serves the critical row either way.)
func TestPeakTenantRowCounts_ExpiresIsReadAtNow(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name    string
		expires time.Time
		want    int
	}{
		{"before expiry", fixedNow.Add(48 * time.Hour), 1},
		{"after expiry", fixedNow.Add(-48 * time.Hour), 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			cfg := loadRowCountTree(t, map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  redis_keys: 5\n",
				"tx.yaml": "tenants:\n  tx:\n    redis_keys:\n      default: disable\n" +
					"      expires: \"" + tc.expires.Format(time.RFC3339) + "\"\n      reason: drill\n",
			})
			got := cfg.PeakTenantRowCounts(fixedNow)["tx"]
			if oracle := rowsEveryMinute(cfg)["tx"]; got != oracle {
				t.Errorf("PeakTenantRowCounts = %d, ResolveAt's most rows over the day = %d", got, oracle)
			}
			if got != tc.want {
				t.Errorf("PeakTenantRowCounts = %d, want %d", got, tc.want)
			}
		})
	}
}
