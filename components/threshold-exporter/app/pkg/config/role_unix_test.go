//go:build unix

package config

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"syscall"
	"testing"
)

// TestStatErrorFileMissingOfEachErrno: the walk's record of a failed stat is
// Missing (not_a_file) for exactly the errors that say the walk cannot reach
// a file there — ENOENT, ENOTDIR, ELOOP — and config for every other: a
// permission refusal, EIO, ENAMETOOLONG. EIO cannot be made on a real
// filesystem here, so the mapping is pinned on the error itself.
func TestStatErrorFileMissingOfEachErrno(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		err  error
		want Role
	}{
		{&fs.PathError{Op: "stat", Path: "x", Err: syscall.ENOENT}, RoleNotAFile},
		{&fs.PathError{Op: "stat", Path: "x", Err: syscall.ENOTDIR}, RoleNotAFile},
		{&fs.PathError{Op: "stat", Path: "x", Err: syscall.ELOOP}, RoleNotAFile},
		{&fs.PathError{Op: "stat", Path: "x", Err: syscall.EACCES}, RoleConfig},
		{&fs.PathError{Op: "stat", Path: "x", Err: syscall.EPERM}, RoleConfig},
		{&fs.PathError{Op: "stat", Path: "x", Err: syscall.EIO}, RoleConfig},
		{&fs.PathError{Op: "stat", Path: "x", Err: syscall.ENAMETOOLONG}, RoleConfig},
		{errors.New("an error nobody named"), RoleConfig},
	} {
		u := statErrorFile("x.yaml", tc.err)
		if u.Reason != UnreadableStatError || u.Role() != tc.want {
			t.Errorf("%v: %+v role %q, want stat_error role %q", tc.err, u, u.Role(), tc.want)
		}
	}
}

// TestScanRecordsWhetherAStatFoundNothing: the walk (both modes) records a
// dangling link, a looping link and a chain of 41 links — ELOOP although
// the chain ends at a regular file, which the exporter's own os.Stat cannot
// reach either — as Missing (not_a_file); a link whose target name is too
// long (ENAMETOOLONG) as a config file whose read fails; a chain of 39 links
// as the file it reaches. Runs as any uid; the permission cases are in
// TestRoleUnderRealModes.
func TestScanRecordsWhetherAStatFoundNothing(t *testing.T) {
	t.Parallel()
	conf := t.TempDir()
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatalf("cannot build the fixture: %v", err)
		}
	}
	must(os.WriteFile(filepath.Join(conf, "a.yaml"), []byte("tenants: {a: {}}\n"), 0o644))
	must(os.Symlink("nowhere.yaml", filepath.Join(conf, "gone.yaml")))
	must(os.Symlink("loop-b.yaml", filepath.Join(conf, "loop-a.yaml")))
	must(os.Symlink("loop-a.yaml", filepath.Join(conf, "loop-b.yaml")))
	must(os.Symlink(strings.Repeat("n", 300)+".yaml", filepath.Join(conf, "long.yaml")))
	// chain-N.yaml → c<N-1> → … → c1 → real: N links to a regular file.
	chain := filepath.Join(conf, "links")
	must(os.Mkdir(chain, 0o755))
	must(os.WriteFile(filepath.Join(chain, "real"), []byte("tenants: {deep: {}}\n"), 0o644))
	prev := filepath.Join(chain, "real")
	for i := 1; i <= 40; i++ {
		link := filepath.Join(chain, "c"+strconv.Itoa(i))
		must(os.Symlink(prev, link))
		prev = link
		if i == 38 {
			must(os.Symlink(link, filepath.Join(conf, "chain-39.yaml")))
		}
	}
	must(os.Symlink(prev, filepath.Join(conf, "chain-41.yaml")))
	if _, err := os.Stat(filepath.Join(conf, "chain-41.yaml")); !errors.Is(err, syscall.ELOOP) {
		t.Fatalf("fixture: os.Stat of a 41-link chain = %v, want ELOOP (the exporter cannot reach it either)", err)
	}
	if _, err := os.Stat(filepath.Join(conf, "long.yaml")); !errors.Is(err, syscall.ENAMETOOLONG) {
		t.Fatalf("fixture: os.Stat of long.yaml = %v, want ENAMETOOLONG", err)
	}
	for _, scanFn := range []func(string, *log.Logger) (*TreeScan, error){ScanDirTreeNames, ScanDirTreeFiles} {
		scan, err := scanFn(conf, log.New(io.Discard, "", 0))
		if err != nil {
			t.Fatal(err)
		}
		got := map[string]Role{}
		for _, u := range scan.Unreadable {
			if u.Reason != UnreadableStatError {
				t.Errorf("%s: %+v, want stat_error", u.RelKey, u)
			}
			got[u.RelKey] = u.Role()
		}
		want := map[string]Role{"gone.yaml": RoleNotAFile, "loop-a.yaml": RoleNotAFile,
			"loop-b.yaml": RoleNotAFile, "chain-41.yaml": RoleNotAFile, "long.yaml": RoleConfig}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("roles of the unreadable entries\n got  %v\n want %v", got, want)
		}
		if _, ok := scan.Files["chain-39.yaml"]; !ok {
			t.Errorf("chain-39.yaml: not kept as a file (keys %v)", scan.Keys)
		}
	}
}

