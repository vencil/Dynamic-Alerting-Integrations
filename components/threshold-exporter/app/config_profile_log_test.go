package main

// #2513: the profile WARNs of a directory load now travel through the
// manager's logger (BuildFlatConfig's FlatBuildInput.Logger) instead of
// log.Printf. A manager built the production way logs to log.Default(), so
// the exporter's process log must still carry the unknown-profile WARN,
// verbatim and once per load.
//
// ⚠️ Swaps the process-global logger: not parallel, and reset to the log
// package's defaults afterwards (idempotent reset, docs/internal/test-map.md).

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoad_UnknownProfileWarnStillReachesTheProcessLog(t *testing.T) {
	dir := t.TempDir()
	for name, body := range map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"_profiles.yaml": "profiles:\n  real-profile:\n    mysql_connections: \"55\"\n",
		"tx.yaml":        "tenants:\n  tx:\n    _profile: no-such-profile\n",
	} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() {
		log.SetOutput(os.Stderr)
		log.SetFlags(log.LstdFlags)
	})

	m := NewConfigManager(dir) // production logger: log.Default()
	if err := m.Load(); err != nil {
		t.Fatal(err)
	}
	const want = `WARN: tenant=tx references unknown profile "no-such-profile", ignoring`
	if n := strings.Count(buf.String(), want); n != 1 {
		t.Errorf("exporter load wrote the unknown-profile WARN %d times, want 1:\n%s", n, buf.String())
	}
}
