package config

// #2513: BuildFlatConfig's profile expansion writes its WARNs to the
// caller's logger (FlatBuildInput.Logger), not to the process-global log.
// LoadDir(dir, nil) — tenant-api's snapshot reload — therefore writes
// nothing; a LoadDir handed a logger gets both WARN classes there, verbatim.
// ScopeEffective (da-guard) deliberately keeps them on the process log.
//
// ⛔ NOT t.Parallel(): swaps the process-global log output (captureGlobalLog
// resets it idempotently to os.Stderr).

import (
	"bytes"
	"log"
	"strings"
	"testing"
)

func TestLoadDirProfileWarnsGoToTheCallersLogger(t *testing.T) {
	dir := t.TempDir()
	writeMergeTree(t, dir, profileLogTree)
	global := captureGlobalLog(t)

	if _, _, err := LoadDir(dir, nil); err != nil {
		t.Fatal(err)
	}
	if global.Len() != 0 {
		t.Errorf("LoadDir(nil) wrote to the process log:\n%s", global.String())
	}

	var own bytes.Buffer
	if _, _, err := LoadDir(dir, log.New(&own, "", 0)); err != nil {
		t.Fatal(err)
	}
	out := own.String()
	if n := strings.Count(out, `WARN: tenant=tx references unknown profile "nosuch", ignoring`); n != 1 {
		t.Errorf("unknown-profile WARN reached the caller's logger %d times, want 1:\n%s", n, out)
	}
	for _, k := range []string{"oracle_wait", "redis_x"} {
		if n := strings.Count(out, `WARN: profile "strict" supplies "`+k+`", but that key is declared without a platform value`); n != 1 {
			t.Errorf("declared-key WARN for %s reached the caller's logger %d times, want 1:\n%s", k, n, out)
		}
	}
	if global.Len() != 0 {
		t.Errorf("LoadDir with a logger also wrote to the process log:\n%s", global.String())
	}
}

// da-guard names a tenant electing an unknown profile only through this line
// on its stderr (the process log); ScopeEffective keeps writing it there.
func TestScopeEffectiveKeepsProfileWarnsOnTheProcessLog(t *testing.T) {
	dir := t.TempDir()
	writeMergeTree(t, dir, profileLogTree)
	global := captureGlobalLog(t)
	if _, err := ScopeEffective(dir, dir); err != nil {
		t.Fatal(err)
	}
	if n := strings.Count(global.String(), `WARN: tenant=tx references unknown profile "nosuch", ignoring`); n != 1 {
		t.Errorf("ScopeEffective wrote the unknown-profile WARN %d times, want 1:\n%s", n, global.String())
	}
}