// Running TestRoleUnderRealModes where a mode bit really denies.
const (
	// roleUnprivChildEnv marks the re-executed test binary (run as nobody).
	roleUnprivChildEnv = "VIBE_ROLE_UNPRIV_CHILD"
	// requireUnprivEnv, when set (CI), makes a missing setup a failure
	// rather than a skip — as VIBE_REQUIRE_HELM does for the helm-gated
	// tests: without it, a uid-0 run would leave EACCES unmeasured.
	requireUnprivEnv = "VIBE_REQUIRE_UNPRIVILEGED_FS"
)

// skipOrFail skips, or fails when requireUnprivEnv is set.
func skipOrFail(t *testing.T, why string) {
	t.Helper()
	if os.Getenv(requireUnprivEnv) != "" {
		t.Fatalf("%s is set, but %s: the permission cases of the role verdict would go unmeasured", requireUnprivEnv, why)
	}
	t.Skip(why)
}

// rerunAsNobody re-executes this test binary, running only test `name`, as
// uid/gid 65534 (setpriv), and fails when that run does not pass. The binary
// is copied into a 0755 directory under /var/tmp: the go build directory is
// 0700 and nobody could not run it from there.
func rerunAsNobody(t *testing.T, name string) {
	t.Helper()
	setpriv, err := exec.LookPath("setpriv")
	if err != nil {
		skipOrFail(t, "running as uid 0 and `setpriv` is not installed")
		return
	}
	exe, err := os.Executable()
	if err != nil {
		skipOrFail(t, fmt.Sprintf("cannot find the test binary: %v", err))
		return
	}
	box, err := os.MkdirTemp("/var/tmp", "vibe-role-unpriv-")
	if err != nil {
		skipOrFail(t, fmt.Sprintf("cannot make a directory under /var/tmp: %v", err))
		return
	}
	t.Cleanup(func() { _ = os.RemoveAll(box) })
	tmp := filepath.Join(box, "tmp")
	bin := filepath.Join(box, "config.test")
	src, err := os.ReadFile(exe) // #nosec G304 -- this test's own binary
	if err == nil {
		err = os.WriteFile(bin, src, 0o755) // #nosec G306 -- nobody must run it
	}
	if err == nil {
		err = os.Mkdir(tmp, 0o777)
	}
	if err == nil {
		err = os.Chmod(tmp, 0o1777)
	}
	if err == nil {
		err = os.Chmod(box, 0o755)
	}
	if err != nil {
		t.Fatalf("cannot prepare the unprivileged run: %v", err)
	}
	cmd := exec.Command(setpriv, "--reuid=65534", "--regid=65534", "--clear-groups", // #nosec G204 -- fixed argv
		bin, "-test.run=^"+name+"$", "-test.count=1", "-test.v")
	cmd.Dir = tmp
	cmd.Env = append(os.Environ(), roleUnprivChildEnv+"=1", "TMPDIR="+tmp, "HOME="+tmp)
	out, err := cmd.CombinedOutput()
	if err != nil || !strings.Contains(string(out), "--- PASS: "+name) {
		t.Fatalf("%s as uid 65534: %v\n%s", name, err, out)
	}
}

