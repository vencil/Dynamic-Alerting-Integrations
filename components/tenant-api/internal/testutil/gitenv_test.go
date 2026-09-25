package testutil

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"
)

// unsetEnv removes key for the rest of the test and restores its previous
// value (or absence) afterwards; t.Setenv alone can only set a value.
func unsetEnv(t *testing.T, key string) {
	t.Helper()
	t.Setenv(key, "")
	if err := os.Unsetenv(key); err != nil {
		t.Fatal(err)
	}
}

// isolateGit points every place git looks for global config at this test's
// own directories, so the host's config neither leaks in nor gets read by
// accident, and registers GIT_CONFIG_GLOBAL so the os.Setenv that
// DisableGitAutoMaintenance makes is rolled back after the test.
func isolateGit(t *testing.T) (home string) {
	t.Helper()
	home = t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("XDG_CONFIG_HOME", filepath.Join(home, "xdg"))
	t.Setenv("GIT_CONFIG_NOSYSTEM", "1")
	unsetEnv(t, "GIT_CONFIG_GLOBAL")
	return home
}

// disable calls DisableGitAutoMaintenance and schedules its cleanup.
func disable(t *testing.T) {
	t.Helper()
	cleanup, err := DisableGitAutoMaintenance()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(cleanup)
}

func writeFile(t *testing.T, path, content string) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
}

func git(t *testing.T, args ...string) string {
	t.Helper()
	out, err := exec.Command("git", args...).CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v: %s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

// gitGet returns what git reports for key in dir, "" when it is unset.
func gitGet(t *testing.T, dir, key string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "config", "--get", key).Output()
	if err != nil {
		var ee *exec.ExitError
		if errors.As(err, &ee) && ee.ExitCode() == 1 {
			return ""
		}
		t.Fatalf("git config --get %s: %v", key, err)
	}
	return strings.TrimSpace(string(out))
}

func wantAllOff(t *testing.T, where string, get func(key string) string) {
	t.Helper()
	for _, kv := range gitAutoMaintenanceOff {
		if got := get(kv[0]); got != kv[1] {
			t.Errorf("%s: git sees %s = %q, want %q", where, kv[0], got, kv[1])
		}
	}
}

func TestDisableGitAutoMaintenance_GitReadsTheSettings(t *testing.T) {
	isolateGit(t)
	disable(t)
	dir := t.TempDir()
	git(t, "-C", dir, "init", "-q")
	wantAllOff(t, "local repo", func(k string) string { return gitGet(t, dir, k) })
}

// The case that failed in CI: pushing to a bare remote over the local
// transport makes git start receive-pack inside the remote, and receive-pack
// runs its auto gc there while t.TempDir deletes it. git strips
// GIT_CONFIG_COUNT/KEY_n/VALUE_n from the environment of that process, so the
// settings must arrive some other way; the pre-receive hook runs in the same
// environment as receive-pack and records what it sees.
func TestDisableGitAutoMaintenance_RemoteSideOfALocalPushSeesTheSettings(t *testing.T) {
	isolateGit(t)
	disable(t)

	remote := filepath.Join(t.TempDir(), "remote.git")
	git(t, "init", "-q", "--bare", remote)
	seen := filepath.Join(t.TempDir(), "seen")
	var hook strings.Builder
	hook.WriteString("#!/bin/sh\n: > '" + seen + "'\n")
	for _, kv := range gitAutoMaintenanceOff {
		hook.WriteString("printf '%s=%s\\n' " + kv[0] + " \"$(git config --get " + kv[0] + ")\" >> '" + seen + "'\n")
	}
	if err := os.WriteFile(filepath.Join(remote, "hooks", "pre-receive"), []byte(hook.String()), 0o755); err != nil {
		t.Fatal(err)
	}

	work := t.TempDir()
	git(t, "-C", work, "init", "-q")
	git(t, "-C", work, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false",
		"commit", "-q", "--allow-empty", "-m", "c")
	git(t, "-C", work, "push", "-q", remote, "HEAD:refs/heads/main")

	b, err := os.ReadFile(seen)
	if err != nil {
		t.Fatalf("pre-receive hook did not run: %v", err)
	}
	got := map[string]string{}
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		k, v, _ := strings.Cut(line, "=")
		got[k] = v
	}
	wantAllOff(t, "receive-pack in the bare remote", func(k string) string { return got[k] })
}

// A developer's or CI's own global config must keep working, and the
// settings must override the same keys set there.
func TestDisableGitAutoMaintenance_KeepsAnExistingGitConfigGlobal(t *testing.T) {
	home := isolateGit(t)
	user := filepath.Join(home, "user.gitconfig")
	writeFile(t, user, "[user]\n\tname = From Global\n[gc]\n\tauto = 50\n")
	t.Setenv("GIT_CONFIG_GLOBAL", user)
	disable(t)
	if got := os.Getenv("GIT_CONFIG_GLOBAL"); got == user {
		t.Fatalf("GIT_CONFIG_GLOBAL still names the user's file")
	}
	dir := t.TempDir()
	git(t, "-C", dir, "init", "-q")
	if got := gitGet(t, dir, "user.name"); got != "From Global" {
		t.Errorf("user.name = %q, want the value from the previous GIT_CONFIG_GLOBAL", got)
	}
	wantAllOff(t, "with a user GIT_CONFIG_GLOBAL", func(k string) string { return gitGet(t, dir, k) })
}

