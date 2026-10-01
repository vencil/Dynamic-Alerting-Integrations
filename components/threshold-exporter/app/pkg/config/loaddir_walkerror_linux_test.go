//go:build linux

package config

// loaddir_walkerror_linux_test.go — #2115: a walk error on a directory below
// the root is recorded as walk_error, measured with a shape that fails for
// root too (chmod does not stop root): a directory whose absolute path is
// longer than PATH_MAX, so opening it to list it fails with ENAMETOOLONG.
// The chain is built with mkdirat relative to an open parent, the only way
// to create a path the kernel will not resolve whole. Linux only (the
// syscall, and PATH_MAX = 4096).
//
// Seams: none — t.TempDir() tree (os.RemoveAll removes deep trees via
// unlinkat, so the cleanup works).

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
)

const linuxPathMax = 4096

// deepDirChain creates under root a chain of directories, each segment named
// seg, until the absolute path passes PATH_MAX. It returns the root-relative
// slash path of the FIRST directory whose absolute path is too long — the
// one the walker cannot open.
func deepDirChain(t *testing.T, root string) string {
	t.Helper()
	seg := strings.Repeat("d", 200)
	fd, err := syscall.Open(root, syscall.O_RDONLY|syscall.O_DIRECTORY, 0)
	if err != nil {
		t.Fatal(err)
	}
	abs := root
	var rel []string
	tooLong := ""
	for tooLong == "" {
		if err := syscall.Mkdirat(fd, seg, 0o755); err != nil {
			_ = syscall.Close(fd)
			t.Fatalf("mkdirat at depth %d: %v", len(rel), err)
		}
		next, err := syscall.Openat(fd, seg, syscall.O_RDONLY|syscall.O_DIRECTORY, 0)
		_ = syscall.Close(fd)
		if err != nil {
			t.Fatalf("openat at depth %d: %v", len(rel), err)
		}
		fd = next
		abs += "/" + seg
		rel = append(rel, seg)
		if len(abs) >= linuxPathMax {
			tooLong = strings.Join(rel, "/")
		}
	}
	_ = syscall.Close(fd)
	// Precondition: the kernel really refuses the path (else this test
	// would pass without measuring anything).
	if _, err := os.ReadDir(filepath.Join(root, filepath.FromSlash(tooLong))); !errors.Is(err, syscall.ENAMETOOLONG) {
		t.Fatalf("precondition: listing the deep dir must fail with ENAMETOOLONG, got %v", err)
	}
	return tooLong
}

func TestLoadDirReport_UnlistableDeepDirIsWalkError(t *testing.T) {
	t.Parallel()
	dir := writeUnreadableTree(t)
	want := deepDirChain(t, AbsScanRoot(dir))
	cfg, rep, logged := loadReport(t, dir)
	wantRep := []UnreadableFile{{RelKey: want, Reason: UnreadableWalkError}}
	if !reflect.DeepEqual(rep.Unreadable, wantRep) {
		t.Errorf("Unreadable = %v, want [{<deep dir> %s}]", rep.Unreadable, UnreadableWalkError)
	}
	if _, ok := cfg.Tenants["tenant-a"]; !ok {
		t.Errorf("tenant-a missing: the rest of the tree must still load")
	}
	if !strings.Contains(logged, "WARN: walk error at") {
		t.Errorf("walk-error WARN missing from log (unchanged behaviour): %.200q", logged)
	}
}
