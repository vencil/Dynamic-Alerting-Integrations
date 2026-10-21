package testutil

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// Running a test where a mode bit really denies (#3206). Under uid 0 a
// chmod denies nothing, so such a test re-executes its own binary as uid
// 65534 (setpriv) and runs there.
const (
	// UnprivChildEnv marks the re-executed test binary (run as nobody).
	UnprivChildEnv = "VIBE_UNPRIV_CHILD"
	// RequireUnprivEnv, when set (CI), makes a missing setup a failure
	// rather than a skip — as VIBE_REQUIRE_HELM does for the helm-gated
	// tests: without it, a uid-0 run would leave EACCES unmeasured.
	RequireUnprivEnv = "VIBE_REQUIRE_UNPRIVILEGED_FS"
)

// SkipOrFail skips, or fails when RequireUnprivEnv is set.
func SkipOrFail(t testing.TB, why string) {
	t.Helper()
	if os.Getenv(RequireUnprivEnv) != "" {
		t.Fatalf("%s is set, but %s: the permission cases would go unmeasured", RequireUnprivEnv, why)
	}
	t.Skip(why)
}

// RunUnprivileged returns true when the caller should go on and run its
// body in this process: this process is not uid 0, so the mode bits deny.
// Under uid 0 it re-executes this test binary, running only test `name`,
// as uid/gid 65534 (setpriv), fails the test when that run does not pass,
// and returns false. The binary is copied into a 0755 directory under
// /var/tmp: the go build directory is 0700 and nobody could not run it
// from there; TMPDIR points into that directory, so t.TempDir works there.
func RunUnprivileged(t *testing.T, name string) bool {
	t.Helper()
	if os.Geteuid() != 0 {
		return true
	}
	if os.Getenv(UnprivChildEnv) != "" {
		t.Fatal("the re-executed run is still uid 0")
	}
	setpriv, err := exec.LookPath("setpriv")
	if err != nil {
		SkipOrFail(t, "running as uid 0 and `setpriv` is not installed")
		return false
	}
	exe, err := os.Executable()
	if err != nil {
		SkipOrFail(t, fmt.Sprintf("cannot find the test binary: %v", err))
		return false
	}
	box, err := os.MkdirTemp("/var/tmp", "vibe-unpriv-")
	if err != nil {
		SkipOrFail(t, fmt.Sprintf("cannot make a directory under /var/tmp: %v", err))
		return false
	}
	t.Cleanup(func() { _ = os.RemoveAll(box) })
	tmp := filepath.Join(box, "tmp")
	bin := filepath.Join(box, "unpriv.test")
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
	cmd.Env = append(os.Environ(), UnprivChildEnv+"=1", "TMPDIR="+tmp, "HOME="+tmp)
	out, err := cmd.CombinedOutput()
	if err != nil || !strings.Contains(string(out), "--- PASS: "+name) {
		t.Fatalf("%s as uid 65534: %v\n%s", name, err, out)
	}
	return false
}
