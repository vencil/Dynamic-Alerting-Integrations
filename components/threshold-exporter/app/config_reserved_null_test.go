package main

// config_reserved_null_test.go — #3341: a tenant's null on a reserved key
// deletes the inherited value (the platform default is served) on the
// exporter's reload paths too. The incremental reload (patchTenants) keeps
// the published maps of the tenants it does not re-read and runs
// ApplyProfiles over them again; the null must still keep the profile's
// value out — which is why it is a marker in the map, not a key deleted
// after the build (pkg/config reservedNullMarker).
//
// Seams: none — a t.TempDir() tree, NewConfigManagerWithDebounce + watchReload.

import (
	"bytes"
	"log"
	"path/filepath"
	"strings"
	"testing"
)

func TestReload_TenantReservedNullKeepsTheProfileOut(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_profiles.yaml"),
		"profiles:\n  p1:\n    _silent_mode: warning\n")
	writeTestYAML(t, filepath.Join(dir, "ta.yaml"),
		"tenants:\n  ta:\n    _profile: p1\n    _silent_mode: ~\n    _severity_dedup:\n")
	writeTestYAML(t, filepath.Join(dir, "tb.yaml"), "tenants:\n  tb:\n    _profile: p1\n")

	var buf bytes.Buffer
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetLogger(log.New(&buf, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	check := func(when string) {
		t.Helper()
		cfg := m.GetConfig()
		silenced := map[string]bool{}
		for _, s := range cfg.ResolveSilentModesAt(fixedNow) {
			silenced[s.Tenant] = true
		}
		dedup := map[string]bool{}
		for _, d := range cfg.ResolveSeverityDedup() {
			dedup[d.Tenant] = true
		}
		// ta nulls both keys: the platform default — nothing silenced (its
		// null deletes the profile's warning), dedup on. tb takes the
		// profile's warning. (A profile's `_severity_dedup` is never filled
		// in, #2914, so ta's dedup null displaces nothing.)
		if silenced["ta"] || !dedup["ta"] {
			t.Errorf("%s: ta silenced=%v dedup=%v, want false / true", when, silenced["ta"], dedup["ta"])
		}
		if !silenced["tb"] {
			t.Errorf("%s: tb is not silenced, want the profile's warning", when)
		}
	}
	check("cold load")

	// A change to tb only: the incremental path, which re-runs ApplyProfiles
	// over ta's published map.
	writeTestYAML(t, filepath.Join(dir, "tb.yaml"), "tenants:\n  tb:\n    _profile: p1\n    mysql_connections: \"5\"\n")
	if err := watchReload(m); err != nil {
		t.Fatalf("reload: %v", err)
	}
	if !strings.Contains(buf.String(), "incremental") {
		t.Fatalf("precondition: the reload did not take the incremental path:\n%s", buf.String())
	}
	check("incremental reload")
}
