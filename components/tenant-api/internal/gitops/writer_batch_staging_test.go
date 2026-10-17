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
	w := newTestWriter(dir, dir)
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
		w := newTestWriter(dir, dir)
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
	w := newTestWriter(dir, dir)
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
	w := newTestWriter(dir, dir)
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
	w := newTestWriter(dir, dir)
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
	w := newTestWriter(dir, dir)
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
	w := newTestWriter(dir, dir)
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
		w := newTestWriter(dir, dir)
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
		w2 := newTestWriter(dir, dir) // a restarted process
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

// Round 3 (1): a publish marker names a path the branch no longer has
// (interrupted publish, then an external commit deleted the file). Recovery
// removes the serving copy and clears the marker instead of failing every
// later write.
func TestRecoverBatchPublish_PathGoneFromHead(t *testing.T) {
	for _, via := range []string{"startup", "next write"} {
		dir := seedTreeRepo(t, baseFiles())
		w := newTestWriter(dir, dir)
		w.beforeBatchSync = func() error { return errors.New("killed after the ref update") }
		if _, err := w.WriteBatch(context.Background(), []BatchOp{
			setOp("s-a", silentBody("s-a", "critical")),
			setOp("s-new", silentBody("s-new", "warning")),
		}, "op@example.com"); err == nil {
			t.Fatal("want the injected failure")
		}
		// External commit deleting s-new.yaml on the branch (index only; the
		// serving worktree still has no s-new.yaml, s-a.yaml is stale).
		externalCommitWithout(t, dir, "s-new.yaml")
		w2 := newTestWriter(dir, dir)
		var err error
		if via == "startup" {
			err = w2.RecoverBatchPublish()
		} else {
			_, err = w2.Write(context.Background(), "s-b", "op@example.com", silentBody("s-b", "critical"))
		}
		if err != nil {
			t.Fatalf("%s: %v", via, err)
		}
		if p, _ := w2.publishMarkerPath(); fileExists(p) {
			t.Errorf("%s: publish marker still present", via)
		}
		if fileExists(filepath.Join(dir, "s-new.yaml")) {
			t.Errorf("%s: s-new.yaml on the serving tree, absent from HEAD", via)
		}
		if got, _ := os.ReadFile(filepath.Join(dir, "s-a.yaml")); string(got) != gitOut(t, dir, "show", "HEAD:s-a.yaml")+"\n" {
			t.Errorf("%s: s-a.yaml = %q, want HEAD's", via, got)
		}
		if st := gitOut(t, dir, "status", "--porcelain", "--untracked-files=all"); st != "" {
			t.Errorf("%s: worktree differs from HEAD:\n%s", via, st)
		}
	}
}

// Round 3 (1): an empty path set never resets the whole index.
func TestSyncServingPaths_EmptySetLeavesIndexAlone(t *testing.T) {
	dir := seedTreeRepo(t, baseFiles())
	if err := os.WriteFile(filepath.Join(dir, "s-a.yaml"), []byte(silentBody("s-a", "staged")), 0o644); err != nil {
		t.Fatal(err)
	}
	gitRun(t, dir, "add", "s-a.yaml") // an operator's staged change
	w := newTestWriter(dir, dir)
	if err := w.syncServingPaths("HEAD", nil); err != nil {
		t.Fatalf("syncServingPaths(nil): %v", err)
	}
	if st := gitOut(t, dir, "diff", "--cached", "--name-only"); st != "s-a.yaml" {
		t.Errorf("staged set = %q, want s-a.yaml still staged", st)
	}
}

// Round 3 (2): a tenant file that is a relative symlink to a file OUTSIDE
// conf.d. The staging copy must resolve to the same file, so the batch
// merges onto the same existing content as a single write and keeps its
// other keys.
func TestWriteBatch_RelativeSymlinkOutsideConfDir(t *testing.T) {
	repo := t.TempDir()
	conf := filepath.Join(repo, "conf.d")
	shared := filepath.Join(repo, "shared")
	for _, d := range []string{conf, shared} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	gitRun(t, repo, "init", "-q", "-b", "main")
	gitRun(t, repo, "config", "user.email", "t@t.com")
	gitRun(t, repo, "config", "user.name", "T")
	const orig = "tenants:\n  ln-t:\n    _silent_mode: \"warning\"\n    _routing_profile: keep-me\n"
	if err := os.WriteFile(filepath.Join(shared, "ln-t.yaml"), []byte(orig), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join("..", "shared", "ln-t.yaml"), filepath.Join(conf, "ln-t.yaml")); err != nil {
		t.Skipf("symlink unsupported: %v", err)
	}
	gitRun(t, repo, "add", "-A")
	gitRun(t, repo, "commit", "-q", "-m", "seed")

	var single, batch []byte
	capture := func(dst *[]byte) MergeFunc {
		return func(existing []byte) (string, error) {
			*dst = append([]byte(nil), existing...)
			return strings.Replace(string(existing), "warning", "critical", 1), nil
		}
	}
	w := newTestWriter(conf, repo)
	// The single write only reads (its merge result is discarded by a no-op).
	if _, err := w.WriteMerged(context.Background(), "ln-t", "op@example.com", func(existing []byte) (string, error) {
		single = append([]byte(nil), existing...)
		return "", ErrMergeNoOp
	}); err != nil {
		t.Fatalf("WriteMerged: %v", err)
	}
	if _, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: "ln-t", Merge: capture(&batch)}}, "op@example.com"); err != nil {
		t.Fatalf("WriteBatch: %v", err)
	}
	if string(single) != orig || string(batch) != string(single) {
		t.Fatalf("existing: single %q, batch %q; want both the shared file", single, batch)
	}
	committed := gitOut(t, repo, "show", "HEAD:conf.d/ln-t.yaml")
	if !strings.Contains(committed, "keep-me") || !strings.Contains(committed, "critical") {
		t.Errorf("committed conf.d/ln-t.yaml = %q, want the other keys kept and the patch applied", committed)
	}
}

