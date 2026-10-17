package gitops

// #3114: every path tenant-api hands git is a name, never a pattern
// (GIT_LITERAL_PATHSPECS=1 on commands that run no hook, `:(literal)` on a
// file checkout — see literalPathspecs). conf.d here is `conf[1]`; a
// tracked `conf1` beside it is what the glob `conf[1]` reaches. Each case
// checks the fixture first (raw git, which globs, does reach the decoy), then
// that the writer's git command touched or read only the target. Seams: the
// git binary (w.gitBinary) for commit-tree's environment; otherwise none
// (internal methods on a real temp repository).

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

const (
	litTarget = "conf[1]/t.yaml"
	litDecoy  = "conf1/t.yaml"
)

// pathspecEnvVars are git's global pathspec settings. A fixture check runs
// raw git, which reads them from the process environment: an inherited
// GIT_NOGLOB_PATHSPECS / GIT_LITERAL_PATHSPECS would stop the glob reaching
// the decoy (the check fails as a broken fixture), and GIT_GLOB_PATHSPECS /
// GIT_ICASE_PATHSPECS beside a literal one make git refuse to run.
var pathspecEnvVars = []string{
	"GIT_LITERAL_PATHSPECS", "GIT_NOGLOB_PATHSPECS", "GIT_GLOB_PATHSPECS", "GIT_ICASE_PATHSPECS",
}

// clearPathspecEnv unsets pathspecEnvVars for the test (t.Setenv restores
// them afterwards), so raw git in a fixture check globs as git does by default.
func clearPathspecEnv(t *testing.T) {
	t.Helper()
	for _, k := range pathspecEnvVars {
		t.Setenv(k, "")
		if err := os.Unsetenv(k); err != nil {
			t.Fatal(err)
		}
	}
}

// literalRepo seeds conf[1]/t.yaml and conf1/t.yaml on main, with the
// pathspec settings cleared (clearPathspecEnv).
func literalRepo(t *testing.T) (repo string, w *Writer) {
	t.Helper()
	clearPathspecEnv(t)
	repo = seedTreeRepo(t, map[string]string{
		litTarget: "target: 1\n",
		litDecoy:  "decoy: 1\n",
	})
	if got := gitOut(t, repo, "ls-files", "--", litTarget); !strings.Contains(got, litDecoy) {
		t.Fatalf("fixture: the glob does not reach the decoy (ls-files = %q)", got)
	}
	return repo, NewWriter(filepath.Join(repo, "conf[1]"), repo)
}

func writeRel(t *testing.T, repo, rel, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(repo, filepath.FromSlash(rel)), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func readRel(t *testing.T, repo, rel string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(repo, filepath.FromSlash(rel)))
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

func stagedSet(t *testing.T, repo string) string {
	t.Helper()
	return gitOut(t, repo, "diff", "--cached", "--name-only")
}

func TestGitEnv_LiteralPathspecs(t *testing.T) {
	repo, _ := literalRepo(t) // before the Setenvs: its fixture check is raw git
	t.Setenv("GIT_GLOB_PATHSPECS", "1")
	t.Setenv("GIT_ICASE_PATHSPECS", "1")
	t.Setenv("GIT_NOGLOB_PATHSPECS", "1")
	t.Setenv("GIT_LITERAL_PATHSPECS", "0")
	cmd, _, cancel := NewWriter(repo, repo).gitCmd("-C", repo, "ls-files", "--", litTarget)
	defer cancel()
	if got := pathspecVars(cmd.Env); got != "GIT_LITERAL_PATHSPECS=1" {
		t.Fatalf("pathspec settings in git's env = %q, want only GIT_LITERAL_PATHSPECS=1", got)
	}
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git with that env: %v\n%s", err, out)
	}
	if got := strings.TrimSpace(string(out)); got != litTarget {
		t.Errorf("ls-files = %q, want only %q", got, litTarget)
	}
}

// pathspecVars is every pathspec setting in env, in order, joined by spaces.
func pathspecVars(env []string) string {
	var got []string
	for _, kv := range env {
		if isPathspecEnv(kv) {
			got = append(got, kv)
		}
	}
	return strings.Join(got, " ")
}

