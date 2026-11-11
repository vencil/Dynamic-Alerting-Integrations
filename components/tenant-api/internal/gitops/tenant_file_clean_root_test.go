package gitops

// #3685: a direct write's key checks read the root platform files from the
// serving worktree, while the shared key verdict judges the HEAD the commit
// lands on. A root `_` file that differs from HEAD is refused like the
// tenant file itself (#1535 item 6): nothing written, HEAD unmoved.
//
// Seams: none — real git repositories (winRepo), every direct write path
// (winWriters).

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// commitRoot commits what a fixture wrote before the repository's first
// commit (initGitRepo commits nothing), so the root files are HEAD's.
func commitRoot(t *testing.T, dir string) {
	t.Helper()
	gitT(t, dir, "add", "-A")
	gitT(t, dir, "commit", "-q", "-m", "seed root")
}

func rootWrite(t *testing.T, dir, name, content string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestDirty_RootPlatformFileDiffersFromHEAD(t *testing.T) {
	t.Parallel()
	x := winBody(winTenant, "90", "")
	served := winBody(winTenant, "60", "")
	for _, c := range []struct {
		name  string
		setup func(t *testing.T, dir string)
	}{
		{"root _defaults.yaml modified", func(t *testing.T, dir string) {
			rootWrite(t, dir, "_defaults.yaml", "defaults:\n  container_cpu: 81\n")
		}},
		{"root _defaults.yaml deleted", func(t *testing.T, dir string) {
			if err := os.Remove(filepath.Join(dir, "_defaults.yaml")); err != nil {
				t.Fatal(err)
			}
		}},
		{"untracked root platform file", func(t *testing.T, dir string) {
			rootWrite(t, dir, "_platform.yaml", "tenants:\n  "+winTenant+":\n    container_cpu: \"70\"\n")
		}},
		{"file under a root _ directory", func(t *testing.T, dir string) {
			if err := os.MkdirAll(filepath.Join(dir, "_team"), 0o755); err != nil {
				t.Fatal(err)
			}
			rootWrite(t, dir, "_team/_defaults.yaml", "defaults:\n  container_cpu: 70\n")
		}},
		{"staged root change", func(t *testing.T, dir string) {
			rootWrite(t, dir, "_defaults.yaml", "defaults:\n  container_cpu: 82\n")
			gitT(t, dir, "add", "--", "_defaults.yaml")
		}},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			setup := func(t *testing.T) string {
				dir := winRepo(t)
				c.setup(t, dir)
				return dir
			}
			winWriters(t, setup, func([]byte) (string, error) { return x, nil }, x, func(t *testing.T, dir string, err error) {
				wantDirty(t, dir, err, gitRevParse(t, dir, "HEAD"), served)
			})
		})
	}
}

// Only the root's `_` entries and the tenant's own file are compared: an
// uncommitted edit of another tenant's file, or of a `_` file in a
// subdirectory, does not refuse the write.
func TestDirty_OtherFilesDoNotRefuse(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	if err := os.MkdirAll(filepath.Join(dir, "team"), 0o755); err != nil {
		t.Fatal(err)
	}
	rootWrite(t, dir, "team/_defaults.yaml", "defaults:\n  container_cpu: 70\n")
	winOutsideEdit(t, dir, winOther, winBody(winOther, "61", ""))
	if _, err := newTestWriter(dir, dir).Write(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", "")); err != nil {
		t.Fatalf("write refused: %v", err)
	}
}

// The consequence #3685 names: an uncommitted root edit that drops a key's
// declaration took the key out of the write's dead-entry check (the shared
// verdict leaves dead_entry to it), so a dead schedule entry was committed
// onto a HEAD that still declares the key. Now the write is refused.
func TestDirty_RootEditCannotSlipADeadEntryIn(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	head := gitRevParse(t, dir, "HEAD")
	rootWrite(t, dir, "_defaults.yaml", "defaults:\n  container_cpu: 80\n")
	dead := "tenants:\n  " + winTenant + ":\n    mysql_threads_running:\n      default: \"60\"\n      overrides:\n" +
		"        - window: \"22:00-06:00\"\n          value: \"90\"\n" +
		"        - window: \"01:00-03:00\"\n          value: \"95\"\n"
	_, err := newTestWriter(dir, dir).Write(context.Background(), winTenant, "alice@example.com", dead)
	wantDirty(t, dir, err, head, winBody(winTenant, "60", ""))
	if strings.Contains(gitT(t, dir, "show", "HEAD:"+winTenant+".yaml"), "01:00-03:00") {
		t.Fatal("the dead entry was committed")
	}
}

