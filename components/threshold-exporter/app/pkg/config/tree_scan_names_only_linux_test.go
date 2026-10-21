//go:build linux

package config

import (
	"os"
	"path/filepath"
	"reflect"
	"slices"
	"testing"
)

// TestScanDirTreeNames_OpensButNeverReads is the direct proof that the
// names-only walk reads no byte: `/proc/self/mem` is a regular file the
// process may open, whose read at offset 0 fails (EIO). The reading walk
// records it as read_error; the names-only walk opens it, closes it and keeps
// it — the one documented difference between the two (ScanDirTreeNames).
func TestScanDirTreeNames_OpensButNeverReads(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "t1.yaml"), []byte("tenants: {t1: {}}\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink("/proc/self/mem", filepath.Join(root, "mem.yaml")); err != nil {
		t.Skipf("symlink: %v", err)
	}
	files, err := ScanDirTreeFiles(root, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	want := []UnreadableFile{{RelKey: "mem.yaml", Reason: UnreadableReadError}}
	if !reflect.DeepEqual(files.Unreadable, want) {
		t.Skipf("this kernel does not fail the read of /proc/self/mem the way the test needs: %+v", files.Unreadable)
	}
	names, err := ScanDirTreeNames(root, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	if names.Unreadable != nil || !slices.Contains(names.Keys, "mem.yaml") {
		t.Errorf("names-only walk: unreadable %+v, keys %v — it read mem.yaml", names.Unreadable, names.Keys)
	}
}
