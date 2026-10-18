package gitops

// #1535 review: gitCommitOnto's compare-and-swap reports an error, but
// where HEAD is afterwards decides what happened — as publishBatch does.
// A ref update killed after it wrote the ref (the per-command timeout) has
// published the commit: that is a write that committed, not ErrConflict
// ("nothing written") with the commit already on the branch. One that
// failed before writing the ref published nothing and leaves the real
// index as it was.
//
// Tenant ids are fixture names (CLAUDE.md #9).

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// updateRefFailingGit is a git wrapper that makes every `update-ref` exit 1:
// after running it (ranFirst) or without running it.
func updateRefFailingGit(t *testing.T, ranFirst bool) string {
	t.Helper()
	onUpdateRef := "exit 1"
	if ranFirst {
		onUpdateRef = "git \"$@\"; exit 1"
	}
	stub := filepath.Join(t.TempDir(), "git")
	script := "#!/bin/sh\n" +
		"for a in \"$@\"; do\n" +
		"  if [ \"$a\" = \"update-ref\" ]; then " + onUpdateRef + "; fi\n" +
		"done\n" +
		"exec git \"$@\"\n"
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}
	return stub
}

func TestDirectWrite_RefUpdateErrorAfterRefMoved_IsCommitted(t *testing.T) {
	t.Parallel()
	dir := oobRepo(t)
	w := newTestWriter(dir, dir)
	w.SetValidator(&recordingValidator{})
	w.gitBinary = updateRefFailingGit(t, true)
	notified := false
	w.SetOnWrite(func(string) { notified = true })
	base := gitRevParse(t, dir, "HEAD")
	body := oobBody(oobWritten, "70")

	if _, err := w.Write(context.Background(), oobWritten, "alice@example.com", body); err != nil {
		t.Fatalf("write = %v, want committed (HEAD moved off the base: %v)", err, gitRevParse(t, dir, "HEAD") != base)
	}
	if gitRevParse(t, dir, "HEAD") == base || gitRevParse(t, dir, "HEAD~1") != base {
		t.Fatal("HEAD is not the write's commit on the base")
	}
	if got := gitT(t, dir, "show", "HEAD:"+oobWritten+".yaml"); got != body {
		t.Errorf("HEAD's file = %q, want the written bytes", got)
	}
	if got := gitT(t, dir, "show", ":"+oobWritten+".yaml"); got != body {
		t.Errorf("index entry = %q, want the committed bytes", got)
	}
	if got, err := os.ReadFile(filepath.Join(dir, oobWritten+".yaml")); err != nil || string(got) != body {
		t.Errorf("serving file = %q (%v), want the committed bytes", got, err)
	}
	if st := gitT(t, dir, "status", "--porcelain", "--", oobWritten+".yaml"); st != "" {
		t.Errorf("git status shows the written path: %q", st)
	}
	if !notified {
		t.Error("onWrite not called for a write that committed")
	}
	assertOOBStillStaged(t, dir)
}

func TestDirectWrite_RefUpdateErrorRefUnmoved_WritesNothing(t *testing.T) {
	t.Parallel()
	dir := oobRepo(t)
	w := newTestWriter(dir, dir)
	w.SetValidator(&recordingValidator{})
	w.gitBinary = updateRefFailingGit(t, false)
	base := gitRevParse(t, dir, "HEAD")

	_, err := w.Write(context.Background(), oobWritten, "alice@example.com", oobBody(oobWritten, "70"))
	if err == nil || errors.Is(err, ErrConflict) {
		t.Fatalf("write = %v, want the ref-update failure (not ErrConflict)", err)
	}
	if !strings.Contains(err.Error(), "advance HEAD") {
		t.Errorf("err = %v, want it to name the HEAD update", err)
	}
	if gitRevParse(t, dir, "HEAD") != base {
		t.Fatal("HEAD moved")
	}
	if got := gitT(t, dir, "show", ":"+oobWritten+".yaml"); got != oobBody(oobWritten, "60") {
		t.Errorf("index entry = %q, want HEAD's (the sync undone)", got)
	}
	if got, err := os.ReadFile(filepath.Join(dir, oobWritten+".yaml")); err != nil || string(got) != oobBody(oobWritten, "60") {
		t.Errorf("serving file = %q (%v), want HEAD's", got, err)
	}
	assertOOBStillStaged(t, dir)
}
