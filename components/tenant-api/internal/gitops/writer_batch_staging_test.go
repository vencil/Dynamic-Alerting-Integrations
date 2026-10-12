package gitops

// #2900 round 2: a batch runs in a staging worktree, so the SERVING worktree
// and branch only ever hold committed content. These pin what that buys:
// a panic, a cancel, a refusal or an external commit mid-batch leaves the
// serving tree exactly at base; a reader during the batch sees base; and a
// batch that WAS committed is never reported as "nothing written".

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

// assertServingAtBase: HEAD unchanged, the serving files as before, nothing
// in `git status`, and no batch worktree left registered or on disk.
func assertServingAtBase(t *testing.T, dir, head string, files map[string]string) {
	t.Helper()
	if got := gitRevParse(t, dir, "HEAD"); got != head {
		t.Errorf("HEAD moved %s → %s", head, got)
	}
	for name, want := range files {
		got, err := os.ReadFile(filepath.Join(dir, name))
		if want == "" {
			if err == nil {
				t.Errorf("%s exists on the serving tree:\n%s", name, got)
			}
			continue
		}
		if string(got) != want {
			t.Errorf("%s on the serving tree = %q (err %v), want base %q", name, got, err, want)
		}
	}
	if st := gitOut(t, dir, "status", "--porcelain", "--untracked-files=all", "--ignored"); st != "" {
		t.Errorf("serving worktree not clean:\n%s", st)
	}
	if wt := gitOut(t, dir, "worktree", "list", "--porcelain"); strings.Count(wt, "worktree ") != 1 {
		t.Errorf("a batch worktree is still registered:\n%s", wt)
	}
}

func baseFiles() map[string]string {
	return map[string]string{
		"s-a.yaml": silentBody("s-a", "warning"),
		"s-b.yaml": silentBody("s-b", "warning"),
	}
}

// F1: the third op panics. The panic propagates (the async pool recovers it),
// and nothing of ops 1-2 is in the serving tree, the index or the branch.
func TestWriteBatchR2_PanicMidBatchLeavesServingTreeAtBase(t *testing.T) {
	dir := seedTreeRepo(t, baseFiles())
	w := NewWriter(dir, dir)
	head := gitRevParse(t, dir, "HEAD")
	func() {
		defer func() {
			if recover() == nil {
				t.Error("the op's panic did not propagate")
			}
		}()
		_, _ = w.WriteBatch(context.Background(), []BatchOp{
			setOp("s-a", silentBody("s-a", "critical")),
			setOp("s-new", silentBody("s-new", "warning")),
			{TenantID: "s-b", Merge: func([]byte) (string, error) { panic("boom") }},
		}, "op@example.com")
	}()
	want := baseFiles()
	want["s-new.yaml"] = ""
	assertServingAtBase(t, dir, head, want)
	// The writer is usable afterwards (lock and admission token released).
	if _, err := w.WriteBatch(context.Background(), []BatchOp{setOp("s-a", silentBody("s-a", "critical"))}, "op@example.com"); err != nil {
		t.Fatalf("batch after a panicked one: %v", err)
	}
}

// F2: while the batch is between ops, a lockless reader (the preview read
// path) and a plain file read both see base — during a batch that is then
// refused, and during one that is then committed.
func TestWriteBatchR2_ReadersSeeBaseDuringBatch(t *testing.T) {
	for _, refuse := range []bool{true, false} {
		dir := seedTreeRepo(t, baseFiles())
		w := NewWriter(dir, dir)
		head := gitRevParse(t, dir, "HEAD")
		var seen []string
		probe := func([]byte) (string, error) {
			var wg sync.WaitGroup
			wg.Add(1)
			go func() { // another goroutine, no lock
				defer wg.Done()
				cur, _, err := w.PreviewCurrent("s-a")
				raw, rerr := os.ReadFile(filepath.Join(dir, "s-a.yaml"))
				seen = append(seen, string(cur), string(raw))
				if err != nil || rerr != nil {
					t.Errorf("read during batch: %v / %v", err, rerr)
				}
			}()
			wg.Wait()
			if refuse {
				return "", errors.New("refused")
			}
			return silentBody("s-b", "critical"), nil
		}
		_, err := w.WriteBatch(context.Background(), []BatchOp{
			setOp("s-a", silentBody("s-a", "critical")),
			{TenantID: "s-b", Merge: probe},
		}, "op@example.com")
		for _, s := range seen {
			if s != silentBody("s-a", "warning") {
				t.Errorf("refuse=%v: a reader saw %q during the batch, want base", refuse, s)
			}
		}
		if refuse {
			if !errors.Is(err, ErrBatchRejected) {
				t.Fatalf("err = %v, want rejected", err)
			}
			assertServingAtBase(t, dir, head, baseFiles())
		} else if err != nil {
			t.Fatalf("WriteBatch: %v", err)
		}
	}
}

