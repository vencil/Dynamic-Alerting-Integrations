package scrape

// #2426: with `state_filters.maintenance` declared, ONE exporter scrape used
// to print the invalid _state_maintenance expires WARN twice —
// OperationalStatesAt → ResolveStateFiltersAt logged it, then
// collectMaintenanceExpiries → ResolveMaintenanceExpiriesAt logged it again.
//
// The resolvers log through the process-global `log`, which has no seam; the
// child process below is how this test reads one scrape's stderr without
// swapping the global logger (the same shape as da-guard's
// served_values_warn_test.go).

import (
	"os"
	"os/exec"
	"regexp"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// scrapeWarnChildEnv selects the child's config: "declared" or "undeclared".
const scrapeWarnChildEnv = "SCRAPE_WARN_CHILD_TREE"

// scrapeWarnConfig is one bad maintenance expires, one maintenance value that
// is not YAML at all (the other maintenance WARN), one good one (no WARN) and
// one bad _silent_mode expires (another WARN type, printed once per scrape).
func scrapeWarnConfig(declared bool) *config.ThresholdConfig {
	cfg := &config.ThresholdConfig{
		Defaults: map[string]float64{"mysql_connections": 80},
		Tenants: map[string]map[string]config.ScheduledValue{
			"t-bad":      {"_state_maintenance": {Default: "target: all\nexpires: \"nope\"\n"}},
			"t-bad-yaml": {"_state_maintenance": {Default: "expires: [\n"}},
			"t-good":     {"_state_maintenance": {Default: "target: all\nexpires: \"2099-01-01T00:00:00Z\"\n"}},
			"t-silent":   {"_silent_mode": {Default: "target: all\nexpires: \"not-a-date\"\n"}},
		},
	}
	if declared {
		cfg.StateFilters = map[string]config.StateFilter{
			"maintenance": {Severity: "info", DefaultState: "disable"},
		}
	}
	return cfg
}

// TestScrapeWarnChildProcess is not a test: it is the child process of
// TestCollector_MaintenanceExpiresWarnOncePerScrape, and skips when run any
// other way. It gathers the exporter's collector (no Hooks) exactly once.
func TestScrapeWarnChildProcess(t *testing.T) {
	tree := os.Getenv(scrapeWarnChildEnv)
	if tree == "" {
		t.Skip("child process of TestCollector_MaintenanceExpiresWarnOncePerScrape")
	}
	reg := prometheus.NewRegistry()
	Register(reg, NewCollector(staticSource{scrapeWarnConfig(tree == "declared")}), NewConfigMetrics())
	if _, err := reg.Gather(); err != nil {
		t.Fatalf("gather: %v", err)
	}
}

func TestCollector_MaintenanceExpiresWarnOncePerScrape(t *testing.T) {
	t.Parallel()
	for _, tree := range []string{"declared", "undeclared"} {
		t.Run(tree, func(t *testing.T) {
			t.Parallel()
			cmd := exec.Command(os.Args[0], "-test.run=^TestScrapeWarnChildProcess$")
			cmd.Env = append(os.Environ(), scrapeWarnChildEnv+"="+tree)
			var stderr strings.Builder
			cmd.Stderr = &stderr
			if err := cmd.Run(); err != nil {
				t.Fatalf("child: %v\nstderr:\n%s", err, stderr.String())
			}
			stamp := regexp.MustCompile(`^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} `)
			count := map[string]int{}
			for _, line := range strings.Split(stderr.String(), "\n") {
				line = stamp.ReplaceAllString(line, "")
				if strings.HasPrefix(line, "WARN") {
					count[line]++
				}
			}
			for _, tc := range []struct {
				sub  string
				want int
			}{
				{`invalid expires "nope" in _state_maintenance for tenant=t-bad`, 1},
				{`failed to parse structured _state_maintenance for tenant=t-bad-yaml`, 1},
				// Control: another expires WARN type, once per scrape before too.
				{`invalid expires "not-a-date" in _silent_mode for tenant=t-silent`, 1},
				// Control: a valid expires is not warned about.
				{`tenant=t-good`, 0},
			} {
				n := 0
				for line, c := range count {
					if strings.Contains(line, tc.sub) {
						n += c
					}
				}
				if n != tc.want {
					t.Errorf("WARN containing %q printed %d times in one scrape, want %d; stderr:\n%s", tc.sub, n, tc.want, stderr.String())
				}
			}
			for line, n := range count {
				if n != 1 {
					t.Errorf("printed %d times in one scrape, want 1: %s", n, line)
				}
			}
		})
	}
}