// literalPathspecs: the per-command allowlist. A command that runs hooks
// gets no pathspec setting, whatever it inherited (its hooks would read
// theirs literally); see TestGitHooks_SeeNoPathspecSetting for the hooks.
func TestLiteralPathspecs_OnlyCommandsThatRunNoHook(t *testing.T) {
	t.Setenv("GIT_LITERAL_PATHSPECS", "1")
	t.Setenv("GIT_NOGLOB_PATHSPECS", "1")
	w := NewWriter(t.TempDir(), t.TempDir())
	for _, c := range []struct {
		args    []string
		literal bool
	}{
		{[]string{"-C", "d", "add", "--", "p"}, true},
		{[]string{"--no-optional-locks", "-C", "d", "ls-files", "-z", "--", "p"}, true},
		{[]string{"-C", "d", "log", "-p", "--format=", "--", "p"}, true},
		{[]string{"--no-optional-locks", "-C", "d", "ls-tree", "HEAD", "--", "p"}, true},
		{[]string{"-C", "d", "diff", "--cached", "--quiet"}, true},
		{[]string{"-C", "d", "reset", "-q", "--", "p"}, true},
		{[]string{"-C", "d", "reset", "-q"}, false},
		{[]string{"-C", "d", "reset", "--hard", "HEAD"}, false},
		{[]string{"-C", "d", "checkout", "-q", "HEAD", "--", "p"}, false},
		{[]string{"-C", "d", "-c", "user.name=add", "commit", "-m", "m"}, false},
		{[]string{"-C", "d", "push", "origin", "b"}, false},
		{[]string{"-C", "d", "fetch", "--prune", "origin", "main"}, false},
		{[]string{"-C", "d", "update-ref", "-m", "m", "HEAD", "a", "b"}, false},
		{[]string{"-C", "d", "worktree", "add", "--no-checkout", "--detach", "r", "HEAD"}, false},
		{[]string{"-C", "d", "commit-tree", "t", "-p", "b", "-m", "m"}, false},
		{[]string{"-C", "d", "branch", "-D", "b"}, false},
	} {
		cmd, _, cancel := w.gitCmd(c.args...)
		cancel()
		want := ""
		if c.literal {
			want = "GIT_LITERAL_PATHSPECS=1"
		}
		if got := pathspecVars(cmd.Env); got != want {
			t.Errorf("git %v: pathspec settings = %q, want %q", c.args, got, want)
		}
	}
}

// commit_cas.go gitCommitOnto: commit-tree gets the commit's identity in
// its own environment, and that environment must still be gitEnv's — built
// from os.Environ() instead, it would carry the pathspec settings tenant-api
// inherited. commit-tree takes no pathspec and is not on literalPathspecs'
// list, so it gets none; the setting is read off the environment git
// actually ran with: a stub git records it, then runs the real one.
func TestGitCommitOnto_CommitTreeKeepsGitEnv(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("unix shell stub; Go tests for this package run in the dev container / Linux CI")
	}
	repo, w := literalRepo(t)
	t.Setenv("GIT_LITERAL_PATHSPECS", "1") // inherited: gitEnv must drop it
	record := filepath.Join(t.TempDir(), "commit-tree.env")
	stub := filepath.Join(t.TempDir(), "git-records-commit-tree.sh")
	script := "#!/bin/sh\n" +
		"for a in \"$@\"; do\n" +
		"  if [ \"$a\" = \"commit-tree\" ]; then\n" +
		"    printf '%s\\n' \"GIT_LITERAL_PATHSPECS=${GIT_LITERAL_PATHSPECS-<unset>}\" " +
		"\"GIT_AUTHOR_NAME=$GIT_AUTHOR_NAME\" \"GIT_AUTHOR_EMAIL=$GIT_AUTHOR_EMAIL\" " +
		"\"GIT_COMMITTER_NAME=$GIT_COMMITTER_NAME\" \"GIT_COMMITTER_EMAIL=$GIT_COMMITTER_EMAIL\" > '" + record + "'\n" +
		"  fi\n" +
		"done\n" +
		"exec git \"$@\"\n"
	if err := os.WriteFile(stub, []byte(script), 0o755); err != nil {
		t.Fatalf("write stub: %v", err)
	}
	w.gitBinary = stub
	w.committerName, w.committerEmail = "tenant-api", "bot@example.com"

	base := strings.TrimSpace(gitOut(t, repo, "rev-parse", "HEAD"))
	body := []byte("target: 2\n")
	writeRel(t, repo, litTarget, string(body))
	target := filepath.Join(repo, litTarget)
	committed, err := w.gitCommitOnto(target, "t", "alice@example.com", body, base)
	if err != nil || !committed {
		t.Fatalf("gitCommitOnto = (%v, %v), want a commit", committed, err)
	}
	got, err := os.ReadFile(record)
	if err != nil {
		t.Fatalf("the stub saw no commit-tree: %v", err)
	}
	want := "GIT_LITERAL_PATHSPECS=<unset>\n" +
		"GIT_AUTHOR_NAME=alice\nGIT_AUTHOR_EMAIL=alice@example.com\n" +
		"GIT_COMMITTER_NAME=tenant-api\nGIT_COMMITTER_EMAIL=bot@example.com\n"
	if string(got) != want {
		t.Errorf("commit-tree's environment:\n%s\nwant:\n%s", got, want)
	}
}