// F5: a cancelled context between ops means the whole batch is not written.
func TestWriteBatchR2_CancelMidBatchWritesNothing(t *testing.T) {
	dir := seedTreeRepo(t, baseFiles())
	w := NewWriter(dir, dir)
	head := gitRevParse(t, dir, "HEAD")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	ranAfterCancel := false
	_, err := w.WriteBatch(ctx, []BatchOp{
		setOp("s-a", silentBody("s-a", "critical")),
		{TenantID: "s-b", Merge: func([]byte) (string, error) {
			cancel()
			return silentBody("s-b", "critical"), nil
		}},
		{TenantID: "s-new", Merge: func([]byte) (string, error) {
			ranAfterCancel = true
			return silentBody("s-new", "warning"), nil
		}},
	}, "op@example.com")
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("err = %v, want context.Canceled", err)
	}
	if ranAfterCancel {
		t.Error("an op ran after the context was cancelled; the batch must stop between ops")
	}
	want := baseFiles()
	want["s-new.yaml"] = ""
	assertServingAtBase(t, dir, head, want)
}

// F3: an external commit lands on the serving branch while the batch runs.
// Whatever the writer reports must be true of the repository: an error that
// is not *BatchCommittedError means the batch is NOT in the branch or the
// serving tree; a *BatchCommittedError means it IS.
func TestWriteBatchR2_ReportMatchesRepositoryOnExternalCommit(t *testing.T) {
	dir := seedTreeRepo(t, baseFiles())
	w := NewWriter(dir, dir)
	_, err := w.WriteBatch(context.Background(), []BatchOp{
		setOp("s-a", silentBody("s-a", "critical")),
		{TenantID: "s-b", Merge: func([]byte) (string, error) {
			if out, err := exec.Command("git", "-C", dir, "commit", "--allow-empty", "-m", "external").CombinedOutput(); err != nil {
				t.Fatalf("external commit: %v\n%s", err, out)
			}
			return silentBody("s-b", "critical"), nil
		}},
	}, "op@example.com")
	if !errors.Is(err, ErrConflict) {
		t.Fatalf("err = %v, want ErrConflict", err)
	}
	inRepo := strings.Contains(gitOut(t, dir, "show", "HEAD:s-a.yaml"), "critical")
	var committed *BatchCommittedError
	switch {
	case errors.As(err, &committed) && !inRepo:
		t.Error("reported committed, but the batch is not in the branch")
	case !errors.As(err, &committed) && inRepo:
		t.Error("reported not committed, but the batch is in the branch")
	}
	if !inRepo {
		if got, _ := os.ReadFile(filepath.Join(dir, "s-a.yaml")); string(got) != silentBody("s-a", "warning") {
			t.Errorf("not committed, but the serving tree carries the batch: %q", got)
		}
	}
}

// F3, the other half: the branch was advanced to the batch's commit, then the
// write failed. The error must say committed (and carry the commit), and the
// branch indeed holds it.
func TestWriteBatchR2_FailureAfterPublishIsReportedCommitted(t *testing.T) {
	dir := seedTreeRepo(t, baseFiles())
	w := NewWriter(dir, dir)
	w.beforeBatchSync = func() error { return errors.New("injected after the ref moved") }
	outcomes, err := w.WriteBatch(context.Background(), []BatchOp{setOp("s-a", silentBody("s-a", "critical"))}, "op@example.com")
	var committed *BatchCommittedError
	if !errors.As(err, &committed) || !errors.Is(err, ErrConflict) {
		t.Fatalf("err = %v, want *BatchCommittedError (ErrConflict)", err)
	}
	if got := gitRevParse(t, dir, "HEAD"); got != committed.Commit {
		t.Errorf("HEAD = %s, want the reported commit %s", got, committed.Commit)
	}
	if !strings.Contains(gitOut(t, dir, "show", "HEAD:s-a.yaml"), "critical") {
		t.Error("the reported commit does not carry the batch")
	}
	if len(outcomes) != 1 || outcomes[0].Err != nil {
		t.Errorf("outcomes = %+v, want the written op", outcomes)
	}
}

