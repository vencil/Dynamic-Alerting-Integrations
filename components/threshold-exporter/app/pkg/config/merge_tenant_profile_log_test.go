package config

// The tenant-api merge core runs per request, so the profile expansion it
// shares with /metrics must not write ApplyProfiles' WARNs to the process
// log (#1385 review): the facts reach the caller as an Error / notices.
// /metrics' ApplyProfiles must keep writing them, verbatim and with the
// one-per-(profile, key) declared-key WARN.
//
// ⛔ NOT t.Parallel(): these tests swap the process-global log output. The
// cleanup is an idempotent reset to os.Stderr (the package default), never
// a save-then-restore (test-map.md).

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func captureGlobalLog(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	log.SetOutput(&buf)
	t.Cleanup(func() { log.SetOutput(os.Stderr) })
	return &buf
}

// Both WARN classes fire: tx elects an unknown profile, ty and tz a profile
// that supplies two declared-without-value keys (two tenants, so the
// declared-key WARN's once-per-(profile, key) rule is exercised).
var profileLogTree = map[string]string{
	"_defaults.yaml": "defaults:\n  mysql_connections: 80\noptional_overrides: [oracle_wait, redis_x]\n",
	"_profiles.yaml": "profiles:\n  strict:\n    oracle_wait: \"1\"\n    redis_x: \"2\"\n    mysql_connections: \"55\"\n",
	"tx.yaml":        "tenants:\n  tx:\n    _profile: nosuch\n",
	"ty.yaml":        "tenants:\n  ty:\n    _profile: strict\n",
	"tz.yaml":        "tenants:\n  tz:\n    _profile: strict\n",
}

func TestMergeTenantProfileExpansionWritesNoLog(t *testing.T) {
	dir := t.TempDir()
	writeMergeTree(t, dir, profileLogTree)
	buf := captureGlobalLog(t)
	for _, id := range []string{"tx", "ty"} {
		body, err := os.ReadFile(filepath.Join(dir, id+".yaml"))
		if err != nil {
			t.Fatal(err)
		}
		m := MergeTenantWithRootDefaults(dir, id, body)
		_ = m.ValidateTenantKeys()
		_ = m.ResolveAt(platformMergeNow)
		parsed, err := ParseConfigFile(body)
		if err != nil {
			t.Fatal(err)
		}
		pm := MergeParsedTenantWithRootDefaults(dir, parsed)
		_ = pm.ValidateTenantKeys()
		if id == "ty" && !containsRow(resolvedRows(&m, id), "mysql_connections{}=55/warning") {
			t.Fatalf("precondition: ty's profile is not expanded: %v", resolvedRows(&m, id))
		}
	}
	if buf.Len() != 0 {
		t.Errorf("the tenant-api merge core wrote to the global log:\n%s", buf.String())
	}
}

func TestApplyProfilesStillLogsItsWarnings(t *testing.T) {
	dir := t.TempDir()
	writeMergeTree(t, dir, profileLogTree)
	cfg, _, err := LoadDir(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	// LoadDir already expanded once; the two WARN classes concern keys the
	// expansion never writes (an unknown name, declared keys), so a second
	// expansion under capture meets the same decisions.
	buf := captureGlobalLog(t)
	cfg.ApplyProfiles()
	out := buf.String()
	if n := strings.Count(out, `WARN: tenant=tx references unknown profile "nosuch", ignoring`); n != 1 {
		t.Errorf("unknown-profile WARN written %d times, want 1:\n%s", n, out)
	}
	for _, k := range []string{"oracle_wait", "redis_x"} {
		if n := strings.Count(out, `WARN: profile "strict" supplies "`+k+`", but that key is declared without a platform value`); n != 1 {
			t.Errorf("declared-key WARN for %s written %d times, want 1:\n%s", k, n, out)
		}
	}
}
