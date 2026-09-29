package main

import (
	"log"
	"path/filepath"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// run() must leave the process-global logger exactly as it found it.
//
// ⛔ #2444: run() used to point `log` at its caller's errOut. Every t.Parallel
// test in this package calls run() with its own buffer, and the resolvers in
// pkg/config and the collector log through the global — so one test's WARN
// landed in another test's buffer while that test was writing it, and -race
// failed whichever pair overlapped. The overlap depends on scheduling (4-core
// runs stayed green), so this pins the cause instead of waiting for the race.
//
// NOT parallel: it reads the global, and Go resumes parallel tests only after
// every sequential top-level test has returned.
func TestRun_LeavesTheGlobalLoggerAlone(t *testing.T) {
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  cpu: 70\n",
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
	})
	confDir := filepath.Join(tmp, "conf.d")
	for _, args := range [][]string{
		{"--config-dir", confDir, "--required-fields", "cpu"},
		{servedValuesCmd, "--config-dir", confDir},
	} {
		wantW, wantF := log.Writer(), log.Flags()
		code, _, stderr := runOnce(t, args...)
		if code != exitOK {
			t.Fatalf("%v: exit = %d, stderr=%q", args, code, stderr)
		}
		if log.Writer() != wantW {
			t.Errorf("%v: run() replaced the global logger's writer", args)
		}
		if log.Flags() != wantF {
			t.Errorf("%v: run() changed the global logger's flags %d -> %d", args, wantF, log.Flags())
		}
	}
}
