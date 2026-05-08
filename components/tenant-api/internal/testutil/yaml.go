// Package testutil provides shared test helpers for the tenant-api Go module.
//
// This package collapses repeated test-setup boilerplate (R3 in the audit
// — Refactor sweep). The most common patterns it replaces are:
//
//  1. The 4-line "write YAML or Fatal" idiom:
//
//     err := os.WriteFile(filepath.Join(dir, "_views.yaml"), []byte(yaml), 0644)
//     if err != nil { t.Fatalf("write: %v", err) }
//
//     becomes one line via WriteYAML(t, dir, "_views.yaml", yaml).
//
//  2. The "MkTempDir + write one file" pattern that opens many tests:
//
//     dir := t.TempDir()
//     err := os.WriteFile(filepath.Join(dir, "_views.yaml"), …)
//     if err != nil { t.Fatalf(…) }
//
//     becomes dir, _ := MkTempYAML(t, "_views.yaml", yaml).
//
// All helpers take testing.TB so they work for both *testing.T and
// *testing.B. They call t.Helper() so test-failure line numbers point to
// the caller, not into this file.
package testutil

import (
	"os"
	"path/filepath"
	"testing"
)

// WriteYAML writes content to dir/name with mode 0644. Calls t.Fatalf on
// error. Returns the full path so the caller can chain into Read/Stat
// without re-joining.
//
//	path := testutil.WriteYAML(t, dir, "_views.yaml", yamlBody)
func WriteYAML(t testing.TB, dir, name, content string) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatalf("WriteYAML(%q): %v", name, err)
	}
	return path
}

// MkTempYAML creates a t.TempDir(), writes name=content into it, and
// returns (dir, fullPath). t.TempDir() is auto-cleaned by the testing
// framework — no need for t.Cleanup.
//
// Use this when a test needs exactly one YAML file in a fresh dir;
// otherwise call t.TempDir() yourself and use WriteYAML for each file.
func MkTempYAML(t testing.TB, name, content string) (dir, path string) {
	t.Helper()
	dir = t.TempDir()
	path = WriteYAML(t, dir, name, content)
	return dir, path
}

// WriteFile writes content to an absolute path, creating parent directories
// as needed. Returns the path. Use this when the caller already has a
// pre-computed deep path (e.g. `dir/sub/tenant-x.yaml`) instead of
// (dir, name) — common in hierarchy / nested-fixture tests.
func WriteFile(t testing.TB, path, content string) string {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		t.Fatalf("WriteFile mkdir %q: %v", filepath.Dir(path), err)
	}
	if err := os.WriteFile(path, []byte(content), 0644); err != nil {
		t.Fatalf("WriteFile(%q): %v", path, err)
	}
	return path
}
