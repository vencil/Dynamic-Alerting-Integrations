// Package testutil provides shared test helpers for the threshold-exporter
// Go module.
//
// Mirrors the tenant-api/internal/testutil package introduced in PR #313.
// Same shape, separate module — Go's internal/ visibility doesn't cross
// module boundaries, so each module needs its own copy.
//
// See tenant-api/internal/testutil/yaml.go for the full rationale (R3 in
// the audit). Quick summary: replaces the 4-line "WriteFile + Join +
// err-check" idiom with one line, calls t.Helper() so failure line
// numbers point to the caller.
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
//	path := testutil.WriteYAML(t, dir, "_defaults.yaml", body)
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
func MkTempYAML(t testing.TB, name, content string) (dir, path string) {
	t.Helper()
	dir = t.TempDir()
	path = WriteYAML(t, dir, name, content)
	return dir, path
}

// WriteYAMLBytes is a []byte variant of WriteYAML, for callers that have
// the content already as bytes (e.g. pre-computed `l0Bytes := []byte(...)`).
// Avoids round-trip through string for those paths.
func WriteYAMLBytes(t testing.TB, dir, name string, content []byte) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, content, 0644); err != nil {
		t.Fatalf("WriteYAMLBytes(%q): %v", name, err)
	}
	return path
}

// WriteFileMode is the mode-explicit variant of WriteYAML, for tests that
// need a specific permission bit (e.g. 0600 for security-sensitive
// fixtures). Most tests should prefer WriteYAML's 0644 default.
func WriteFileMode(t testing.TB, dir, name, content string, mode os.FileMode) string {
	t.Helper()
	path := filepath.Join(dir, name)
	if err := os.WriteFile(path, []byte(content), mode); err != nil {
		t.Fatalf("WriteFileMode(%q, %v): %v", name, mode, err)
	}
	return path
}

// WriteFile writes content to an absolute path, creating parent directories
// as needed. Returns the path. Use this when the caller already has a
// pre-computed deep path (e.g. `team-a/sub/tenant-x.yaml`) instead of
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