// Startup recovery: a batch worktree a killed process left (registered with
// git, or only a directory) is removed, and git forgets it.
func TestPruneBatchWorktrees_RemovesLeftovers(t *testing.T) {
	dir := seedTreeRepo(t, baseFiles())
	w := NewWriter(dir, dir)
	parent, err := w.batchWorktreesDir()
	if err != nil {
		t.Fatal(err)
	}
	gitRun(t, dir, "worktree", "add", "-q", "--detach", filepath.Join(parent, "crashed-1"), "HEAD")
	if err := os.MkdirAll(filepath.Join(parent, "crashed-2", "half"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := w.PruneBatchWorktrees(); err != nil {
		t.Fatalf("PruneBatchWorktrees: %v", err)
	}
	if entries, _ := os.ReadDir(parent); len(entries) != 0 {
		t.Errorf("left in %s: %v", parent, entries)
	}
	if wt := gitOut(t, dir, "worktree", "list", "--porcelain"); strings.Count(wt, "worktree ") != 1 {
		t.Errorf("git still lists a batch worktree:\n%s", wt)
	}
	// And a batch runs normally afterwards.
	if _, err := w.WriteBatch(context.Background(), []BatchOp{setOp("s-a", silentBody("s-a", "critical"))}, "op@example.com"); err != nil {
		t.Fatalf("WriteBatch after prune: %v", err)
	}
	// Idempotent on a clean repo.
	if err := w.PruneBatchWorktrees(); err != nil {
		t.Fatalf("second prune: %v", err)
	}
}

// A direct-mode batch is judged on the serving conf.d as the exporter reads
// it: an untracked file there that declares the tenant still refuses the
// write as declared elsewhere, as a single write does — and stays untracked.
func TestWriteBatchR2_UntrackedServingFileIsJudged(t *testing.T) {
	dir := seedTreeRepo(t, baseFiles())
	if err := os.MkdirAll(filepath.Join(dir, "team"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "team", "local.yaml"), []byte(silentBody("u-t", "warning")), 0o644); err != nil {
		t.Fatal(err)
	}
	w := NewWriter(dir, dir)
	head := gitRevParse(t, dir, "HEAD")
	_, err := w.WriteBatch(context.Background(), []BatchOp{setOp("u-t", silentBody("u-t", "critical"))}, "op@example.com")
	if !errors.Is(err, ErrTenantDeclaredElsewhere) {
		t.Fatalf("err = %v, want ErrTenantDeclaredElsewhere", err)
	}
	if got := gitRevParse(t, dir, "HEAD"); got != head {
		t.Errorf("HEAD moved")
	}
	if _, err := os.Stat(filepath.Join(dir, "u-t.yaml")); err == nil {
		t.Error("u-t.yaml written on the serving tree")
	}
}

// Owner decision (ADR-036 §4): the branch is advanced first, the worktree
// second. A crash between the two — simulated by failing right after the
// ref update — leaves the worktree on the OLD committed content (never
// content no commit holds), and the next write's lock, or startup, brings
// it up to the branch.
func TestWriteBatchR2_InterruptedPublishIsRecovered(t *testing.T) {
	for _, via := range []string{"startup", "next write"} {
		dir := seedTreeRepo(t, baseFiles())
		w := NewWriter(dir, dir)
		w.beforeBatchSync = func() error { return errors.New("killed after the ref update") }
		_, err := w.WriteBatch(context.Background(), []BatchOp{
			setOp("s-a", silentBody("s-a", "critical")),
			setOp("s-new", silentBody("s-new", "warning")),
		}, "op@example.com")
		var committed *BatchCommittedError
		if !errors.As(err, &committed) {
			t.Fatalf("%s: err = %v, want *BatchCommittedError", via, err)
		}
		// Between the two steps: behind the branch, but committed content.
		if got, _ := os.ReadFile(filepath.Join(dir, "s-a.yaml")); string(got) != silentBody("s-a", "warning") {
			t.Fatalf("%s: interrupted worktree s-a.yaml = %q, want the previous commit's", via, got)
		}
		w2 := NewWriter(dir, dir) // a restarted process
		switch via {
		case "startup":
			if err := w2.RecoverBatchPublish(); err != nil {
				t.Fatalf("RecoverBatchPublish: %v", err)
			}
		default:
			if _, err := w2.Write(context.Background(), "s-b", "op@example.com", silentBody("s-b", "critical")); err != nil {
				t.Fatalf("next write: %v", err)
			}
		}
		for name, want := range map[string]string{
			"s-a.yaml":   gitOut(t, dir, "show", "HEAD:s-a.yaml") + "\n",
			"s-new.yaml": gitOut(t, dir, "show", "HEAD:s-new.yaml") + "\n",
		} {
			if got, _ := os.ReadFile(filepath.Join(dir, name)); string(got) != want {
				t.Errorf("%s: %s = %q, want the branch's %q", via, name, got, want)
			}
		}
		if st := gitOut(t, dir, "status", "--porcelain", "--untracked-files=all"); st != "" {
			t.Errorf("%s: worktree differs from HEAD after recovery:\n%s", via, st)
		}
	}
}
