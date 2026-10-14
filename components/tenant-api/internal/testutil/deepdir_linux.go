//go:build linux

package testutil

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
)

// linuxPathMax is PATH_MAX on Linux: a path this long or longer is refused
// whole by the kernel (ENAMETOOLONG).
const linuxPathMax = 4096

// UnlistableDeepDir builds, under root, a chain of directories whose last one
// cannot be listed even by root: its absolute path is longer than PATH_MAX,
// so opening it to list it fails with ENAMETOOLONG — the walker records it
// as walk_error (chmod cannot build that shape under euid 0). It returns the
// root-relative slash path of that directory. Every directory above it
// leaves at least slack bytes below PATH_MAX, so a copy of the tree under a
// root up to slack bytes longer (a batch's staging worktree) can still be
// created. slack must be below 200.
//
// The chain is built with mkdirat relative to an open parent — the only way
// to create a path the kernel will not resolve whole — like the exporter's
// own fixture (pkg/config loaddir_walkerror_linux_test.go). t.TempDir's
// cleanup removes deep trees (os.RemoveAll uses unlinkat). Seams: none.
func UnlistableDeepDir(t testing.TB, root string, slack int) string {
	t.Helper()
	if slack <= 0 || slack >= 200 {
		t.Fatalf("UnlistableDeepDir: slack %d out of (0, 200)", slack)
	}
	abs, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	fd, err := syscall.Open(abs, syscall.O_RDONLY|syscall.O_DIRECTORY, 0)
	if err != nil {
		t.Fatal(err)
	}
	var rel []string
	push := func(seg string) {
		t.Helper()
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
	}
	seg := strings.Repeat("d", 200)
	last := linuxPathMax - 1 - slack // the deepest listable directory's length
	for len(abs)+1+len(seg) <= last {
		push(seg)
	}
	if gap := last - len(abs); gap >= 2 {
		push(strings.Repeat("e", gap-1))
	}
	push(seg) // past PATH_MAX: the unlistable one
	_ = syscall.Close(fd)
	tooLong := strings.Join(rel, "/")
	parent := filepath.Dir(filepath.Join(root, filepath.FromSlash(tooLong)))
	// Preconditions: the kernel really refuses the deep dir, and lists the
	// one above it (else the fixture measures nothing, or the wrong thing).
	if _, err := os.ReadDir(filepath.Join(root, filepath.FromSlash(tooLong))); !errors.Is(err, syscall.ENAMETOOLONG) {
		t.Fatalf("precondition: listing the deep dir must fail with ENAMETOOLONG, got %v", err)
	}
	if _, err := os.ReadDir(parent); err != nil {
		t.Fatalf("precondition: the directory above it must be listable: %v", err)
	}
	return tooLong
}

// ProcSelfMem is a path whose stat is a regular file and whose read fails
// (EIO at offset 0) even for root: a symlink to it named like a config file
// is the walker's read_error. It reports false when this process has no
// such file (callers skip rather than test nothing).
func ProcSelfMem() (string, bool) {
	const p = "/proc/self/mem"
	fi, err := os.Stat(p)
	if err != nil || !fi.Mode().IsRegular() {
		return "", false
	}
	if _, err := os.ReadFile(p); err == nil {
		return "", false
	}
	return p, true
}
