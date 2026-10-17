package gitops

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// #3062 (CodeQL go/command-injection, go/path-injection): DiffTexts must not
// put the tenant id into a git argument or a temporary file path. A legal id
// that would do harm cannot be built (it is DNS-1123 by the time it gets
// here), so the test asserts the id's absence instead: a stub git records the
// arguments it got and the files it was run among, then runs the real git.
// The caller-visible output must still be what the old id-named files gave.
func TestDiffTexts_TenantIDReachesNeitherGitNorTheFilesystem(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix shell stub; Go tests for this package run in the dev container / Linux CI")
	}
	const id = "diff-sink-tenant"
	current := "tenants:\n  " + id + ":\n    cpu: \"80\"\n"
	// The body spells the fixed temp names: only the header is relabelled.
	proposed := "tenants:\n  " + id + ":\n    cpu: \"90\"\n    note: current/tenant.yaml proposed/tenant.yaml\n"

	record := filepath.Join(t.TempDir(), "git.record")
	stub := filepath.Join(t.TempDir(), "git-records-diff.sh")
	script := "#!/bin/sh\n" +
		"{ printf 'arg:%s\\n' \"$@\"; find . -print | sed 's/^/path:/'; } > '" + record + "'\n" +
		"exec git \"$@\"\n"
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}
	w := NewWriter(t.TempDir(), "")
	w.gitBinary = stub

	got, err := w.DiffTexts(id, current, true, proposed)
	if err != nil {
		t.Fatalf("DiffTexts: %v", err)
	}
	seen, err := os.ReadFile(record)
	if err != nil {
		t.Fatalf("the stub never ran: %v", err)
	}
	if !strings.Contains(string(seen), "arg:--no-index") || !strings.Contains(string(seen), "path:./current/") {
		t.Fatalf("stub record does not show the diff run:\n%s", seen)
	}
	if strings.Contains(string(seen), id) {
		t.Errorf("the tenant id reached git's arguments or a temp path:\n%s", seen)
	}

	// The output the old code gave: git diff on current/<id>.yaml and
	// proposed/<id>.yaml, run directly.
	dir := t.TempDir()
	for sub, text := range map[string]string{"current": current, "proposed": proposed} {
		if err := os.Mkdir(filepath.Join(dir, sub), 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, sub, id+".yaml"), []byte(text), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	cmd := exec.Command("git", "diff", "--no-index", "--", "current/"+id+".yaml", "proposed/"+id+".yaml")
	cmd.Dir = dir
	cmd.Env = gitEnv(false)
	want, _ := cmd.Output() // exit 1 on a difference
	if len(want) == 0 {
		t.Fatal("reference git diff gave no output")
	}
	if got != string(want) {
		t.Errorf("DiffTexts output differs from the id-named diff:\n got:\n%s\nwant:\n%s", got, want)
	}
	if !strings.Contains(got, "+    note: current/tenant.yaml proposed/tenant.yaml") {
		t.Errorf("a content line spelling the fixed names was rewritten:\n%s", got)
	}
}

func TestRelabelDiffHeader(t *testing.T) {
	t.Parallel()
	in := "diff --git a/current/tenant.yaml b/proposed/tenant.yaml\n" +
		"index 1..2 100644\n" +
		"--- a/current/tenant.yaml\n" +
		"+++ b/proposed/tenant.yaml\n" +
		"@@ -1 +1 @@\n" +
		"-current/tenant.yaml\n" +
		"+proposed/tenant.yaml\n"
	want := "diff --git a/current/t-x.yaml b/proposed/t-x.yaml\n" +
		"index 1..2 100644\n" +
		"--- a/current/t-x.yaml\n" +
		"+++ b/proposed/t-x.yaml\n" +
		"@@ -1 +1 @@\n" +
		"-current/tenant.yaml\n" +
		"+proposed/tenant.yaml\n"
	if got := relabelDiffHeader(in, "t-x.yaml"); got != want {
		t.Errorf("relabelDiffHeader:\n got:\n%s\nwant:\n%s", got, want)
	}
	bin := "diff --git a/current/tenant.yaml b/proposed/tenant.yaml\n" +
		"Binary files a/current/tenant.yaml and b/proposed/tenant.yaml differ\n"
	wantBin := "diff --git a/current/t-x.yaml b/proposed/t-x.yaml\n" +
		"Binary files a/current/t-x.yaml and b/proposed/t-x.yaml differ\n"
	if got := relabelDiffHeader(bin, "t-x.yaml"); got != wantBin {
		t.Errorf("relabelDiffHeader (no hunk):\n got:\n%s\nwant:\n%s", got, wantBin)
	}
}