// TestRoleUnderRealModes measures the whitelist where the permission bits
// really deny (as uid 65534 when this process is uid 0): a link into a
// directory nobody may search (stat EACCES), an entry of a directory that
// may be listed but not searched (lstat EACCES) and a mode-000 file (open
// refused) are config files whose read fails — never not_a_file; a
// directory that cannot be listed is unlistable_dir; a dangling link, the
// control, is not_a_file. A walk that took any stat failure for "no file"
// would let those tenant files vanish (#3206).
func TestRoleUnderRealModes(t *testing.T) {
	if os.Geteuid() == 0 {
		if os.Getenv(roleUnprivChildEnv) != "" {
			t.Fatal("the re-executed run is still uid 0")
		}
		rerunAsNobody(t, "TestRoleUnderRealModes")
		return
	}
	root := t.TempDir()
	conf := filepath.Join(root, "conf.d")
	locked := filepath.Join(root, "locked")
	must := func(err error) {
		t.Helper()
		if err != nil {
			t.Fatalf("cannot build the fixture: %v", err)
		}
	}
	for _, d := range []string{conf, locked, filepath.Join(conf, "sealed"), filepath.Join(conf, "noexec")} {
		must(os.Mkdir(d, 0o755))
	}
	for p, body := range map[string]string{
		filepath.Join(conf, "ok.yaml"):          "tenants: {ok: {}}\n",
		filepath.Join(conf, "zero.yaml"):        "tenants: {zero: {}}\n",
		filepath.Join(locked, "real.yaml"):      "tenants: {b: {}}\n",
		filepath.Join(conf, "sealed", "s.yaml"): "tenants: {s: {}}\n",
		filepath.Join(conf, "noexec", "t.yaml"): "tenants: {t: {}}\n",
	} {
		must(os.WriteFile(p, []byte(body), 0o644))
	}
	must(os.Symlink(filepath.Join(locked, "real.yaml"), filepath.Join(conf, "b.yaml")))
	must(os.Symlink("nowhere.yaml", filepath.Join(conf, "gone.yaml")))
	modes := map[string]os.FileMode{
		filepath.Join(conf, "zero.yaml"): 0, locked: 0, filepath.Join(conf, "sealed"): 0,
		filepath.Join(conf, "noexec"): 0o444, // listable, not searchable
	}
	t.Cleanup(func() {
		for p := range modes {
			_ = os.Chmod(p, 0o755)
		}
	})
	for p, m := range modes {
		must(os.Chmod(p, m))
	}
	if _, err := os.Stat(filepath.Join(conf, "b.yaml")); !errors.Is(err, fs.ErrPermission) {
		t.Fatalf("fixture: stat b.yaml = %v, want EACCES (do the mode bits deny here?)", err)
	}
	for _, scanFn := range []func(string, *log.Logger) (*TreeScan, error){ScanDirTreeNames, ScanDirTreeFiles} {
		scan, err := scanFn(conf, log.New(io.Discard, "", 0))
		if err != nil {
			t.Fatal(err)
		}
		got := map[string]Role{}
		for _, u := range scan.Unreadable {
			got[u.RelKey] = u.Role()
		}
		want := map[string]Role{
			"b.yaml": RoleConfig, "noexec/t.yaml": RoleConfig, "zero.yaml": RoleConfig,
			"sealed": RoleUnlistableDir, "gone.yaml": RoleNotAFile,
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("roles\n got  %v\n want %v (unreadable %+v)", got, want, scan.Unreadable)
		}
		if _, ok := scan.Files["ok.yaml"]; !ok {
			t.Errorf("ok.yaml: not kept")
		}
	}
}
