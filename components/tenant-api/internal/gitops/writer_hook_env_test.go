package gitops

// #3114: git hands its environment to the hooks it runs, so a pathspec
// setting tenant-api gives a hook-running command would reach the conf
// repo's own hooks — a pre-commit linting `git diff --cached -- '*.yaml'`
// would match nothing and pass. Hooks here record the pathspec settings
// they see; the writer's flows that run them must leave all four unset,
// even when tenant-api itself inherited some. Seams: none (real temp
// repositories with hooks installed, a bare local origin for push).

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

const hookUnset = "LIT=<unset> GLOB=<unset> ICASE=<unset> NOGLOB=<unset>"

// installEnvHooks makes each named hook append "<hook> <settings>" to the
// returned log. pre-commit also appends what `git diff --cached
// --name-only -- '*.yaml'` lists to the returned yaml log. Hooks read stdin
// to its end (reference-transaction, pre-push get one).
func installEnvHooks(t *testing.T, repo string, hooks ...string) (envLog, yamlLog string) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("sh hooks; Go tests for this package run in the dev container / Linux CI")
	}
	logs := t.TempDir()
	envLog, yamlLog = filepath.Join(logs, "env.log"), filepath.Join(logs, "yaml.log")
	for _, h := range hooks {
		script := "#!/bin/sh\n" +
			"cat >/dev/null 2>&1 || true\n" +
			"echo \"" + h + " LIT=${GIT_LITERAL_PATHSPECS-<unset>} GLOB=${GIT_GLOB_PATHSPECS-<unset>} " +
			"ICASE=${GIT_ICASE_PATHSPECS-<unset>} NOGLOB=${GIT_NOGLOB_PATHSPECS-<unset>}\" >> '" + envLog + "'\n"
		if h == "pre-commit" {
			script += "git diff --cached --name-only -- '*.yaml' >> '" + yamlLog + "'\n"
		}
		if err := os.WriteFile(filepath.Join(repo, ".git", "hooks", h), []byte(script), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return envLog, yamlLog
}

// assertHooksSawNothing: every hook in want ran, and every run saw all four
// pathspec settings unset.
func assertHooksSawNothing(t *testing.T, envLog string, want ...string) {
	t.Helper()
	b, err := os.ReadFile(envLog)
	if err != nil {
		t.Fatalf("no hook ran: %v", err)
	}
	ran, reported := map[string]bool{}, map[string]bool{}
	for _, line := range strings.Split(strings.TrimSpace(string(b)), "\n") {
		name, settings, _ := strings.Cut(line, " ")
		ran[name] = true
		if settings != hookUnset && !reported[name+settings] {
			reported[name+settings] = true
			t.Errorf("hook %s saw %s, want %s", name, settings, hookUnset)
		}
	}
	for _, h := range want {
		if !ran[h] {
			t.Errorf("hook %s did not run (ran: %v) — the flow no longer reaches it", h, ran)
		}
	}
}

// inheritPathspecEnv gives the test process (and so tenant-api) pathspec
// settings an operator never configured for the conf repo. Glob/icase
// cannot sit beside a literal setting, so noglob and literal are used.
func inheritPathspecEnv(t *testing.T) {
	t.Setenv("GIT_NOGLOB_PATHSPECS", "1")
	t.Setenv("GIT_LITERAL_PATHSPECS", "1")
}

// Direct batch: the commit in the staging worktree (gitCommitPaths) runs
// pre-commit / commit-msg / post-commit, the CAS update-ref onto the
// serving branch runs reference-transaction.
func TestGitHooks_SeeNoPathspecSetting_DirectBatch(t *testing.T) {
	clearPathspecEnv(t)
	dir := seedTreeRepo(t, map[string]string{"h-a.yaml": silentBody("h-a", "warning")})
	envLog, yamlLog := installEnvHooks(t, dir,
		"pre-commit", "commit-msg", "post-commit", "reference-transaction")
	inheritPathspecEnv(t)
	w := NewWriter(dir, dir)
	if _, err := w.WriteBatch(context.Background(), []BatchOp{
		setOp("h-a", silentBody("h-a", "critical")),
		setOp("h-new", silentBody("h-new", "warning")),
	}, "op@example.com"); err != nil {
		t.Fatalf("WriteBatch: %v", err)
	}
	assertHooksSawNothing(t, envLog, "pre-commit", "commit-msg", "post-commit", "reference-transaction")
	b, err := os.ReadFile(yamlLog)
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.TrimSpace(string(b)); got != "h-a.yaml\nh-new.yaml" {
		t.Errorf("pre-commit's `git diff --cached -- '*.yaml'` = %q, want both staged files", got)
	}
}

// PR batch: the staging worktree's file checkout of conf.d runs
// post-checkout, the branch's commit the commit hooks, the push pre-push,
// the base fetch reference-transaction.
func TestGitHooks_SeeNoPathspecSetting_PRBatch(t *testing.T) {
	clearPathspecEnv(t)
	dir := seedTreeRepo(t, map[string]string{"conf/p-a.yaml": silentBody("p-a", "warning")})
	gitRun(t, dir, "push", "-q", "origin", "main")
	envLog, yamlLog := installEnvHooks(t, dir,
		"pre-commit", "post-checkout", "pre-push", "reference-transaction")
	inheritPathspecEnv(t)
	w := NewWriter(filepath.Join(dir, "conf"), dir)
	op := PRBatchOp{TenantID: "p-a", Merge: func([]byte) (string, error) { return silentBody("p-a", "critical"), nil }}
	if _, err := w.WritePRBatch(context.Background(), []PRBatchOp{op}, "op@example.com"); err != nil {
		t.Fatalf("WritePRBatch: %v", err)
	}
	assertHooksSawNothing(t, envLog, "pre-commit", "post-checkout", "pre-push", "reference-transaction")
	b, err := os.ReadFile(yamlLog)
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.TrimSpace(string(b)); got != "conf/p-a.yaml" {
		t.Errorf("pre-commit's `git diff --cached -- '*.yaml'` = %q, want conf/p-a.yaml", got)
	}
}

// restoreFromHEAD's file checkout runs post-checkout; gitCommitOnto's CAS
// update-ref runs reference-transaction.
func TestGitHooks_SeeNoPathspecSetting_RestoreAndCAS(t *testing.T) {
	repo, w := literalRepo(t)
	envLog, _ := installEnvHooks(t, repo, "post-checkout", "reference-transaction")
	inheritPathspecEnv(t)

	target := filepath.Join(repo, litTarget)
	writeRel(t, repo, litTarget, "target: 2\n")
	if err := w.restoreFromHEAD(target); err != nil {
		t.Fatalf("restoreFromHEAD: %v", err)
	}
	assertHooksSawNothing(t, envLog, "post-checkout")

	base := strings.TrimSpace(gitOut(t, repo, "rev-parse", "HEAD"))
	body := []byte("target: 3\n")
	writeRel(t, repo, litTarget, string(body))
	if committed, err := w.gitCommitOnto(target, "t", "alice@example.com", body, base); err != nil || !committed {
		t.Fatalf("gitCommitOnto = (%v, %v), want a commit", committed, err)
	}
	assertHooksSawNothing(t, envLog, "post-checkout", "reference-transaction")
}
