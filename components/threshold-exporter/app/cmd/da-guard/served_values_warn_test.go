package main

// #2374: served-values used to call the _metadata, _severity_dedup and
// _silent_mode resolvers itself and then gather the collector, which calls
// them again, so each of their WARN lines reached stderr twice. The resolvers
// log through the process-global `log`, which served-values leaves on the
// process's stderr; the child process below is how this test reads that
// stderr without swapping the global logger.
//
// ⚠️ Not covered by the #2374 fix: the _state_maintenance expires WARN. When
// the tree declares `state_filters.maintenance`, ONE collector scrape prints
// it twice — ResolveStateFiltersAt and ResolveMaintenanceExpiriesAt each log
// maintenanceExpiresIgnoredWarn — so the exporter prints it twice per scrape
// as well. That is the collector's own double log, not served-values
// resolving twice; it is a known follow-up, pinned below as it is today.

import (
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// servedValuesChildEnv carries the child's served-values arguments, one per line.
const servedValuesChildEnv = "DA_GUARD_SERVED_VALUES_CHILD_ARGS"

// TestServedValuesChildProcess is not a test: it is the child process of
// TestServedValues_EachResolverWarnOnce, and skips when run any other way.
func TestServedValuesChildProcess(t *testing.T) {
	args := os.Getenv(servedValuesChildEnv)
	if args == "" {
		t.Skip("child process of TestServedValues_EachResolverWarnOnce")
	}
	os.Exit(run(strings.Split(args, "\n"), os.Stdout, os.Stderr))
}

// servedValuesWarnCounts runs served-values over files in a child process and
// counts each distinct WARN line of its stderr (log timestamps stripped).
func servedValuesWarnCounts(t *testing.T, files map[string]string) (map[string]int, string) {
	t.Helper()
	tmp := t.TempDir()
	tree := make(map[string]string, len(files))
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	cmd := exec.Command(os.Args[0], "-test.run=^TestServedValuesChildProcess$")
	cmd.Env = append(os.Environ(), servedValuesChildEnv+"="+strings.Join([]string{
		servedValuesCmd, "--config-dir", filepath.Join(tmp, "conf.d"), "--at", "2026-09-28T00:00:00Z",
	}, "\n"))
	var stdout, stderr strings.Builder
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	if err := cmd.Run(); err != nil {
		t.Fatalf("child: %v\nstderr:\n%s", err, stderr.String())
	}
	if !strings.HasPrefix(stdout.String(), "{") {
		t.Fatalf("child stdout is not the JSON document:\n%s", stdout.String())
	}
	stamp := regexp.MustCompile(`^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2} `)
	count := map[string]int{}
	for _, line := range strings.Split(stderr.String(), "\n") {
		line = stamp.ReplaceAllString(line, "")
		if strings.HasPrefix(line, "WARN") {
			count[line]++
		}
	}
	return count, stderr.String()
}

func TestServedValues_EachResolverWarnOnce(t *testing.T) {
	t.Parallel()
	const maintenanceWarn = `invalid expires "nope" in _state_maintenance for tenant=a8`
	tenants := map[string]string{
		"a1.yaml": "tenants:\n  a1:\n    _silent_mode: \"bogus\"\n",
		"a3.yaml": "tenants:\n  a3:\n    _severity_dedup: \"bogus\"\n",
		"b2.yaml": "tenants:\n  b2:\n    _metadata: [1, 2]\n",
		"a6.yaml": "tenants:\n  a6:\n    _silent_mode:\n      target: all\n      expires: \"not-a-date\"\n",
		"a8.yaml": "tenants:\n  a8:\n    _state_maintenance:\n      target: all\n      expires: \"nope\"\n",
		"a7.yaml": "tenants:\n  a7:\n    container_cpu_critical: \"xyz\"\n",
	}
	for _, tc := range []struct {
		name     string
		defaults string
		// maintenanceWant is how many times the a8 WARN is printed.
		maintenanceWant int
	}{
		// No maintenance filter: ResolveStateFiltersAt never reaches a8, so
		// only ResolveMaintenanceExpiriesAt warns — once, before #2374 too.
		// This row therefore checks nothing about the fix for a8; it is here
		// so the other six lines are counted over a tree that has it.
		{"no maintenance filter", "defaults:\n  container_cpu: 80\n", 1},
		// ⚠️ Known follow-up, NOT the desired state: with the filter declared
		// (as the repo's own conf.d/_defaults.yaml does), both collector
		// resolvers log the same WARN in one scrape, so it prints twice (three
		// times before #2374). Pinned so a change to it is seen; when the
		// follow-up lands, this becomes 1.
		{"maintenance filter declared", "defaults:\n  container_cpu: 80\n" +
			"state_filters:\n  maintenance:\n    reasons: []\n    severity: \"info\"\n    default_state: \"disable\"\n", 2},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{"_defaults.yaml": tc.defaults}
			for k, v := range tenants {
				files[k] = v
			}
			count, stderr := servedValuesWarnCounts(t, files)

			// Printed twice before #2374 (served-values resolved them itself).
			fixed := []string{
				`unknown silent mode "bogus" for tenant=a1`,
				`unknown severity_dedup value "bogus" for tenant=a3`,
				`tenant=b2: failed to parse _metadata`,
				`invalid expires "not-a-date" in _silent_mode for tenant=a6`,
			}
			// Once before and after: served-values never resolved these itself.
			control := []string{`invalid critical threshold "xyz" for tenant=a7`}
			for _, want := range append(fixed, control...) {
				if n := countContaining(count, want); n != 1 {
					t.Errorf("WARN containing %q printed %d times, want 1; stderr:\n%s", want, n, stderr)
				}
			}
			if n := countContaining(count, maintenanceWarn); n != tc.maintenanceWant {
				t.Errorf("WARN containing %q printed %d times, want %d; stderr:\n%s", maintenanceWarn, n, tc.maintenanceWant, stderr)
			}
			// Nothing else printed more than once.
			for line, n := range count {
				if n != 1 && !strings.Contains(line, maintenanceWarn) {
					t.Errorf("printed %d times, want 1: %s", n, line)
				}
			}
		})
	}
}

// countContaining sums the counts of the WARN lines that contain sub.
func countContaining(count map[string]int, sub string) int {
	n := 0
	for line, c := range count {
		if strings.Contains(line, sub) {
			n += c
		}
	}
	return n
}