// With GIT_CONFIG_GLOBAL unset git reads both $XDG_CONFIG_HOME/git/config and
// ~/.gitconfig. Whatever git reported before the call, it must report after.
func TestDisableGitAutoMaintenance_KeepsXDGAndHomeConfig(t *testing.T) {
	home := isolateGit(t)
	writeFile(t, filepath.Join(home, "xdg", "git", "config"), "[x]\n\tboth = xdg\n\tonlyxdg = xdg\n")
	writeFile(t, filepath.Join(home, ".gitconfig"), "[x]\n\tboth = home\n\tonlyhome = home\n")
	dir := t.TempDir()
	git(t, "-C", dir, "init", "-q")
	keys := []string{"x.both", "x.onlyxdg", "x.onlyhome"}
	before := map[string]string{}
	for _, k := range keys {
		before[k] = gitGet(t, dir, k)
		if before[k] == "" {
			t.Fatalf("baseline: git does not read %s from the test's global files", k)
		}
	}
	disable(t)
	for _, k := range keys {
		if got := gitGet(t, dir, k); got != before[k] {
			t.Errorf("%s = %q after the call, %q before", k, got, before[k])
		}
	}
	wantAllOff(t, "with XDG and home config", func(k string) string { return gitGet(t, dir, k) })
}

// GIT_CONFIG_GLOBAL set to "" means git reads no global config; the call must
// not bring ~/.gitconfig back.
func TestDisableGitAutoMaintenance_EmptyGitConfigGlobalStaysEmpty(t *testing.T) {
	home := isolateGit(t)
	writeFile(t, filepath.Join(home, ".gitconfig"), "[x]\n\thome = home\n")
	t.Setenv("GIT_CONFIG_GLOBAL", "")
	disable(t)
	dir := t.TempDir()
	git(t, "-C", dir, "init", "-q")
	if got := gitGet(t, dir, "x.home"); got != "" {
		t.Errorf("x.home = %q: ~/.gitconfig was read although GIT_CONFIG_GLOBAL was empty", got)
	}
	wantAllOff(t, "with an empty GIT_CONFIG_GLOBAL", func(k string) string { return gitGet(t, dir, k) })
}

func TestDisableGitAutoMaintenance_CleanupRemovesTheFile(t *testing.T) {
	isolateGit(t)
	cleanup, err := DisableGitAutoMaintenance()
	if err != nil {
		t.Fatal(err)
	}
	file := os.Getenv("GIT_CONFIG_GLOBAL")
	if _, err := os.Stat(file); err != nil {
		t.Fatalf("config file %q missing before cleanup: %v", file, err)
	}
	cleanup()
	if _, err := os.Stat(filepath.Dir(file)); !os.IsNotExist(err) {
		t.Errorf("cleanup left %s behind (stat err = %v)", filepath.Dir(file), err)
	}
}

// runsGit matches the two ways a test in this module reaches git: passing the
// "git" binary name to exec, or constructing the real gitops writer.
var runsGit = regexp.MustCompile(`"git"|\bNewWriter\(`)

// Every package whose tests run git must call DisableGitAutoMaintenance from
// its TestMain; a new package that forgets it gets the flaky TempDir cleanup
// back. The detection is textual, so it is anchored by a floor: if it stops
// finding the packages it knows about, it fails instead of passing on nothing.
func TestEveryPackageThatRunsGitDisablesAutoMaintenance(t *testing.T) {
	root, err := filepath.Abs(filepath.Join("..", ".."))
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(filepath.Join(root, "go.mod")); err != nil {
		t.Fatalf("module root not found at %s: %v", root, err)
	}
	runs := map[string]bool{}
	calls := map[string]bool{}
	err = filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() || !strings.HasSuffix(path, "_test.go") {
			return nil
		}
		dir, _ := filepath.Rel(root, filepath.Dir(path))
		if filepath.Base(dir) == "testutil" {
			return nil // this package's own tests run git on purpose
		}
		b, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		if runsGit.Match(b) {
			runs[dir] = true
		}
		if strings.Contains(string(b), "testutil.DisableGitAutoMaintenance()") {
			calls[dir] = true
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	var missing []string
	for dir := range runs {
		if !calls[dir] {
			missing = append(missing, dir)
		}
	}
	sort.Strings(missing)
	if len(runs) < 3 {
		t.Fatalf("found only %d packages whose tests run git (%v); expected at least gitops, handler and handler/federation — the detection regexp stopped matching", len(runs), runs)
	}
	if len(missing) > 0 {
		t.Errorf("these packages run git in tests but their TestMain does not call testutil.DisableGitAutoMaintenance(): %v", missing)
	}
}