// externalCommitWithout advances the serving branch with a commit that drops
// path, without touching the serving index or worktree (another process).
func externalCommitWithout(t *testing.T, dir, path string) {
	t.Helper()
	idx := filepath.Join(t.TempDir(), "index")
	run := func(args ...string) string {
		cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
		cmd.Env = append(os.Environ(), "GIT_INDEX_FILE="+idx)
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	run("read-tree", "HEAD")
	run("rm", "-q", "--cached", path)
	tree := run("write-tree")
	commit := run("commit-tree", tree, "-p", "HEAD", "-m", "external: drop "+path)
	run("update-ref", "HEAD", commit)
}

func fileExists(p string) bool {
	_, err := os.Lstat(p)
	return err == nil
}

// Round 4: recovery deletes a serving file only when git positively says the
// commit does not have it. A broken repository (here the commit's tree
// object is gone) is an error: the marker stays and no file is touched.
func TestRecoverBatchPublish_BrokenTreeDeletesNothing(t *testing.T) {
	dir := seedTreeRepo(t, baseFiles())
	w := newTestWriter(dir, dir)
	w.beforeBatchSync = func() error { return errors.New("killed after the ref update") }
	if _, err := w.WriteBatch(context.Background(), []BatchOp{
		setOp("s-a", silentBody("s-a", "critical")),
	}, "op@example.com"); err == nil {
		t.Fatal("want the injected failure")
	}
	tree := gitOut(t, dir, "rev-parse", "HEAD^{tree}")
	obj := filepath.Join(dir, ".git", "objects", tree[:2], tree[2:])
	if err := os.Remove(obj); err != nil {
		t.Skipf("tree object is not loose: %v", err)
	}
	w2 := newTestWriter(dir, dir)
	marker, _ := w2.publishMarkerPath()
	if err := w2.RecoverBatchPublish(); err == nil {
		t.Error("RecoverBatchPublish on a broken tree returned nil, want an error")
	} else if !strings.Contains(err.Error(), marker) {
		t.Errorf("recovery error = %q, want it to name the marker %s", err, marker)
	}
	if got, err := os.ReadFile(filepath.Join(dir, "s-a.yaml")); err != nil || string(got) != silentBody("s-a", "warning") {
		t.Errorf("serving s-a.yaml = %q (err %v), want it untouched", got, err)
	}
	if p, _ := w2.publishMarkerPath(); !fileExists(p) {
		t.Error("publish marker removed although recovery failed")
	}
}

// Recovery drops a marker without a sync only on a POSITIVE "not an
// ancestor" (merge-base exit 1). Any other failure of the ancestry check —
// here the marker's commit object does not exist, which git reports with
// exit 128 — is an error that keeps the marker and touches no file.
func TestRecoverBatchPublish_AncestryCheckFailureKeepsMarker(t *testing.T) {
	dir := seedTreeRepo(t, baseFiles())
	if err := os.WriteFile(filepath.Join(dir, "s-a.yaml"), []byte("operator edit\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	w := newTestWriter(dir, dir)
	missing := strings.Repeat("ab", 20) // well-formed, absent from the object store
	if err := w.writePublishMarker(missing, []string{"s-a.yaml"}); err != nil {
		t.Fatal(err)
	}
	marker, _ := w.publishMarkerPath()
	if err := w.RecoverBatchPublish(); err == nil {
		t.Error("RecoverBatchPublish with an unresolvable marker commit returned nil, want an error")
	} else if !strings.Contains(err.Error(), marker) {
		t.Errorf("recovery error = %q, want it to name the marker %s", err, marker)
	}
	if p, _ := w.publishMarkerPath(); !fileExists(p) {
		t.Error("publish marker removed although the ancestry check failed")
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "s-a.yaml")); string(got) != "operator edit\n" {
		t.Errorf("s-a.yaml = %q, want it untouched", got)
	}

	// A commit that exists but is not on the branch (merge-base exit 1): the
	// publish never landed, so the marker is dropped and nothing is synced.
	unrelated := gitOut(t, dir, "commit-tree", gitOut(t, dir, "rev-parse", "HEAD^{tree}"), "-m", "off-branch")
	if err := w.writePublishMarker(unrelated, []string{"s-a.yaml"}); err != nil {
		t.Fatal(err)
	}
	if err := w.RecoverBatchPublish(); err != nil {
		t.Fatalf("RecoverBatchPublish with an off-branch commit: %v", err)
	}
	if p, _ := w.publishMarkerPath(); fileExists(p) {
		t.Error("publish marker kept although the commit is positively not on the branch")
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "s-a.yaml")); string(got) != "operator edit\n" {
		t.Errorf("s-a.yaml = %q, want it untouched (nothing was published)", got)
	}
}
