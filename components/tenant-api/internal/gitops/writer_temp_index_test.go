package gitops

// #1535 item 1: a direct tenant write commits ONLY the path it wrote. The
// commit is built from a temporary index (HEAD's tree plus this write's
// path — `git commit --only` semantics), so a file an operator staged in
// the serving repository by hand, which the generator never judged, does
// not ride along in the tenant's commit; and the operator's staged change
// is still staged afterwards, whether the write committed or not.
//
// Tenant ids are fixture names (CLAUDE.md #9).

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

const (
	oobWritten = "tn-oob-written" // the tenant the API writes
	oobOther   = "tn-oob-other"   // the tenant an operator stages by hand
)

func oobBody(tenant, v string) string {
	return "tenants:\n  " + tenant + ":\n    mysql_threads_running: \"" + v + "\"\n"
}

func gitT(t *testing.T, dir string, args ...string) string {
	t.Helper()
	out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return string(out)
}

// oobRepo is a repository holding both tenants' files, with the other
// tenant's file changed and `git add`-ed out of band (not committed).
func oobRepo(t *testing.T) string {
	t.Helper()
	dir := initRepoWithDefaults(t)
	for _, tn := range []string{oobWritten, oobOther} {
		if err := os.WriteFile(filepath.Join(dir, tn+".yaml"), []byte(oobBody(tn, "60")), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	gitT(t, dir, "add", ".")
	gitT(t, dir, "commit", "-q", "-m", "seed")
	if err := os.WriteFile(filepath.Join(dir, oobOther+".yaml"), []byte(oobBody(oobOther, "99")), 0o644); err != nil {
		t.Fatal(err)
	}
	gitT(t, dir, "add", oobOther+".yaml")
	return dir
}

// assertOOBStillStaged: the operator's change is still staged, with its
// bytes, and is not in HEAD.
func assertOOBStillStaged(t *testing.T, dir string) {
	t.Helper()
	if got := strings.TrimSpace(gitT(t, dir, "diff", "--cached", "--name-only", "--", oobOther+".yaml")); got != oobOther+".yaml" {
		t.Errorf("the out-of-band staged change is no longer staged (diff --cached = %q)", got)
	}
	if got := gitT(t, dir, "show", ":"+oobOther+".yaml"); got != oobBody(oobOther, "99") {
		t.Errorf("staged bytes of %s = %q, want the operator's", oobOther, got)
	}
	if got := gitT(t, dir, "show", "HEAD:"+oobOther+".yaml"); got != oobBody(oobOther, "60") {
		t.Errorf("HEAD's %s = %q: the unjudged change was committed", oobOther, got)
	}
}

func TestDirectWrite_CommitsOnlyItsOwnPath(t *testing.T) {
	t.Parallel()
	body := oobBody(oobWritten, "70")
	for name, run := range map[string]func(*Writer) error{
		"Write": func(w *Writer) error {
			_, err := w.Write(context.Background(), oobWritten, "alice@example.com", body)
			return err
		},
		"WriteMerged": func(w *Writer) error {
			_, err := w.WriteMerged(context.Background(), oobWritten, "alice@example.com",
				func([]byte) (string, error) { return body, nil })
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := oobRepo(t)
			w := newTestWriter(dir, dir)
			w.SetValidator(&recordingValidator{})
			base := gitRevParse(t, dir, "HEAD")

			if err := run(w); err != nil {
				t.Fatalf("write: %v", err)
			}
			if gitRevParse(t, dir, "HEAD~1") != base {
				t.Fatal("the write did not commit onto the base")
			}
			if got := strings.TrimSpace(gitT(t, dir, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")); got != oobWritten+".yaml" {
				t.Errorf("the tenant's commit holds %q, want only %s.yaml", got, oobWritten)
			}
			assertOOBStillStaged(t, dir)
			// The real index follows the commit for the written path: no
			// phantom staged revert of it in `git status`.
			if st := gitT(t, dir, "status", "--porcelain", "--", oobWritten+".yaml"); st != "" {
				t.Errorf("git status shows the written path: %q", st)
			}
			if got := gitT(t, dir, "show", ":"+oobWritten+".yaml"); got != body {
				t.Errorf("index entry of the written path = %q, want the committed bytes", got)
			}
		})
	}
}

// A write that does not commit leaves the operator's staged change alone:
// the generator refusing it, HEAD moving while it was judged, and the
// compare-and-swap failing on a held ref lock.
func TestDirectWrite_FailureLeavesOutOfBandStagedChange(t *testing.T) {
	t.Parallel()
	body := oobBody(oobWritten, "70")
	for name, tc := range map[string]struct {
		setup func(t *testing.T, dir string, w *Writer)
		check func(t *testing.T, err error)
	}{
		"generator refuses": {
			setup: func(t *testing.T, dir string, w *Writer) {
				w.SetValidator(&recordingValidator{err: errFakeRefusal})
			},
			check: func(t *testing.T, err error) {
				if !errors.Is(err, errFakeRefusal) {
					t.Fatalf("err = %v, want the refusal", err)
				}
			},
		},
		"HEAD moved while judged": {
			setup: func(t *testing.T, dir string, w *Writer) {
				w.SetValidator(&recordingValidator{})
				w.beforeCASAdvance = func() {
					// git commit takes the staged operator change into this
					// outside commit too, so commit-tree an unrelated one.
					tree := strings.TrimSpace(gitT(t, dir, "rev-parse", "HEAD^{tree}"))
					c := strings.TrimSpace(gitT(t, dir, "commit-tree", tree, "-p", "HEAD", "-m", "outside"))
					gitT(t, dir, "update-ref", "HEAD", c)
				}
			},
			check: func(t *testing.T, err error) {
				if !errors.Is(err, ErrConflict) {
					t.Fatalf("err = %v, want ErrConflict", err)
				}
			},
		},
		// The real index's entry is synced BEFORE HEAD moves, so a held
		// index.lock still refuses the write as lock contention with
		// nothing published — not a published commit behind a stale entry.
		"index lock held": {
			setup: func(t *testing.T, dir string, w *Writer) {
				w.SetValidator(&recordingValidator{})
				lock := filepath.Join(dir, ".git", "index.lock")
				if err := os.WriteFile(lock, nil, 0o644); err != nil {
					t.Fatal(err)
				}
				head := gitRevParse(t, dir, "HEAD")
				t.Cleanup(func() { _ = os.Remove(lock) })
				w.beforeCASAdvance = func() { t.Error("reached the ref update with the index locked") }
				w.SetOnWrite(func(string) {
					if gitRevParse(t, dir, "HEAD") != head {
						t.Error("published with the index locked")
					}
				})
			},
			check: func(t *testing.T, err error) {
				if !errors.Is(err, ErrWriteOverloaded) {
					t.Fatalf("err = %v, want ErrWriteOverloaded", err)
				}
			},
		},
		"ref lock held": {
			setup: func(t *testing.T, dir string, w *Writer) {
				w.SetValidator(&recordingValidator{})
				lock := filepath.Join(dir, ".git", "refs", "heads", "main.lock")
				w.beforeCASAdvance = func() {
					if err := os.WriteFile(lock, nil, 0o644); err != nil {
						t.Errorf("create ref lock: %v", err)
					}
				}
				t.Cleanup(func() { _ = os.Remove(lock) })
			},
			check: func(t *testing.T, err error) {
				if err == nil || errors.Is(err, ErrConflict) {
					t.Fatalf("err = %v, want the ref-update failure", err)
				}
			},
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := oobRepo(t)
			w := newTestWriter(dir, dir)
			tc.setup(t, dir, w)
			_, err := w.Write(context.Background(), oobWritten, "alice@example.com", body)
			tc.check(t, err)
			assertOOBStillStaged(t, dir)
			got, rerr := os.ReadFile(filepath.Join(dir, oobWritten+".yaml"))
			if rerr != nil || string(got) != oobBody(oobWritten, "60") {
				t.Errorf("serving file = %q (%v), want HEAD's", got, rerr)
			}
			if st := gitT(t, dir, "diff", "--cached", "--name-only", "--", oobWritten+".yaml"); st != "" {
				t.Errorf("the failed write left its path staged: %q", st)
			}
		})
	}
}

// The operator staged a change to the SAME file the API then writes. A
// write that fails after its file was written (here: the compare-and-swap,
// on a held ref lock) leaves that staged entry exactly as it was — the
// write never touched the real index; a write that commits replaces it
// with the committed bytes, as `git commit --only <path>` does.
func TestDirectWrite_SamePathStagedEntry(t *testing.T) {
	t.Parallel()
	staged := oobBody(oobWritten, "88")
	body := oobBody(oobWritten, "70")
	for _, fail := range []bool{true, false} {
		dir := oobRepo(t)
		if err := os.WriteFile(filepath.Join(dir, oobWritten+".yaml"), []byte(staged), 0o644); err != nil {
			t.Fatal(err)
		}
		gitT(t, dir, "add", oobWritten+".yaml")
		w := newTestWriter(dir, dir)
		w.SetValidator(&recordingValidator{})
		lock := filepath.Join(dir, ".git", "refs", "heads", "main.lock")
		if fail {
			w.beforeCASAdvance = func() {
				if err := os.WriteFile(lock, nil, 0o644); err != nil {
					t.Errorf("create ref lock: %v", err)
				}
			}
		}
		_, err := w.Write(context.Background(), oobWritten, "alice@example.com", body)
		_ = os.Remove(lock)
		if fail != (err != nil) {
			t.Fatalf("fail=%v: err = %v", fail, err)
		}
		want := body
		if fail {
			want = staged
		}
		if got := gitT(t, dir, "show", ":"+oobWritten+".yaml"); got != want {
			t.Errorf("fail=%v: staged entry = %q, want %q", fail, got, want)
		}
		assertOOBStillStaged(t, dir)
	}
}

// Batches commit in their own staging worktree (its own index): pinned so
// a change there cannot reintroduce the ride-along. The serving index keeps
// the operator's change; the batch's path follows the commit.
func TestWriteBatch_CommitsOnlyItsOwnPaths(t *testing.T) {
	t.Parallel()
	dir := oobRepo(t)
	w := newTestWriter(dir, dir)
	w.SetValidator(&recordingValidator{})
	body := oobBody(oobWritten, "70")
	if _, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: oobWritten,
		Merge: func([]byte) (string, error) { return body, nil }}}, "alice@example.com"); err != nil {
		t.Fatalf("batch: %v", err)
	}
	if got := strings.TrimSpace(gitT(t, dir, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")); got != oobWritten+".yaml" {
		t.Errorf("the batch commit holds %q, want only %s.yaml", got, oobWritten)
	}
	assertOOBStillStaged(t, dir)
	if st := gitT(t, dir, "status", "--porcelain", "--", oobWritten+".yaml"); st != "" {
		t.Errorf("git status shows the batch's path: %q", st)
	}
}

