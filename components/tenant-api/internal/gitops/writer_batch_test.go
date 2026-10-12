package gitops

// #2900: the single-commit batch at the writer. The handler matrix
// (batch_all_or_nothing_test.go) covers the endpoints; these pin what only
// the writer can show — that a refused or failed batch leaves the serving
// tree, index and repository untouched (down to the inode of a file an op
// would have rewritten), and that an op is still judged on the tree the ops
// before it left. writer_batch_staging_test.go covers panics, cancellation,
// readers during a batch and conflicts.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

func setOp(id, body string) BatchOp {
	return BatchOp{TenantID: id, Merge: func([]byte) (string, error) { return body, nil }}
}

func silentBody(id, mode string) string {
	return "tenants:\n  " + id + ":\n    _silent_mode: \"" + mode + "\"\n"
}

// assertBatchLeftNothing: HEAD is where it was, the files are as snapshot,
// and `git status` shows nothing — no staged entry, no stray temp or backup.
func assertBatchLeftNothing(t *testing.T, dir, headBefore string, before map[string][]byte) {
	t.Helper()
	if got := gitRevParse(t, dir, "HEAD"); got != headBefore {
		t.Errorf("HEAD moved %s → %s", headBefore, got)
	}
	for name, want := range before {
		got, err := os.ReadFile(filepath.Join(dir, name))
		switch {
		case want == nil && err == nil:
			t.Errorf("%s exists after the batch was undone:\n%s", name, got)
		case want != nil && string(got) != string(want):
			t.Errorf("%s = %q (err %v), want %q", name, got, err, want)
		}
	}
	if st := gitOut(t, dir, "status", "--porcelain", "--untracked-files=all", "--ignored"); st != "" {
		t.Errorf("worktree/index not clean:\n%s", st)
	}
}

func TestWriteBatch_OneCommitForEveryOp(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"b-a.yaml": silentBody("b-a", "warning")})
	w := NewWriter(dir, dir)
	var mu sync.Mutex
	var notified []string
	w.SetOnWrite(func(id string) { mu.Lock(); notified = append(notified, id); mu.Unlock() })
	before := gitRevParse(t, dir, "HEAD")

	outcomes, err := w.WriteBatch(context.Background(), []BatchOp{
		setOp("b-a", silentBody("b-a", "critical")),
		setOp("b-new", silentBody("b-new", "warning")),
		// A second op on b-a merges onto the first op's result.
		{TenantID: "b-a", Merge: func(existing []byte) (string, error) {
			if !strings.Contains(string(existing), "critical") {
				t.Errorf("op 3 merged onto %q, want op 1's result", existing)
			}
			return silentBody("b-a", "disable"), nil
		}},
		// A no-op is accepted and writes nothing.
		{TenantID: "b-a", Merge: func([]byte) (string, error) { return "", ErrMergeNoOp }},
	}, "op@example.com")
	if err != nil {
		t.Fatalf("WriteBatch: %v", err)
	}
	if len(outcomes) != 4 || !outcomes[3].NoOp {
		t.Errorf("outcomes = %+v, want 4 with the last a no-op", outcomes)
	}
	if n := gitOut(t, dir, "rev-list", "--count", before+"..HEAD"); n != "1" {
		t.Fatalf("commits = %s, want 1", n)
	}
	if files := gitOut(t, dir, "show", "--name-only", "--format=", "HEAD"); files != "b-a.yaml\nb-new.yaml" {
		t.Errorf("commit files = %q, want b-a.yaml and b-new.yaml", files)
	}
	if subj := gitOut(t, dir, "log", "-1", "--format=%s"); subj != "tenant/batch: update 2 tenants via portal" {
		t.Errorf("subject = %q", subj)
	}
	if got := gitOut(t, dir, "show", "HEAD:b-a.yaml"); !strings.Contains(got, "disable") {
		t.Errorf("committed b-a.yaml = %q, want the last op's content", got)
	}
	if strings.Join(notified, ",") != "b-a,b-new" {
		t.Errorf("onWrite = %v, want once per changed tenant", notified)
	}
	if st := gitOut(t, dir, "status", "--porcelain", "--untracked-files=all"); st != "" {
		t.Errorf("worktree not clean after the commit (backup left?):\n%s", st)
	}
}