// commit_cas.go stagePaths: `git add -- <paths>`.
func TestLiteralPathspec_AddStagesOnlyTarget(t *testing.T) {
	repo, w := literalRepo(t)
	writeRel(t, repo, litTarget, "target: 2\n")
	writeRel(t, repo, litDecoy, "decoy: 2\n")
	if _, err := w.stagePaths(repo, []string{filepath.Join(repo, litTarget)}, nil); err != nil {
		t.Fatalf("stagePaths: %v", err)
	}
	if got := stagedSet(t, repo); got != litTarget {
		t.Errorf("staged = %q, want only %q", got, litTarget)
	}
}

// commit_cas.go stagePaths, on a refusal: `git reset -q -- <paths>`.
func TestLiteralPathspec_RefusalUnstagesOnlyTarget(t *testing.T) {
	repo, w := literalRepo(t)
	writeRel(t, repo, litDecoy, "decoy: 2\n")
	gitRun(t, repo, "add", "--", litDecoy) // someone else's staged change
	writeRel(t, repo, litTarget, "target: 2\n")
	target := filepath.Join(repo, litTarget)
	_, err := w.stagePaths(repo, []string{target}, map[string][]byte{target: []byte("not what is on disk\n")})
	if err == nil || !w.stagedRefusal(err) {
		t.Fatalf("stagePaths err = %v, want the staged-bytes refusal", err)
	}
	if got := stagedSet(t, repo); got != litDecoy {
		t.Errorf("staged after the refusal = %q, want only %q (the decoy's own staging kept)", got, litDecoy)
	}
}

// write_validator.go restoreFromHEAD: `reset -q -- <file>` and
// `checkout -q HEAD -- <file>`.
func TestLiteralPathspec_RestoreFromHEADTouchesOnlyTarget(t *testing.T) {
	repo, w := literalRepo(t)
	writeRel(t, repo, litDecoy, "decoy: staged\n")
	gitRun(t, repo, "add", "--", litDecoy)
	writeRel(t, repo, litDecoy, "decoy: on disk\n")
	writeRel(t, repo, litTarget, "target: 2\n")
	gitRun(t, repo, "add", "--", litTarget)
	if err := w.restoreFromHEAD(filepath.Join(repo, litTarget)); err != nil {
		t.Fatalf("restoreFromHEAD: %v", err)
	}
	if got := readRel(t, repo, litTarget); got != "target: 1\n" {
		t.Errorf("target = %q, want HEAD's", got)
	}
	if got := stagedSet(t, repo); got != litDecoy {
		t.Errorf("staged = %q, want only %q (reset reached the decoy)", got, litDecoy)
	}
	if got := readRel(t, repo, litDecoy); got != "decoy: on disk\n" {
		t.Errorf("decoy on disk = %q, want its own edit (checkout reached the decoy)", got)
	}
}

// write_validator.go restoreFromHEAD: `ls-tree HEAD -- <file>` — a target
// HEAD does not have is removed, even when the glob names a file HEAD has.
// ls-tree does not glob even without GIT_LITERAL_PATHSPECS (git 2.43
// measured), so this one stays green without the setting; it pins the
// behavior, not the setting.
func TestLiteralPathspec_RestoreFromHEADRemovesTargetHEADLacks(t *testing.T) {
	repo, w := literalRepo(t)
	const newTarget, newDecoy = "conf[1]/n.yaml", "conf1/n.yaml"
	writeRel(t, repo, newDecoy, "decoy: 1\n")
	gitRun(t, repo, "add", "--", newDecoy)
	gitRun(t, repo, "commit", "-m", "decoy n")
	writeRel(t, repo, newTarget, "target: new\n")
	if err := w.restoreFromHEAD(filepath.Join(repo, newTarget)); err != nil {
		t.Fatalf("restoreFromHEAD: %v", err)
	}
	if _, err := os.Lstat(filepath.Join(repo, filepath.FromSlash(newTarget))); !os.IsNotExist(err) {
		t.Errorf("target HEAD does not have is still there (lstat err = %v)", err)
	}
	if got := readRel(t, repo, newDecoy); got != "decoy: 1\n" {
		t.Errorf("decoy = %q, want untouched", got)
	}
}

