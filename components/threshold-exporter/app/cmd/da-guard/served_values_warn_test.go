package main

// #2374: served-values used to call the reserved-key resolvers itself and then
// gather the collector, which calls them again, so each of their WARN lines
// reached stderr twice. The resolvers log through the process-global `log`,
// which served-values leaves on the process's stderr; the child process below
// is how this test reads that stderr without swapping the global logger.

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

func TestServedValues_EachResolverWarnOnce(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  container_cpu: 80\n",
		"conf.d/a1.yaml":        "tenants:\n  a1:\n    _silent_mode: \"bogus\"\n",
		"conf.d/a3.yaml":        "tenants:\n  a3:\n    _severity_dedup: \"bogus\"\n",
		"conf.d/b2.yaml":        "tenants:\n  b2:\n    _metadata: [1, 2]\n",
		"conf.d/a6.yaml":        "tenants:\n  a6:\n    _silent_mode:\n      target: all\n      expires: \"not-a-date\"\n",
		"conf.d/a8.yaml":        "tenants:\n  a8:\n    _state_maintenance:\n      target: all\n      expires: \"nope\"\n",
		"conf.d/a7.yaml":        "tenants:\n  a7:\n    container_cpu_critical: \"xyz\"\n",
	})
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
	for line, n := range count {
		if n != 1 {
			t.Errorf("printed %d times, want 1: %s", n, line)
		}
	}
	// Not vacuous: every resolver the tree trips did warn.
	for _, want := range []string{
		`unknown silent mode "bogus" for tenant=a1`,
		`unknown severity_dedup value "bogus" for tenant=a3`,
		`tenant=b2: failed to parse _metadata`,
		`invalid expires "not-a-date" in _silent_mode for tenant=a6`,
		`invalid expires "nope" in _state_maintenance for tenant=a8`,
		`invalid critical threshold "xyz" for tenant=a7`,
	} {
		found := false
		for line := range count {
			if strings.Contains(line, want) {
				found = true
			}
		}
		if !found {
			t.Errorf("no WARN containing %q; stderr:\n%s", want, stderr.String())
		}
	}
}
