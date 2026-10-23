//go:build unix

package main

import (
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// chownFixtureEnv carries the fixture uid 0 prepares to the unprivileged run.
const chownFixtureEnv = "VIBE_TEST_CHOWN_FIXTURE"

// TestEdit_AnOwnerItCannotSetIsAWarning: run as uid 65534 (setpriv, the
// repo's unprivileged pattern) on a file uid 0 owns in group 65534, in a
// directory the group may write. The owner cannot be given away — that is a
// warning, on stderr and in --json — but the group is set alone and is not
// reported.
func TestEdit_AnOwnerItCannotSetIsAWarning(t *testing.T) {
	const name = "TestEdit_AnOwnerItCannotSetIsAWarning"
	if os.Getenv(testutil.UnprivChildEnv) == "" {
		if os.Geteuid() != 0 {
			// A plain skip, not testutil.SkipOrFail: the retry and the warning
			// it measures are TestOwnerWarning's (a fake chown, in every
			// environment); only this real-kernel path needs uid 0 to build a
			// file another user owns, which a CI runner is not.
			t.Skip("the fixture is a file another user owns: preparing it needs uid 0")
		}
		box, err := os.MkdirTemp("/var/tmp", "vibe-chown-")
		if err != nil {
			testutil.SkipOrFail(t, "cannot make a directory under /var/tmp: "+err.Error())
			return
		}
		t.Cleanup(func() { _ = os.RemoveAll(box) })
		conf := filepath.Join(box, "conf.d")
		file := filepath.Join(conf, "t1.yaml")
		must := func(err error) {
			t.Helper()
			if err != nil {
				t.Fatalf("cannot build the fixture: %v", err)
			}
		}
		must(os.Mkdir(conf, 0o775))
		must(os.WriteFile(file, []byte("tenants:\n  t1:\n    a: \"1\"\n"), 0o664))
		must(os.Chown(conf, 0, 65534))
		must(os.Chmod(conf, 0o775))
		must(os.Chown(file, 0, 65534))
		must(os.Chmod(box, 0o755))
		t.Setenv(chownFixtureEnv, conf)
		if !testutil.RunUnprivileged(t, name) {
			b, err := os.ReadFile(file)
			if err != nil || !strings.Contains(string(b), `a: "2"`) {
				t.Fatalf("the unprivileged run did not write: %q %v", b, err)
			}
			return
		}
	}
	conf := os.Getenv(chownFixtureEnv)
	if conf == "" {
		t.Fatal("no fixture: the unprivileged run lost " + chownFixtureEnv)
	}
	code, res := runW(t, writeCmd, `{"ops":[{"op":"set","path":["tenants","t1","a"],"value":{"str":"2"}}]}`,
		"--config-dir", conf, "--path", "t1.yaml")
	if code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %+v %v", code, res, res.Message)
	}
	if len(res.Warnings) != 1 || !strings.Contains(res.Warnings[0], "owner (uid 0)") ||
		!strings.Contains(res.Warnings[0], "group (gid 65534) is kept") {
		t.Fatalf("warnings %q: want the owner alone named, the group kept", res.Warnings)
	}
	fi, err := os.Stat(filepath.Join(conf, "t1.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if st := fi.Sys().(*syscall.Stat_t); st.Gid != 65534 {
		t.Fatalf("gid %d, want 65534", st.Gid)
	}
}
