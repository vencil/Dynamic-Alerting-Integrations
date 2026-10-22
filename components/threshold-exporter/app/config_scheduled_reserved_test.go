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
