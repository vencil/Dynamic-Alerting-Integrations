package gitops

// #1535 review: the real-index bookkeeping of gitCommitOnto
//
//   - with the Writer rooted in a SUBDIRECTORY of the repository (NewWriter
//     with gitDir "" roots git at conf.d): every real-index path is the
//     top-level-relative one, so the written path's entry is the right one
//     and nothing else appears in the index;
//   - in the window between syncing the real index and the ref update, a
//     change made to that entry from outside (an operator's `git add`, or a
//     `git commit` that took our blob) is left alone when the CAS fails.
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

// subdirRepo is a repository whose conf.d is the subdirectory "conf.d",
// holding both oob tenants committed, with the other tenant's file changed
// and staged out of band. Returns the top level and conf.d.
func subdirRepo(t *testing.T) (top, conf string) {
	t.Helper()
	top = t.TempDir()
	conf = filepath.Join(top, "conf.d")
	if err := os.Mkdir(conf, 0o755); err != nil {
		t.Fatal(err)
	}
	files := map[string]string{
		"_defaults.yaml":     "defaults:\n  container_cpu: 80\n  container_memory: 85\n  mysql_threads_running: 80\n",
		oobWritten + ".yaml": oobBody(oobWritten, "60"),
		oobOther + ".yaml":   oobBody(oobOther, "60"),
	}
	for n, b := range files {
		if err := os.WriteFile(filepath.Join(conf, n), []byte(b), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	for _, args := range [][]string{{"init", "-q", "-b", "main"}, {"config", "user.email", "t@t.com"},
		{"config", "user.name", "T"}, {"add", "."}, {"commit", "-q", "-m", "seed"}} {
		gitT(t, top, args...)
	}
	if err := os.WriteFile(filepath.Join(conf, oobOther+".yaml"), []byte(oobBody(oobOther, "99")), 0o644); err != nil {
		t.Fatal(err)
	}
	gitT(t, top, "add", "conf.d/"+oobOther+".yaml")
	return top, conf
}

// realIndex is the whole real index, `ls-files -s` from the top level.
func realIndex(t *testing.T, top string) string {
	t.Helper()
	return gitT(t, top, "ls-files", "-s")
}

func TestDirectWrite_SubdirGitDirKeepsTheRealIndexExact(t *testing.T) {
	t.Parallel()
	body := oobBody(oobWritten, "70")
	for _, fail := range []bool{false, true} {
		top, conf := subdirRepo(t)
		w := newTestWriter(conf, "") // git rooted at conf.d, as with --git-dir unset
		lock := filepath.Join(top, ".git", "refs", "heads", "main.lock")
		if fail {
			w.beforeCASAdvance = func() {
				if err := os.WriteFile(lock, nil, 0o644); err != nil {
					t.Errorf("create ref lock: %v", err)
				}
			}
		}
		before := realIndex(t, top)
		_, err := w.Write(context.Background(), oobWritten, "alice@example.com", body)
		_ = os.Remove(lock)
		if fail != (err != nil) {
			t.Fatalf("fail=%v: err = %v", fail, err)
		}
		if strings.Contains(realIndex(t, top), "../") || strings.Count(realIndex(t, top), "\n") != 3 {
			t.Errorf("fail=%v: the real index holds other entries:\n%s", fail, realIndex(t, top))
		}
		st := gitT(t, top, "status", "--porcelain")
		if want := "M  conf.d/" + oobOther + ".yaml\n"; st != want {
			t.Errorf("fail=%v: status = %q, want only the operator's staged change %q", fail, st, want)
		}
		if fail {
			if got := realIndex(t, top); got != before {
				t.Errorf("the failed write changed the real index:\n%s\nwas:\n%s", got, before)
			}
			continue
		}
		if got := strings.TrimSpace(gitT(t, top, "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD")); got != "conf.d/"+oobWritten+".yaml" {
			t.Errorf("the commit holds %q", got)
		}
		if got := gitT(t, top, "show", ":conf.d/"+oobWritten+".yaml"); got != body {
			t.Errorf("index entry of the written path = %q, want the committed bytes", got)
		}
	}
}

// A NEW tenant file whose write fails at the CAS: the real index had no
// entry for it, and has none afterwards (the sync's entry is taken out by
// its top-level-relative path).
func TestDirectWrite_SubdirGitDirNewFileCASFailureLeavesNoEntry(t *testing.T) {
	t.Parallel()
	const fresh = "tn-oob-fresh"
	top, conf := subdirRepo(t)
	w := newTestWriter(conf, "")
	lock := filepath.Join(top, ".git", "refs", "heads", "main.lock")
	w.beforeCASAdvance = func() {
		if err := os.WriteFile(lock, nil, 0o644); err != nil {
			t.Errorf("create ref lock: %v", err)
		}
	}
	before := realIndex(t, top)
	_, err := w.Write(context.Background(), fresh, "alice@example.com", oobBody(fresh, "70"))
	_ = os.Remove(lock)
	if err == nil {
		t.Fatal("the write succeeded with the ref locked")
	}
	if got := realIndex(t, top); got != before {
		t.Errorf("the failed write left the real index changed:\n%s\nwas:\n%s", got, before)
	}
	if st, want := gitT(t, top, "status", "--porcelain"), "M  conf.d/"+oobOther+".yaml\n"; st != want {
		t.Errorf("status = %q, want %q", st, want)
	}
}

// What happens to the written path's real-index entry when, between the
// sync and the ref update, something outside changes it.
func TestDirectWrite_CASFailureLeavesAnEntryChangedFromOutside(t *testing.T) {
	t.Parallel()
	body := oobBody(oobWritten, "70")
	outside := oobBody(oobWritten, "55")
	for name, tc := range map[string]struct {
		window    func(t *testing.T, dir string)
		wantErr   error
		wantEntry func(dir string) string
	}{
		// An operator stages other bytes for the path; the CAS then fails
		// on a ref lock. Their staged bytes stay.
		"outside add": {
			window: func(t *testing.T, dir string) {
				if err := os.WriteFile(filepath.Join(dir, oobWritten+".yaml"), []byte(outside), 0o644); err != nil {
					t.Fatal(err)
				}
				gitT(t, dir, "add", oobWritten+".yaml")
				if err := os.WriteFile(filepath.Join(dir, ".git", "refs", "heads", "main.lock"), nil, 0o644); err != nil {
					t.Fatal(err)
				}
			},
			wantEntry: func(string) string { return outside },
		},
		// An outside `git commit` takes the real index — our blob included
		// — and moves HEAD: the entry already equals HEAD and stays.
		"outside commit": {
			window: func(t *testing.T, dir string) {
				gitT(t, dir, "commit", "-q", "-m", "outside")
			},
			wantErr:   ErrConflict,
			wantEntry: func(string) string { return body },
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := oobRepo(t)
			gitT(t, dir, "reset", "-q") // no other staged change here
			w := newTestWriter(dir, dir)
			w.beforeCASAdvance = func() { tc.window(t, dir) }
			t.Cleanup(func() { _ = os.Remove(filepath.Join(dir, ".git", "refs", "heads", "main.lock")) })
			_, err := w.Write(context.Background(), oobWritten, "alice@example.com", body)
			if err == nil || (tc.wantErr != nil && !errors.Is(err, tc.wantErr)) {
				t.Fatalf("err = %v, want a failed CAS (%v)", err, tc.wantErr)
			}
			if got, want := gitT(t, dir, "show", ":"+oobWritten+".yaml"), tc.wantEntry(dir); got != want {
				t.Errorf("index entry = %q, want %q", got, want)
			}
			if name == "outside commit" {
				if st := gitT(t, dir, "diff", "--cached", "--name-only"); st != "" {
					t.Errorf("a staged revert of the outside commit was left: %q", st)
				}
			}
		})
	}
}
