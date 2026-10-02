package config

// root_unreadable_test.go — #2627: a --config-dir or --scope that cannot be
// read is named in Unreadable (configDir as RootUnreadable /
// RootStatUnreadable, with RootListErr), a wrong path is an error
// (StatErrIsWrongPath), a scope that resolves through a symlink outside
// configDir is "outside configDir" even when the target cannot be read, and
// every "no .yaml files found" refusal wraps ErrNoYAMLFiles.
//
// Every shape that needs a permission bit is skipped as root (chmod does not
// stop root); CI runs them as non-root. Seams: none — t.TempDir() trees.

import (
	"bytes"
	"errors"
	"io/fs"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"syscall"
	"testing"
)

func skipIfRoot(t *testing.T) {
	t.Helper()
	if os.Geteuid() == 0 {
		t.Skip("running as root: chmod does not stop root")
	}
}

func chmodRestore(t *testing.T, p string, mode, restore os.FileMode) {
	t.Helper()
	if err := os.Chmod(p, mode); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(p, restore) })
}

func writeFileT(t *testing.T, p, body string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

const rootUnreadableTenant = "tenants:\n  tenant-t:\n    mysql_connections: 70\n"

func TestErrNoYAMLFiles_WrappedByEveryRefusal(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	if _, _, err := LoadDirReport(dir, nil); !errors.Is(err, ErrNoYAMLFiles) {
		t.Errorf("LoadDirReport: err = %v, want ErrNoYAMLFiles", err)
	}
	if _, err := EffectiveTree(dir); !errors.Is(err, ErrNoYAMLFiles) {
		t.Errorf("EffectiveTree: err = %v, want ErrNoYAMLFiles", err)
	}
	if _, err := BuildFlatConfig(&TreeScan{}, FlatBuildInput{Root: dir}); !errors.Is(err, ErrNoYAMLFiles) {
		t.Errorf("BuildFlatConfig: err = %v, want ErrNoYAMLFiles", err)
	}
	// The message is the one operators know.
	if _, err := EffectiveTree(dir); err == nil || !strings.HasPrefix(err.Error(), "no .yaml files found in ") {
		t.Errorf("EffectiveTree message = %v", err)
	}
}

func TestStatErrIsWrongPath(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFileT(t, filepath.Join(dir, "f"), "x")
	loop := filepath.Join(dir, "loop")
	symlinkOrSkip(t, "loop", loop)
	stat := func(p string) error { _, err := os.Stat(p); return err }
	for name, c := range map[string]struct {
		err   error
		wrong bool
	}{
		"ENOENT":  {stat(filepath.Join(dir, "nope")), true},
		"ENOTDIR": {stat(filepath.Join(dir, "f", "x")), true},
		"ELOOP":   {stat(loop), true},
		"EACCES":  {&fs.PathError{Op: "stat", Path: "p", Err: syscall.EACCES}, false},
		"EIO":     {&fs.PathError{Op: "stat", Path: "p", Err: syscall.EIO}, false},
	} {
		if c.err == nil {
			t.Fatalf("%s: precondition: stat did not fail", name)
		}
		if got := StatErrIsWrongPath(c.err); got != c.wrong {
			t.Errorf("%s (%v): StatErrIsWrongPath = %v, want %v", name, c.err, got, c.wrong)
		}
	}
}

// A configDir the walk cannot list: RootUnreadable + RootListErr from both
// readers, and LoadDirReport does not log the walker's WARN for the root
// (its RootListErr carries the same reason).
func TestRootUnlistable_NamedAsRootUnreadable(t *testing.T) {
	t.Parallel()
	skipIfRoot(t)
	dir := t.TempDir()
	writeFileT(t, filepath.Join(dir, "t.yaml"), rootUnreadableTenant)
	chmodRestore(t, dir, 0, 0o755)

	for name, f := range map[string]func() (*ScopedTenants, error){
		"ScopeEffective":       func() (*ScopedTenants, error) { return ScopeEffective(dir, "") },
		"ScopeEffective scope": func() (*ScopedTenants, error) { return ScopeEffective(dir, "sub") },
		"EffectiveTree":        func() (*ScopedTenants, error) { return EffectiveTree(dir) },
	} {
		st, err := f()
		if err != nil {
			t.Fatalf("%s: err = %v, want a result", name, err)
		}
		if len(st.Tenants) != 0 || st.Unreadable[0] != RootUnreadable {
			t.Errorf("%s: tenants = %d unreadable = %v, want none and %v first", name, len(st.Tenants), st.Unreadable, RootUnreadable)
		}
		if st.RootListErr == nil || !strings.Contains(st.RootListErr.Error(), "cannot list configDir") ||
			!errors.Is(st.RootListErr, fs.ErrPermission) {
			t.Errorf("%s: RootListErr = %v", name, st.RootListErr)
		}
	}

	var buf bytes.Buffer
	_, rep, err := LoadDirReport(dir, log.New(&buf, "", 0))
	if !errors.Is(err, ErrNoYAMLFiles) || !errors.Is(err, fs.ErrPermission) {
		t.Errorf("LoadDirReport err = %v, want ErrNoYAMLFiles wrapping permission denied", err)
	}
	if !reflect.DeepEqual(rep.Unreadable, []UnreadableFile{RootUnreadable}) || rep.RootListErr == nil {
		t.Errorf("LoadDirReport report = %+v", rep)
	}
	if strings.Contains(buf.String(), "walk error at "+AbsScanRoot(dir)+":") {
		t.Errorf("LoadDirReport logged the root's walk error beside RootListErr: %q", buf.String())
	}
}

// A configDir the process may not even stat (under a directory it may not
// search): RootStatUnreadable, not an error.
func TestRootStatDenied_NamedAsRootStatUnreadable(t *testing.T) {
	t.Parallel()
	skipIfRoot(t)
	parent := t.TempDir()
	dir := filepath.Join(parent, "conf.d")
	writeFileT(t, filepath.Join(dir, "t.yaml"), rootUnreadableTenant)
	chmodRestore(t, parent, 0, 0o755)

	for name, f := range map[string]func() (*ScopedTenants, error){
		"ScopeEffective": func() (*ScopedTenants, error) { return ScopeEffective(dir, "") },
		"EffectiveTree":  func() (*ScopedTenants, error) { return EffectiveTree(dir) },
	} {
		st, err := f()
		if err != nil {
			t.Fatalf("%s: err = %v, want a result", name, err)
		}
		if !reflect.DeepEqual(st.Unreadable, []UnreadableFile{RootStatUnreadable}) || st.RootListErr == nil ||
			!strings.Contains(st.RootListErr.Error(), "stat configDir") {
			t.Errorf("%s: unreadable = %v RootListErr = %v", name, st.Unreadable, st.RootListErr)
		}
	}
	_, rep, err := LoadDirReport(dir, nil)
	if !errors.Is(err, ErrNoYAMLFiles) || !reflect.DeepEqual(rep.Unreadable, []UnreadableFile{RootStatUnreadable}) || rep.RootListErr == nil {
		t.Errorf("LoadDirReport: err = %v report = %+v", err, rep)
	}
	// A configDir that does not exist stays an error.
	if _, err := ScopeEffective(filepath.Join(t.TempDir(), "nope"), ""); err == nil {
		t.Error("ScopeEffective on a missing configDir: no error")
	}
}

// A scope the process may not stat is named with the directory that hides
// it; a scope that does not exist is an error.
func TestScopeStatDenied_Unreadable(t *testing.T) {
	t.Parallel()
	skipIfRoot(t)
	dir := t.TempDir()
	writeFileT(t, filepath.Join(dir, "t.yaml"), rootUnreadableTenant)
	writeFileT(t, filepath.Join(dir, "locked", "inner", "t.yaml"), "tenants:\n  tenant-l: {}\n")
	chmodRestore(t, filepath.Join(dir, "locked"), 0, 0o755)

	st, err := ScopeEffective(dir, "locked/inner")
	if err != nil {
		t.Fatalf("err = %v, want a result", err)
	}
	want := []UnreadableFile{{"locked", UnreadableWalkError}, {"locked/inner", UnreadableStatError}}
	if !reflect.DeepEqual(st.Unreadable, want) || len(st.Tenants) != 0 {
		t.Errorf("unreadable = %v tenants = %d, want %v and none", st.Unreadable, len(st.Tenants), want)
	}
	if _, err := ScopeEffective(dir, "nope"); err == nil || !strings.Contains(err.Error(), "stat scopeDir") {
		t.Errorf("missing scope: err = %v, want stat scopeDir", err)
	}
}

// A scope through a symlink: one that resolves outside configDir is
// "outside configDir" even when its target cannot be read (the exporter
// never follows a directory symlink, so a/b is no path it reads); one to a
// directory inside the tree resolves to it, as a fully resolvable link does.
func TestScopeThroughSymlink_OutsideStaysOutside(t *testing.T) {
	t.Parallel()
	skipIfRoot(t)
	base := t.TempDir()
	dir := filepath.Join(base, "conf.d")
	writeFileT(t, filepath.Join(dir, "t.yaml"), rootUnreadableTenant)
	writeFileT(t, filepath.Join(dir, "locked", "b", "t.yaml"), "tenants:\n  tenant-l: {}\n")
	writeFileT(t, filepath.Join(base, "outside", "locked", "b", "t.yaml"), "tenants:\n  tenant-o: {}\n")
	symlinkOrSkip(t, filepath.Join(base, "outside", "locked"), filepath.Join(dir, "a"))
	symlinkOrSkip(t, filepath.Join(dir, "locked"), filepath.Join(dir, "in"))
	chmodRestore(t, filepath.Join(base, "outside", "locked"), 0, 0o755)
	chmodRestore(t, filepath.Join(dir, "locked"), 0, 0o755)

	for _, scope := range []string{"a", "a/b"} {
		if _, err := ScopeEffective(dir, scope); err == nil || !strings.Contains(err.Error(), "is outside configDir") {
			t.Errorf("--scope %s: err = %v, want outside configDir", scope, err)
		}
	}
	for scope, want := range map[string][]UnreadableFile{
		"in":   {{"locked", UnreadableWalkError}},
		"in/b": {{"locked", UnreadableWalkError}, {"locked/b", UnreadableStatError}},
	} {
		st, err := ScopeEffective(dir, scope)
		if err != nil {
			t.Errorf("--scope %s: err = %v, want a result", scope, err)
			continue
		}
		if !reflect.DeepEqual(st.Unreadable, want) {
			t.Errorf("--scope %s: unreadable = %v, want %v", scope, st.Unreadable, want)
		}
	}
}

// Windows never returns ENOTDIR or ELOOP from stat; its malformed-path codes
// are the wrong-path set there (stat_classify.go). Tested here on any
// platform through the pure classifier.
func TestStatErrIsWrongPath_WindowsErrnos(t *testing.T) {
	t.Parallel()
	pathErr := func(e syscall.Errno) error { return &fs.PathError{Op: "stat", Path: "p", Err: e} }
	for _, c := range []struct {
		name  string
		errno syscall.Errno
		wrong bool
	}{
		{"ERROR_INVALID_NAME", 123, true},
		{"ERROR_BAD_PATHNAME", 161, true},
		{"ERROR_DIRECTORY", 267, true},
		{"ERROR_CANT_RESOLVE_FILENAME", 1921, true},
		{"ERROR_ACCESS_DENIED", 5, false},
		{"ERROR_SHARING_VIOLATION", 32, false},
		{"ENOTDIR is not Windows' code", syscall.ENOTDIR, false},
	} {
		if got := statErrIsWrongPath(pathErr(c.errno), windowsWrongPathErrnos); got != c.wrong {
			t.Errorf("%s (%d): wrong path = %v, want %v", c.name, c.errno, got, c.wrong)
		}
	}
	if !statErrIsWrongPath(fs.ErrNotExist, windowsWrongPathErrnos) {
		t.Error("fs.ErrNotExist is a wrong path on every platform")
	}
}

// The root's walk WARN is dropped only when the caller reports the same
// reason (RootListErr); otherwise it is logged, through the caller's logger
// (prefix and flags), after the walk's other lines (#2627 review).
func TestRootWarnFilter_DropsOnlyWhenReported(t *testing.T) {
	t.Parallel()
	for _, reported := range []bool{true, false} {
		var buf bytes.Buffer
		lg, f := withRootWalkWarnHeld(log.New(&buf, "P ", 0), "/r")
		lg.Printf("WARN: walk error at /r: boom")
		lg.Printf("WARN: walk error at /r/sub: other")
		f.release(reported)
		want := "P WARN: walk error at /r/sub: other\n"
		if !reported {
			want += "P WARN: walk error at /r: boom\n"
		}
		if buf.String() != want {
			t.Errorf("reported=%v: logged %q, want %q", reported, buf.String(), want)
		}
	}
	// The discard logger needs no filter; release is still safe.
	lg, f := withRootWalkWarnHeld(discardLogger, "/r")
	lg.Printf("WARN: walk error at /r: boom")
	f.release(false)
}
