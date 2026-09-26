package gitops

// #1988 D1 PR-3 (review R1 / R4 / R7b): TreeOnBase is a READ that runs under
// the writer lock, so it must neither leave a lock behind for the next write
// nor call a tree dirty for files the loader never reads.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func writeIn(t *testing.T, dir, rel, body string) {
	t.Helper()
	p := filepath.Join(dir, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func gitIn(t *testing.T, dir string, args ...string) {
	t.Helper()
	if out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).CombinedOutput(); err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
}

func treeOnBase(w *Writer) error {
	var err error
	if !w.TryWithTreeLock(func() { err = w.TreeOnBase() }) {
		return fmt.Errorf("tree lock busy")
	}
	return err
}

// R4: only changes the loader would read count; other untracked files do not.
func TestTreeOnBase_OnlyLoaderVisibleChangesCount(t *testing.T) {
	cases := []struct {
		name  string
		setup func(t *testing.T, repo string)
		dirty bool
	}{
		{"clean", func(*testing.T, string) {}, false},
		{"hidden temp leftover", func(t *testing.T, repo string) { writeIn(t, repo, ".db-a.yaml.tmp-123", "x") }, false},
		{"non-yaml leftover", func(t *testing.T, repo string) { writeIn(t, repo, "db-a.yaml.tmp", "x") }, false},
		{"file under a hidden dir", func(t *testing.T, repo string) { writeIn(t, repo, ".cache/db-z.yaml", "x") }, false},
		{"untracked tenant file", func(t *testing.T, repo string) { writeIn(t, repo, "db-new.yaml", "tenants: {}\n") }, true},
		{"untracked nested yaml", func(t *testing.T, repo string) { writeIn(t, repo, "team/db-n.YML", "tenants: {}\n") }, true},
		// R7b: a tracked modification is dirt whatever its name.
		{"tracked modification", func(t *testing.T, repo string) { writeIn(t, repo, "db-a.yaml", "tenants: {db-a: {}}\n") }, true},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			repo := initRepoOnMain(t)
			writeIn(t, repo, "db-a.yaml", "tenants:\n  db-a: {}\n")
			gitIn(t, repo, "add", "-A")
			gitIn(t, repo, "commit", "-m", "seed")
			c.setup(t, repo)
			err := treeOnBase(NewWriter(repo, repo))
			if got := err != nil; got != c.dirty {
				t.Fatalf("TreeOnBase = %v, want dirty=%v", err, c.dirty)
			}
			if err != nil && !errors.Is(err, ErrTreeNotOnBase) {
				t.Errorf("error %v does not wrap ErrTreeNotOnBase", err)
			}
		})
	}
}

// R4: an untracked file outside conf.d (gitDir != configDir) is not the
// loader's business.
func TestTreeOnBase_UntrackedOutsideConfDirIgnored(t *testing.T) {
	repo := initRepoOnMain(t)
	writeIn(t, repo, "conf.d/db-a.yaml", "tenants:\n  db-a: {}\n")
	gitIn(t, repo, "add", "-A")
	gitIn(t, repo, "commit", "-m", "seed")
	writeIn(t, repo, "docs/other.yaml", "x: 1\n")
	if err := treeOnBase(NewWriter(filepath.Join(repo, "conf.d"), repo)); err != nil {
		t.Errorf("untracked yaml outside conf.d made the tree dirty: %v", err)
	}
	writeIn(t, repo, "conf.d/db-b.yaml", "tenants: {}\n")
	if err := treeOnBase(NewWriter(filepath.Join(repo, "conf.d"), repo)); err == nil {
		t.Error("untracked tenant file inside conf.d was not reported")
	}
}

// hangingStatusGit is a git stub that hangs on `status` and runs the real
// git for everything else.
func hangingStatusGit(t *testing.T) string {
	t.Helper()
	realGit, err := exec.LookPath("git")
	if err != nil {
		t.Skip("git not found")
	}
	stub := filepath.Join(t.TempDir(), "git-hang-on-status.sh")
	script := "#!/bin/sh\nfor a in \"$@\"; do\n  if [ \"$a\" = status ]; then\n    exec sleep 30\n  fi\ndone\nexec '" + realGit + "' \"$@\"\n"
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatal(err)
	}
	return stub
}

// R1 / S8: a reader's timed-out status leaves the repository exactly as it
// found it. It takes no lock (--no-optional-locks), so none can be left
// behind for the next write — and a lock that IS there belongs to someone
// else and must survive the reader's timeout.
func TestTreeOnBase_TimeoutTouchesNoLock(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix stub")
	}
	repo := initRepoOnMain(t)
	w := NewWriter(repo, repo)
	w.gitBinary = hangingStatusGit(t)
	w.gitTimeout = 300 * time.Millisecond
	w.gitWaitDelay = 300 * time.Millisecond

	// A foreign writer's lock.
	lock := filepath.Join(repo, ".git", "index.lock")
	if err := os.WriteFile(lock, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	err := treeOnBase(w)
	if _, serr := os.Stat(lock); serr != nil {
		t.Errorf("a reader's timeout removed another process's index.lock: %v", serr)
	}
	if err == nil || !strings.Contains(err.Error(), "timed out") {
		t.Errorf("TreeOnBase = %v, want a timeout", err)
	} else if strings.Contains(err.Error(), "write lock released") {
		t.Errorf("a read-only timeout claims to release the write lock: %v", err)
	}

	// Without a foreign lock, a write after the reader's timeout proceeds.
	if err := os.Remove(lock); err != nil {
		t.Fatal(err)
	}
	_ = treeOnBase(w)
	w.gitBinary = "git"
	w.gitTimeout = 0
	if _, err := w.Write(context.Background(), "db-a", "alice@example.com", validTenantYAML); err != nil {
		t.Errorf("Write after a timed-out TreeOnBase: %v", err)
	}
}