// conf.d as a subdirectory of the repository (the HEAD listing is of
// `conf.d/`, not of the top level), and a root `_` file that is a symlink to
// a file elsewhere in the repository (its target is compared too).
func TestDirty_RootPlatformFileInASubdirectoryConfD(t *testing.T) {
	t.Parallel()
	seed := func(t *testing.T) (repo, conf string) {
		repo = t.TempDir()
		conf = filepath.Join(repo, "conf.d")
		for _, d := range []string{conf, filepath.Join(repo, "shared")} {
			if err := os.MkdirAll(d, 0o755); err != nil {
				t.Fatal(err)
			}
		}
		rootWrite(t, repo, "shared/platform.yaml", "tenants:\n  "+winTenant+":\n    container_cpu: \"70\"\n")
		rootWrite(t, conf, "_defaults.yaml", "defaults:\n  container_cpu: 80\n  mysql_threads_running: 80\n")
		rootWrite(t, conf, winTenant+".yaml", winBody(winTenant, "60", ""))
		if err := os.Symlink("../shared/platform.yaml", filepath.Join(conf, "_platform.yaml")); err != nil {
			t.Skipf("symlink: %v", err)
		}
		gitT(t, repo, "init", "-q", "-b", "main")
		gitT(t, repo, "config", "user.email", "t@t.com")
		gitT(t, repo, "config", "user.name", "T")
		commitRoot(t, repo)
		return repo, conf
	}
	for name, dirty := range map[string]func(t *testing.T, repo, conf string){
		"root file deleted": func(t *testing.T, repo, conf string) {
			if err := os.Remove(filepath.Join(conf, "_defaults.yaml")); err != nil {
				t.Fatal(err)
			}
		},
		"symlinked root file's target edited": func(t *testing.T, repo, conf string) {
			rootWrite(t, repo, "shared/platform.yaml", "tenants:\n  "+winTenant+":\n    container_cpu: \"71\"\n")
		},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			repo, conf := seed(t)
			dirty(t, repo, conf)
			head := gitRevParse(t, repo, "HEAD")
			_, err := newTestWriter(conf, repo).Write(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", ""))
			if !errors.Is(err, ErrTenantFileDirty) || gitRevParse(t, repo, "HEAD") != head {
				t.Fatalf("err = %v, want ErrTenantFileDirty and HEAD unmoved", err)
			}
		})
	}
	// Control: the same tree, clean, writes.
	repo, conf := seed(t)
	if _, err := newTestWriter(conf, repo).Write(context.Background(), winTenant, "alice@example.com", winBody(winTenant, "90", "")); err != nil {
		t.Fatalf("clean tree refused: %v", err)
	}
}

// headUnborn is true only for a branch with no commit yet.
func TestHeadUnborn(t *testing.T) {
	t.Parallel()
	born := winRepo(t)
	if u, err := newTestWriter(born, born).headUnborn(born); err != nil || u {
		t.Fatalf("a repository with commits: unborn=%v err=%v", u, err)
	}
	empty := t.TempDir()
	gitT(t, empty, "init", "-q", "-b", "main")
	if u, err := newTestWriter(empty, empty).headUnborn(empty); err != nil || !u {
		t.Fatalf("a repository with no commit: unborn=%v err=%v", u, err)
	}
	// A detached HEAD names a commit: never unborn, and asking is an error
	// (symbolic-ref has no branch to name), so a failed listing stays a failure.
	detached := winRepo(t)
	gitT(t, detached, "checkout", "-q", "--detach")
	if u, err := newTestWriter(detached, detached).headUnborn(detached); err == nil || u {
		t.Fatalf("a detached HEAD: unborn=%v err=%v, want an error", u, err)
	}
}

// A git failure while listing HEAD is an error, never "HEAD holds nothing":
// only a branch with no commit yet skips the listing.
func TestRootPlatformCleanPaths_GitFailureIsAnError(t *testing.T) {
	t.Parallel()
	dir := winRepo(t)
	// HEAD's tree object gone: refs, HEAD and the worktree read as before,
	// only the listing of HEAD fails.
	tree := strings.TrimSpace(gitT(t, dir, "rev-parse", "HEAD^{tree}"))
	if err := os.Remove(filepath.Join(dir, ".git", "objects", tree[:2], tree[2:])); err != nil {
		t.Skipf("tree object not loose: %v", err)
	}
	w := newTestWriter(dir, dir)
	if paths, err := w.rootPlatformCleanPaths(dir); err == nil {
		t.Fatalf("git failing: paths %q, no error", paths)
	}
}