// #1535 item 5: the validator is required. A Writer without one refuses
// every content-changing write with ErrNoValidator — direct and PR mode,
// single and batch — and nothing is committed or left in the serving tree.
// (Before, a nil validator took an unjudged `git commit` path.)
func TestWriter_RefusesToWriteWithoutAValidator(t *testing.T) {
	t.Parallel()
	body := oobBody(oobWritten, "70")
	merge := func([]byte) (string, error) { return body, nil }
	for name, run := range map[string]func(*Writer) error{
		"Write": func(w *Writer) error {
			_, err := w.Write(context.Background(), oobWritten, "alice@example.com", body)
			return err
		},
		"WriteMerged": func(w *Writer) error {
			_, err := w.WriteMerged(context.Background(), oobWritten, "alice@example.com", merge)
			return err
		},
		"WriteBatch": func(w *Writer) error {
			_, err := w.WriteBatch(context.Background(), []BatchOp{{TenantID: oobWritten, Merge: merge}}, "alice@example.com")
			return err
		},
		"WritePR": func(w *Writer) error {
			_, err := w.WritePR(context.Background(), oobWritten, "alice@example.com", body)
			return err
		},
		"WritePRBatch": func(w *Writer) error {
			_, err := w.WritePRBatch(context.Background(), []PRBatchOp{{TenantID: oobWritten, Merge: merge}}, "alice@example.com")
			return err
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := initRepoWithDefaults(t)
			if err := os.WriteFile(filepath.Join(dir, oobWritten+".yaml"), []byte(oobBody(oobWritten, "60")), 0o644); err != nil {
				t.Fatal(err)
			}
			gitT(t, dir, "add", ".")
			gitT(t, dir, "commit", "-q", "-m", "seed")
			head := gitRevParse(t, dir, "HEAD")
			w := NewWriter(dir, dir) // no SetValidator

			if err := run(w); !errors.Is(err, ErrNoValidator) {
				t.Fatalf("err = %v, want ErrNoValidator", err)
			}
			if got := gitRevParse(t, dir, "HEAD"); got != head {
				t.Errorf("HEAD moved to %s", got)
			}
			if st := gitT(t, dir, "status", "--porcelain"); st != "" {
				t.Errorf("the refused write left the serving tree changed:\n%s", st)
			}
			if b := gitT(t, dir, "branch", "--list"); strings.Count(b, "\n") != 1 {
				t.Errorf("a branch was left behind:\n%s", b)
			}
		})
	}
}