// S7: paths are resolved against the repository toplevel and the real
// conf.d, and gitignored files the loader reads count.
func TestTreeOnBase_ResolvesPathsLikeTheLoader(t *testing.T) {
	type layout struct {
		gitDir, configDir string // relative to the repo toplevel ("" = toplevel)
		linkConf          bool   // reach configDir through a symlink
	}
	cases := []struct {
		name   string
		layout layout
		extra  map[string]string // repo-relative files written after the seed commit
		dirty  bool
	}{
		{"gitignored yaml in conf.d", layout{"", "conf.d", false},
			map[string]string{".gitignore": "*.local.yaml\n", "conf.d/db-x.local.yaml": "tenants: {}\n"}, true},
		{"gitignored directory in conf.d", layout{"", "conf.d", false},
			map[string]string{".gitignore": "gen/\n", "conf.d/gen/db-g.yaml": "tenants: {}\n"}, true},
		{"gitignored non-config file", layout{"", "conf.d", false},
			map[string]string{".gitignore": "*.log\n", "conf.d/run.log": "x"}, false},
		{"gitDir below the toplevel", layout{"sub", "sub/conf.d", false},
			map[string]string{"sub/conf.d/db-u.yaml": "tenants: {}\n"}, true},
		{"gitDir below the toplevel, change elsewhere", layout{"sub", "sub/conf.d", false},
			map[string]string{"other.yaml": "x: 1\n"}, false},
		{"symlinked configDir", layout{"", "conf.d", true},
			map[string]string{"conf.d/db-s.yaml": "tenants: {}\n"}, true},
		{"conf.d under a subdirectory", layout{"", "deploy/conf.d", false},
			map[string]string{"deploy/conf.d/db-d.yaml": "tenants: {}\n"}, true},
		{"sibling of a nested conf.d", layout{"", "deploy/conf.d", false},
			map[string]string{"deploy/values.yaml": "x: 1\n"}, false},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			top := initRepoOnMain(t)
			writeIn(t, top, filepath.Join(c.layout.configDir, "db-a.yaml"), "tenants:\n  db-a: {}\n")
			gitIn(t, top, "add", "-A")
			gitIn(t, top, "commit", "-m", "seed")
			for rel, body := range c.extra {
				writeIn(t, top, rel, body)
			}
			confDir := filepath.Join(top, c.layout.configDir)
			if c.layout.linkConf {
				link := filepath.Join(t.TempDir(), "conf-link")
				if err := os.Symlink(confDir, link); err != nil {
					t.Skipf("symlink: %v", err)
				}
				confDir = link
			}
			err := treeOnBase(NewWriter(confDir, filepath.Join(top, c.layout.gitDir)))
			if got := err != nil; got != c.dirty {
				t.Errorf("TreeOnBase = %v, want dirty=%v", err, c.dirty)
			}
		})
	}
}

// R1, lock-free layer: TreeOnBase's status never takes the index lock, even
// when the stat data is stale and plain `git status` would write it back.
func TestTreeOnBase_StatusTakesNoIndexLock(t *testing.T) {
	repo := initRepoOnMain(t)
	const n = 30000
	for i := 0; i < n; i++ {
		if err := os.WriteFile(filepath.Join(repo, fmt.Sprintf("f%05d.txt", i)), nil, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	gitIn(t, repo, "add", "-A")
	gitIn(t, repo, "commit", "-q", "-m", "many")
	// Stale stat data: plain status would refresh and write the index back
	// under index.lock.
	later := time.Now().Add(time.Minute)
	for i := 0; i < n; i++ {
		_ = os.Chtimes(filepath.Join(repo, fmt.Sprintf("f%05d.txt", i)), later, later)
	}
	lock := filepath.Join(repo, ".git", "index.lock")
	done := make(chan error, 1)
	w := NewWriter(repo, repo)
	go func() { done <- treeOnBase(w) }()
	seen := false
	for {
		select {
		case err := <-done:
			if err != nil {
				t.Errorf("TreeOnBase on a clean tree: %v", err)
			}
			if seen {
				t.Error("TreeOnBase's status took .git/index.lock")
			}
			return
		default:
			if _, err := os.Stat(lock); err == nil {
				seen = true
			}
			time.Sleep(100 * time.Microsecond)
		}
	}
}
