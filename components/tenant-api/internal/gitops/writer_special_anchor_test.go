package gitops

// #1723: after a PR-mode write fails to return the tree to base
// (ErrBaseRestore), the worktree stays on the feature branch. Every write that
// goes through commitFileChange used to commit onto that branch and report
// success, and the next WritePR's anchoring checkout then stranded those
// commits. These tests pin the guard in commitFileChange (leavePRBranch).
//
// The oracle is git itself, queried with plain commands on refs — never the
// Writer under test.

import (
	"context"
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

const anchorEmail = "alice@example.com"

// strandOnPRBranch reproduces the #2070 failure: a WritePR whose two
// return-to-base attempts both fail on a held index.lock. It returns the repo,
// the Writer (seam cleared, lock removed), and the stranded branch's name.
func strandOnPRBranch(t *testing.T) (string, *Writer, string) {
	t.Helper()
	dir := initRepoOnMain(t)
	addBareRemote(t, dir)
	w := NewWriter(dir, dir)
	hook, _, unlock := lockIndexOnAttempts(t, dir, 1, 2)
	w.beforeBaseRestore = hook
	if _, err := w.WritePR(context.Background(), "db-a", anchorEmail, validTenantYAML); !errors.Is(err, ErrBaseRestore) {
		t.Fatalf("setup: WritePR err = %v, want ErrBaseRestore", err)
	}
	unlock()
	w.beforeBaseRestore = nil
	branch := gitOut(t, dir, "symbolic-ref", "--short", "HEAD")
	if !strings.HasPrefix(branch, "tenant-api/db-a/") {
		t.Fatalf("setup: HEAD = %q, want the stranded tenant-api/db-a/* branch", branch)
	}
	return dir, w, branch
}

// headBranch reads HEAD's branch name; "" for a detached HEAD.
func headBranch(t *testing.T, dir string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "symbolic-ref", "--short", "-q", "HEAD").Output()
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// anchorWrite is one of the direct-commit paths under test: it writes and
// reports the file (relative to the repo root) and the content it committed.
type anchorWrite struct {
	name    string
	rel     string
	content string
	do      func(w *Writer) error
}

func anchorWrites() []anchorWrite {
	groups := "groups:\n  g1:\n    label: G1\n"
	subset := "metrics:\n  - up\n"
	tenant := "tenants:\n  db-b:\n    _silent_mode: \"warning\"\n"
	return []anchorWrite{
		{"MutateConfigFile/_groups.yaml", "_groups.yaml", groups, func(w *Writer) error {
			return w.MutateConfigFile(context.Background(), "_groups.yaml", "groups", anchorEmail,
				func([]byte) ([]byte, error) { return []byte(groups), nil })
		}},
		{"WriteFederationSubsetFile", "_federation/db-b.yaml", subset, func(w *Writer) error {
			return w.WriteFederationSubsetFile(context.Background(), "db-b", anchorEmail, subset)
		}},
		{"Write/tenant", "db-b.yaml", tenant, func(w *Writer) error {
			_, err := w.Write(context.Background(), "db-b", anchorEmail, tenant)
			return err
		}},
	}
}

// (i) The tree is stranded, returning to base works: the write lands on base,
// the feature branch gains nothing, HEAD is base.
func TestCommitFileChange_StrandedOnPRBranch_ReturnsToBaseAndCommitsThere(t *testing.T) {
	for _, tc := range anchorWrites() {
		t.Run(tc.name, func(t *testing.T) {
			dir, w, branch := strandOnPRBranch(t)
			mainBefore := gitOut(t, dir, "rev-parse", "main")
			branchBefore := gitOut(t, dir, "rev-parse", branch)

			if err := tc.do(w); err != nil {
				t.Fatalf("write: %v", err)
			}

			if got := headBranch(t, dir); got != "main" {
				t.Errorf("HEAD = %q, want main", got)
			}
			if got := gitOut(t, dir, "rev-parse", branch); got != branchBefore {
				t.Errorf("feature branch moved %s → %s: the write committed onto the stranded branch", branchBefore[:8], got[:8])
			}
			if got := gitOut(t, dir, "rev-parse", "main~1"); got != mainBefore {
				t.Errorf("main~1 = %s, want the previous main tip %s (exactly one new commit on base)", got[:8], mainBefore[:8])
			}
			if got := gitOut(t, dir, "show", "main:"+tc.rel); got != strings.TrimSpace(tc.content) {
				t.Errorf("main:%s = %q, want %q", tc.rel, got, tc.content)
			}
			if st := gitOut(t, dir, "status", "--porcelain"); st != "" {
				t.Errorf("working tree dirty after the write:\n%s", st)
			}
		})
	}
}

// (ii) The tree is stranded and returning to base fails too: nothing is
// written anywhere, and the error is ErrTreeNotOnBase — still carrying the
// lock contention as ErrWriteOverloaded, both of which mean "retry".
func TestCommitFileChange_StrandedOnPRBranch_ReturnFailsWritesNothing(t *testing.T) {
	for _, tc := range anchorWrites() {
		t.Run(tc.name, func(t *testing.T) {
			dir, w, branch := strandOnPRBranch(t)
			mainBefore := gitOut(t, dir, "rev-parse", "main")
			branchBefore := gitOut(t, dir, "rev-parse", branch)
			lockPath := filepath.Join(dir, ".git", "index.lock")
			if err := os.WriteFile(lockPath, nil, 0o644); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = os.Remove(lockPath) })

			err := tc.do(w)
			if !errors.Is(err, ErrTreeNotOnBase) {
				t.Fatalf("err = %v, want ErrTreeNotOnBase", err)
			}
			if !errors.Is(err, ErrWriteOverloaded) {
				t.Errorf("err = %v: the index.lock contention no longer reads as ErrWriteOverloaded", err)
			}
			if errors.Is(err, ErrBaseRestore) {
				t.Errorf("err = %v carries ErrBaseRestore, whose contract is \"do not retry\"", err)
			}

			if got := gitOut(t, dir, "rev-parse", "main"); got != mainBefore {
				t.Errorf("main moved although the write failed")
			}
			if got := gitOut(t, dir, "rev-parse", branch); got != branchBefore {
				t.Errorf("feature branch moved although the write failed")
			}
			if _, statErr := os.Stat(filepath.Join(dir, tc.rel)); !os.IsNotExist(statErr) {
				t.Errorf("%s exists on disk (stat err %v); the refused write must not touch the tree", tc.rel, statErr)
			}

			// Recovery: with the lock gone, the same write lands on base.
			_ = os.Remove(lockPath)
			if err := tc.do(w); err != nil {
				t.Fatalf("retry after the lock cleared: %v", err)
			}
			if got := gitOut(t, dir, "show", "main:"+tc.rel); got != strings.TrimSpace(tc.content) {
				t.Errorf("after retry main:%s = %q, want %q", tc.rel, got, tc.content)
			}
		})
	}
}