// writer_batch.go syncServingPaths: `ls-tree <commit> -- <rel>` and
// `reset -q -- <serving paths>`.
func TestLiteralPathspec_SyncServingPathsTouchesOnlyTarget(t *testing.T) {
	repo, w := literalRepo(t)
	writeRel(t, repo, litDecoy, "decoy: staged\n")
	gitRun(t, repo, "add", "--", litDecoy)
	writeRel(t, repo, litTarget, "target: 2\n")
	gitRun(t, repo, "add", "--", litTarget)
	if err := w.syncServingPaths("HEAD", []string{litTarget}); err != nil {
		t.Fatalf("syncServingPaths: %v", err)
	}
	if got := readRel(t, repo, litTarget); got != "target: 1\n" {
		t.Errorf("target = %q, want HEAD's", got)
	}
	if got := stagedSet(t, repo); got != litDecoy {
		t.Errorf("staged = %q, want only %q (reset reached the decoy)", got, litDecoy)
	}
}

// writer_batch.go openStaging (PR mode, no mirror): `ls-files -- <conf.d>`
// and `checkout -- <conf.d>`. The glob `conf[1]` reaches a tracked FILE
// named conf1 (a directory conf1/ is not matched by it), so that is the
// decoy here; only conf.d may appear in the staging worktree.
func TestLiteralPathspec_StagingChecksOutOnlyConfD(t *testing.T) {
	clearPathspecEnv(t)
	repo := seedTreeRepo(t, map[string]string{
		litTarget: "target: 1\n",
		"conf1":   "decoy\n",
	})
	if got := gitOut(t, repo, "ls-files", "--", "conf[1]"); !strings.Contains(got, "conf1") {
		t.Fatalf("fixture: the glob does not reach the decoy (ls-files = %q)", got)
	}
	w := NewWriter(filepath.Join(repo, "conf[1]"), repo)
	st, err := w.openStaging("HEAD", "", false)
	if err != nil {
		t.Fatalf("openStaging: %v", err)
	}
	defer w.closeStaging(st)
	if _, err := os.Stat(filepath.Join(st.root, filepath.FromSlash(litTarget))); err != nil {
		t.Errorf("conf.d's file is not checked out in the staging worktree: %v", err)
	}
	if _, err := os.Lstat(filepath.Join(st.root, "conf1")); !os.IsNotExist(err) {
		t.Errorf("the decoy outside conf.d was checked out in the staging worktree (lstat err = %v)", err)
	}
}

// writer_registry.go HistoricalMaxUint: `log -p -- <file>`.
func TestLiteralPathspec_HistoricalMaxReadsOnlyTarget(t *testing.T) {
	clearPathspecEnv(t)
	repo := seedTreeRepo(t, map[string]string{
		"_reg[1].yaml": "next_id: 5\n",
		"_reg1.yaml":   "next_id: 99\n",
	})
	if got := gitOut(t, repo, "ls-files", "--", "_reg[1].yaml"); !strings.Contains(got, "_reg1.yaml") {
		t.Fatalf("fixture: the glob does not reach the decoy (ls-files = %q)", got)
	}
	got, err := NewWriter(repo, repo).HistoricalMaxUint(context.Background(), "_reg[1].yaml", "next_id")
	if err != nil {
		t.Fatalf("HistoricalMaxUint: %v", err)
	}
	if got != 5 {
		t.Errorf("HistoricalMaxUint = %d, want 5 (the decoy's 99 was read)", got)
	}
}

// writer_pr.go refuseUntrackedNonRegular: `ls-files -- <conf.d>/<file>`,
// run from the top level with gitDir below it (the `:(top)` magic #3113
// used is off under GIT_LITERAL_PATHSPECS). A directory is the non-regular
// entry, so this runs on every platform; the FIFO shape is in
// writer_tree_unreadable_linux_test.go.
func TestLiteralPathspec_TrackedCheck(t *testing.T) {
	repo, _ := literalRepo(t)
	confDir := filepath.Join(repo, "conf[1]")
	w := NewWriter(confDir, confDir) // gitDir below the top level

	// Untracked, non-regular: refused, though the glob names the tracked decoy.
	untracked := filepath.Join(confDir, "u.yaml")
	writeRel(t, repo, "conf1/u.yaml", "decoy\n")
	gitRun(t, repo, "add", "--", "conf1/u.yaml")
	gitRun(t, repo, "commit", "-m", "decoy u")
	if err := os.Mkdir(untracked, 0o755); err != nil {
		t.Fatal(err)
	}
	var te *TargetUnreadableError
	if err := w.refuseUntrackedNonRegular(untracked, "u"); err == nil {
		t.Errorf("untracked directory at the target passed as tracked (the glob reached %s)", "conf1/u.yaml")
	} else if !errors.As(err, &te) {
		t.Errorf("err = %v, want *TargetUnreadableError", err)
	}

	// Tracked: allowed — the check still finds the target itself.
	tracked := filepath.Join(repo, filepath.FromSlash(litTarget))
	if err := os.Remove(tracked); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(tracked, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := w.refuseUntrackedNonRegular(tracked, "t"); err != nil {
		t.Errorf("tracked target refused: %v", err)
	}
}
