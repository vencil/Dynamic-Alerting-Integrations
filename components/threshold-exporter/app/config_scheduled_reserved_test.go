package main

// #3269: the load WARNs about a reserved key written with a schedule — the
// decode kept only its `default` — naming file, tenant and key. What is
// served is unchanged (the readers read the default alone before as after).

import (
	"log"
	"path/filepath"
	"strings"
	"testing"
)

const scheduledSilentMode = "    _silent_mode: {default: disable, overrides: [{window: \"03:00-04:00\", value: all}]}\n"

func TestLoad_DirMode_WarnsScheduledReserved(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  key_0: 10\n")
	writeTestYAML(t, filepath.Join(dir, "a.yaml"), "tenants:\n  t-a:\n"+scheduledSilentMode)
	writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  t-b:\n    key_0: \"1\"\n")

	var buf syncBuffer
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetLogger(log.New(&buf, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	want := "WARN: 1 config entries write a reserved key with a time window or expires"
	if got := buf.String(); !strings.Contains(got, want) || !strings.Contains(got, "a.yaml: tenants.t-a._silent_mode") {
		t.Fatalf("log = %q, want %q naming a.yaml: tenants.t-a._silent_mode", got, want)
	}
	if sv := m.GetConfig().Tenants["t-a"]["_silent_mode"]; sv.Default != "disable" || sv.Overrides != nil {
		t.Errorf("committed _silent_mode = %+v, want the default alone", sv)
	}

	// An unchanged file is still named when another one reloads
	// (incrementalLoadFrom reuses its cached partial).
	writeTestYAML(t, filepath.Join(dir, "b.yaml"), "tenants:\n  t-b:\n    key_0: \"2\"\n")
	before := len(buf.String())
	if err := watchReload(m); err != nil {
		t.Fatalf("reload: %v", err)
	}
	if got := buf.String()[before:]; !strings.Contains(got, "a.yaml: tenants.t-a._silent_mode") {
		t.Errorf("reload log = %q, want the entry again", got)
	}
}

func TestLoad_SingleFile_WarnsScheduledReserved(t *testing.T) {
	t.Parallel()
	path := filepath.Join(t.TempDir(), "config.yaml")
	writeTestYAML(t, path, "tenants:\n  t-a:\n"+scheduledSilentMode+"  t-b:\n    _silent_mode: disable\n")
	var buf syncBuffer
	m := NewConfigManagerWithDebounce(path, 0)
	m.SetLogger(log.New(&buf, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got := buf.String(); !strings.Contains(got, "WARN: 1 config entries write a reserved key") ||
		!strings.Contains(got, ": tenants.t-a._silent_mode —") {
		t.Errorf("log = %q", got)
	}
}

// A subtree `_defaults.yaml` is named too — same judgement, "<file>:
// defaults.<key>" — on the cold load and on a reload that changes only a
// tenant file, worded by what the overlay does with the value (the verdict
// applySubtreeDefaults acts on): since #3341 a subtree hands no reserved key
// down, so every such entry is "not applied at all" — a windowed `_state_`
// key whose default is `disable` among them (until #3341 that one was used
// as its `default` alone, #3295).
func TestLoad_DirMode_WarnsSubtreeScheduledReserved(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  key_0: 10\n"+
		"state_filters:\n  maint:\n    reasons: [X]\n    severity: critical\n")
	writeTestYAML(t, filepath.Join(dir, "sub", "_defaults.yaml"), "defaults:\n"+
		"  _severity_dedup: {default: disable, overrides: [{window: \"03:00-04:00\", value: enable}]}\n"+
		"  _silent_mode: {default: disable, expires: \"2099-01-01T00:00:00Z\"}\n"+
		"  _metadata: {default: x, owner: team-a}\n")
	writeTestYAML(t, filepath.Join(dir, "sub", "t-a.yaml"), "tenants:\n  t-a:\n    key_0: \"1\"\n")
	writeTestYAML(t, filepath.Join(dir, "other", "_defaults.yaml"), "defaults:\n"+
		"  _silent_mode: {default: warning, overrides: [{window: \"03:00-04:00\", value: all}]}\n"+
		"  _state_maint: {default: disable, overrides: [{window: \"03:00-04:00\", value: enable}]}\n")
	writeTestYAML(t, filepath.Join(dir, "other", "t-b.yaml"), "tenants:\n  t-b:\n    key_0: \"1\"\n")

	var buf syncBuffer
	m := NewConfigManagerWithDebounce(dir, 0)
	m.SetLogger(log.New(&buf, "", 0))
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	unapplied := "WARN: 4 subtree defaults entries write a reserved key with a time window or expires (`overrides:` / `expires:` / `reason:`, #3269): " +
		"other/_defaults.yaml: defaults._silent_mode, other/_defaults.yaml: defaults._state_maint, " +
		"sub/_defaults.yaml: defaults._severity_dedup, sub/_defaults.yaml: defaults._silent_mode — not applied at all: " +
		"a subtree `_defaults.yaml` does not hand this value down to its tenants, `default` included. Remove them"
	if got := buf.String(); !strings.Contains(got, unapplied) {
		t.Fatalf("log = %q, want %q", got, unapplied)
	}
	if got := buf.String(); strings.Contains(got, "config entries write a reserved key") {
		t.Errorf("log names an entry as used: %q", got)
	}
	// What the WARN says is what was served.
	cfg := m.GetConfig()
	if sv, in := cfg.Tenants["t-a"]["_severity_dedup"]; in {
		t.Errorf("t-a _severity_dedup = %+v, want not applied (#3341)", sv)
	}
	if sv, in := cfg.Tenants["t-b"]["_silent_mode"]; in {
		t.Errorf("t-b _silent_mode = %+v, want not applied", sv)
	}
	if sv, in := cfg.Tenants["t-b"]["_state_maint"]; in {
		t.Errorf("t-b _state_maint = %+v, want not applied (#3341)", sv)
	}

	writeTestYAML(t, filepath.Join(dir, "sub", "t-a.yaml"), "tenants:\n  t-a:\n    key_0: \"2\"\n")
	before := len(buf.String())
	if err := watchReload(m); err != nil {
		t.Fatalf("reload: %v", err)
	}
	if got := buf.String()[before:]; !strings.Contains(got, unapplied) {
		t.Errorf("reload log = %q, want %q", got, unapplied)
	}
}