// The target file differs between the stranded branch and base (the PR's own
// tenant): the caller read the branch's copy, so even after returning to base
// nothing may be written — a retry reads base's copy.
func TestCommitFileChange_StrandedOnPRBranch_TargetDiffersOnBaseIsRefused(t *testing.T) {
	dir, w, branch := strandOnPRBranch(t)
	mainBefore := gitOut(t, dir, "rev-parse", "main")
	branchBefore := gitOut(t, dir, "rev-parse", branch)
	body := "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n"

	_, err := w.Write(context.Background(), "db-a", anchorEmail, body)
	if !errors.Is(err, ErrTreeNotOnBase) {
		t.Fatalf("err = %v, want ErrTreeNotOnBase", err)
	}
	if got := headBranch(t, dir); got != "main" {
		t.Errorf("HEAD = %q, want main (the return to base itself succeeded)", got)
	}
	if got := gitOut(t, dir, "rev-parse", "main"); got != mainBefore {
		t.Errorf("main moved although the write was refused")
	}
	if got := gitOut(t, dir, "rev-parse", branch); got != branchBefore {
		t.Errorf("feature branch moved although the write was refused")
	}

	if _, err := w.Write(context.Background(), "db-a", anchorEmail, body); err != nil {
		t.Fatalf("retry on base: %v", err)
	}
	if got := gitOut(t, dir, "show", "main:db-a.yaml"); got != strings.TrimSpace(body) {
		t.Errorf("after retry main:db-a.yaml = %q", got)
	}
}

// Direct mode commits on whatever branch the operator checked out. A branch
// outside the PR namespace, or a detached HEAD, is left alone.
func TestCommitFileChange_NonPRBranchIsLeftAlone(t *testing.T) {
	for _, tc := range []struct {
		name     string
		checkout []string
		wantHEAD string
	}{
		{"feature/x", []string{"checkout", "-b", "feature/x"}, "feature/x"},
		{"detached", []string{"checkout", "--detach"}, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			dir := initRepoOnMain(t)
			gitRun(t, dir, tc.checkout...)
			mainBefore := gitOut(t, dir, "rev-parse", "main")
			headBefore := gitOut(t, dir, "rev-parse", "HEAD")
			w := NewWriter(dir, dir)
			body := "tenants:\n  db-b:\n    _silent_mode: \"warning\"\n"

			if _, err := w.Write(context.Background(), "db-b", anchorEmail, body); err != nil {
				t.Fatalf("Write: %v", err)
			}
			if got := headBranch(t, dir); got != tc.wantHEAD {
				t.Errorf("HEAD branch = %q, want %q (the guard switched branches)", got, tc.wantHEAD)
			}
			if got := gitOut(t, dir, "rev-parse", "HEAD~1"); got != headBefore {
				t.Errorf("HEAD~1 = %s, want %s (one commit on the operator's HEAD)", got[:8], headBefore[:8])
			}
			if got := gitOut(t, dir, "show", "HEAD:db-b.yaml"); got != strings.TrimSpace(body) {
				t.Errorf("HEAD:db-b.yaml = %q", got)
			}
			if got := gitOut(t, dir, "rev-parse", "main"); got != mainBefore {
				t.Errorf("main moved: the write did not stay on the operator's HEAD")
			}
		})
	}
}