func TestWriteBatch_RefusedOpUndoesEveryWrite(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"r-a.yaml": silentBody("r-a", "warning")})
	path := filepath.Join(dir, "r-a.yaml")
	if err := os.Chmod(path, 0o600); err != nil {
		t.Fatal(err)
	}
	// A second hard link to the file, outside the repository: the serving
	// file is untouched only if the name still points at the same inode (an
	// inode NUMBER alone can be reused by a rewrite).
	link := filepath.Join(t.TempDir(), "r-a.link")
	if err := os.Link(path, link); err != nil {
		t.Skipf("hard links unsupported here: %v", err)
	}
	w := NewWriter(dir, dir)
	head := gitRevParse(t, dir, "HEAD")
	snap := map[string][]byte{"r-a.yaml": []byte(silentBody("r-a", "warning")), "r-new.yaml": nil}

	boom := errors.New("boom")
	_, err := w.WriteBatch(context.Background(), []BatchOp{
		setOp("r-a", silentBody("r-a", "critical")),
		setOp("r-new", silentBody("r-new", "warning")),
		{TenantID: "r-a", Merge: func([]byte) (string, error) { return "", boom }},
		// Judged although an earlier op was refused: every refusal is reported.
		setOp("r-bad", "tenants:\n  someone-else:\n    _silent_mode: \"x\"\n"),
	}, "op@example.com")
	var rej *BatchRejectedError
	if !errors.As(err, &rej) || !errors.Is(err, ErrBatchRejected) {
		t.Fatalf("err = %v, want *BatchRejectedError", err)
	}
	if rej.Outcomes[0].Err != nil || rej.Outcomes[1].Err != nil ||
		!errors.Is(rej.Outcomes[2].Err, boom) || !errors.Is(rej.Outcomes[3].Err, ErrValidation) {
		t.Errorf("outcomes = %+v, want ops 2 (merge error) and 3 (validation) refused", rej.Outcomes)
	}
	assertBatchLeftNothing(t, dir, head, snap)
	fiAfter, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	fiLink, err := os.Stat(link)
	if err != nil {
		t.Fatal(err)
	}
	if !os.SameFile(fiLink, fiAfter) || fiAfter.Mode().Perm() != 0o600 {
		t.Errorf("r-a.yaml is not the original file any more (still linked %v, mode %v)",
			os.SameFile(fiLink, fiAfter), fiAfter.Mode().Perm())
	}
}

// A commit that fails after every op passed (here a pre-commit hook, run in
// the staging worktree) publishes nothing: the serving tree and index are
// untouched.
func TestWriteBatch_CommitFailureUndoesEveryWrite(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"c-a.yaml": silentBody("c-a", "warning")})
	if err := os.WriteFile(filepath.Join(dir, ".git", "hooks", "pre-commit"), []byte("#!/bin/sh\nexit 1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	w := NewWriter(dir, dir)
	head := gitRevParse(t, dir, "HEAD")
	_, err := w.WriteBatch(context.Background(), []BatchOp{
		setOp("c-a", silentBody("c-a", "critical")),
		setOp("c-new", silentBody("c-new", "warning")),
	}, "op@example.com")
	if err == nil || errors.Is(err, ErrBatchRejected) {
		t.Fatalf("err = %v, want the commit failure", err)
	}
	assertBatchLeftNothing(t, dir, head, map[string][]byte{
		"c-a.yaml": []byte(silentBody("c-a", "warning")), "c-new.yaml": nil})
}

// The PR-mode twin: no commit on a branch, no branch, the serving tree clean.
func TestWritePRBatch_CommitFailureLeavesNothing(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"p-a.yaml": silentBody("p-a", "warning")})
	if err := os.WriteFile(filepath.Join(dir, ".git", "hooks", "pre-commit"), []byte("#!/bin/sh\nexit 1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	w := NewWriter(dir, dir)
	head := gitRevParse(t, dir, "HEAD")
	pass := func(id, mode string) PRBatchOp {
		return PRBatchOp{TenantID: id, Merge: func([]byte) (string, error) { return silentBody(id, mode), nil }}
	}
	_, err := w.WritePRBatch(context.Background(), []PRBatchOp{pass("p-a", "critical"), pass("p-new", "warning")}, "op@example.com")
	if err == nil {
		t.Fatal("WritePRBatch succeeded with a failing commit")
	}
	if br := gitOut(t, dir, "branch", "--list", "tenant-api/*"); br != "" {
		t.Errorf("branch left behind: %s", br)
	}
	if br := gitOut(t, dir, "ls-remote", "--heads", "origin"); strings.Contains(br, "tenant-api/") {
		t.Errorf("branch pushed: %s", br)
	}
	assertBatchLeftNothing(t, dir, head, map[string][]byte{
		"p-a.yaml": []byte(silentBody("p-a", "warning")), "p-new.yaml": nil})
}

// The direct twin of TestWritePRBatch_OpSeesEarlierOpsWrites: op1 drops mv-y
// from the shared mv-x.yaml, so op2's mv-y.yaml is the id's only declaration
// on the tree it lands on — judged there, it is accepted. Judged on the tree
// before op1 (or on an in-memory overlay the placement walk cannot see), it
// would be refused as declared elsewhere.
func TestWriteBatch_OpIsJudgedOnEarlierOpsWrites(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"mv-x.yaml": "tenants:\n" +
		"  mv-x:\n    _silent_mode: \"warning\"\n" +
		"  mv-y:\n    _silent_mode: \"warning\"\n"})
	w := NewWriter(dir, dir)
	head := gitRevParse(t, dir, "HEAD")
	if _, err := w.WriteBatch(context.Background(), []BatchOp{
		setOp("mv-x", tenantBody("mv-x")),
		setOp("mv-y", tenantBody("mv-y")),
	}, "op@example.com"); err != nil {
		t.Fatalf("WriteBatch: %v", err)
	}
	if n := gitOut(t, dir, "rev-list", "--count", head+"..HEAD"); n != "1" {
		t.Errorf("commits = %s, want 1", n)
	}
}
