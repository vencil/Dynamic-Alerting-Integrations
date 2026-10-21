package gitops

// #3175: tenant-api refuses to run against a git config that names a
// filter, diff or merge driver program (CheckGitDrivers), at startup and
// again before every write and preview, and turns core.fsmonitor off on
// every git command (gitFixedConfig). Seams: real temp repositories; the
// config git reads beyond the repository's own comes through the
// per-Writer gitTestEnv (GIT_CONFIG_GLOBAL, GIT_CONFIG_COUNT), never the
// process environment, so every test here runs in parallel.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

// gitDriverKeys are the keys the check refuses, one per kind.
var gitDriverKeys = []string{
	"filter.x.clean", "filter.x.smudge", "filter.x.process",
	"diff.x.textconv", "diff.x.command", "merge.x.driver",
}

// secretValue is in every value the tests set: it must never reach an
// error text.
const secretValue = "SECRET-VALUE-do-not-print"

// configFileFor renders key = value as config file text.
func configFileFor(key, value string) string {
	first, last := strings.Index(key, "."), strings.LastIndex(key, ".")
	return "[" + key[:first] + " \"" + key[first+1:last] + "\"]\n\t" + key[last+1:] + " = " + value + "\n"
}

func skipOnWindows(t *testing.T) {
	t.Helper()
	if runtime.GOOS == "windows" {
		t.Skip("sh drivers; Go tests for this package run in the dev container / Linux CI")
	}
}

func TestCheckGitDrivers_RefusesEveryKeyFromEveryScope(t *testing.T) {
	t.Parallel()
	type source struct {
		name, scope string
		set         func(t *testing.T, repo string, w *Writer, key, value string)
	}
	writeConf := func(t *testing.T, key, value string) string {
		p := filepath.Join(t.TempDir(), "driver.gitconfig")
		if err := os.WriteFile(p, []byte(configFileFor(key, value)), 0o644); err != nil {
			t.Fatal(err)
		}
		return p
	}
	sources := []source{
		{"repo config", "local", func(t *testing.T, repo string, _ *Writer, key, value string) {
			gitRun(t, repo, "config", key, value)
		}},
		{"global config", "global", func(t *testing.T, _ string, w *Writer, key, value string) {
			w.gitTestEnv = []string{"GIT_CONFIG_GLOBAL=" + writeConf(t, key, value)}
		}},
		{"include.path", "local", func(t *testing.T, repo string, _ *Writer, key, value string) {
			gitRun(t, repo, "config", "include.path", writeConf(t, key, value))
		}},
		{"includeIf gitdir", "local", func(t *testing.T, repo string, _ *Writer, key, value string) {
			gitRun(t, repo, "config", "includeIf.gitdir:"+repo+"/.path", writeConf(t, key, value))
		}},
		{"GIT_CONFIG_COUNT", "command", func(_ *testing.T, _ string, w *Writer, key, value string) {
			w.gitTestEnv = []string{"GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=" + key, "GIT_CONFIG_VALUE_0=" + value}
		}},
	}
	for _, src := range sources {
		for _, key := range gitDriverKeys {
			t.Run(src.name+"/"+key, func(t *testing.T) {
				t.Parallel()
				repo := initRepoOnMain(t)
				w := newTestWriter(repo, repo)
				if err := w.CheckGitDrivers(); err != nil {
					t.Fatalf("baseline: a clean repository is refused: %v", err)
				}
				src.set(t, repo, w, key, "/opt/"+secretValue+" --token")
				err := w.CheckGitDrivers()
				if !errors.Is(err, ErrGitDriverConfigured) {
					t.Fatalf("CheckGitDrivers() = %v, want ErrGitDriverConfigured", err)
				}
				var de *GitDriverError
				if !errors.As(err, &de) {
					t.Fatalf("CheckGitDrivers() = %v, want a *GitDriverError", err)
				}
				if want := src.scope + " " + key; !strings.Contains(err.Error(), want) {
					t.Errorf("error does not name %q: %v", want, err)
				}
				if strings.Contains(err.Error(), secretValue) {
					t.Errorf("error prints the value: %v", err)
				}
			})
		}
	}
}

// Keys of the same sections that name no program, and core.fsmonitor
// (turned off per command instead), are not refused.
func TestCheckGitDrivers_PassesKeysThatRunNothing(t *testing.T) {
	t.Parallel()
	repo := initRepoOnMain(t)
	for _, kv := range [][2]string{
		{"filter.x.required", "true"}, {"diff.x.binary", "true"}, {"diff.x.cachetextconv", "true"},
		{"merge.x.name", "a merge driver"}, {"core.fsmonitor", "/bin/false"},
		{"diff.renames", "true"}, {"merge.conflictstyle", "diff3"},
	} {
		gitRun(t, repo, "config", kv[0], kv[1])
	}
	w := newTestWriter(repo, repo)
	w.gitTestEnv = []string{"GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=filter.y.required", "GIT_CONFIG_VALUE_0=false"}
	if err := w.CheckGitDrivers(); err != nil {
		t.Fatalf("CheckGitDrivers() = %v, want nil", err)
	}
}

// A config git cannot read is refused too: it cannot be shown to be clean.
func TestCheckGitDrivers_RefusesUnreadableConfig(t *testing.T) {
	t.Parallel()
	repo := initRepoOnMain(t)
	f, err := os.OpenFile(filepath.Join(repo, ".git", "config"), os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	_, _ = f.WriteString("[[[ not a config line\n")
	_ = f.Close()
	err = newTestWriter(repo, repo).CheckGitDrivers()
	if !errors.Is(err, ErrGitConfigUnreadable) {
		t.Fatalf("CheckGitDrivers() = %v, want ErrGitConfigUnreadable", err)
	}
	// #3226: not the driver refusal — nothing says a driver is configured.
	var de *GitDriverError
	if errors.Is(err, ErrGitDriverConfigured) || errors.As(err, &de) {
		t.Errorf("CheckGitDrivers() = %v, which says a driver is configured", err)
	}
}

// driverRepo is a repository whose .gitattributes picks driver x for every
// YAML file (committed, with tenant t1's file), and mark is a directory
// the driver programs touch a file in when they run.
func driverRepo(t *testing.T) (repo, mark string) {
	t.Helper()
	repo = seedTreeRepo(t, map[string]string{
		".gitattributes": "*.yaml filter=x diff=x merge=x\n",
		"t1.yaml":        tenantBody("t1"),
	})
	return repo, t.TempDir()
}

// setDriverPrograms points every driver key in the repo's config at a
// program that records, in mark, that it ran (and passes content through).
func setDriverPrograms(t *testing.T, repo, mark string) {
	t.Helper()
	for _, key := range gitDriverKeys {
		gitRun(t, repo, "config", key, "touch '"+filepath.Join(mark, key)+"'; cat")
	}
}

func ranPrograms(t *testing.T, mark string) []string {
	t.Helper()
	ents, err := os.ReadDir(mark)
	if err != nil {
		t.Fatal(err)
	}
	var ran []string
	for _, e := range ents {
		ran = append(ran, e.Name())
	}
	return ran
}

// staleStat makes git re-read path's content on the next status (its
// index stat no longer matches), so a clean filter runs on it.
func staleStat(t *testing.T, path string) {
	t.Helper()
	future := time.Now().Add(time.Hour)
	if err := os.Chtimes(path, future, future); err != nil {
		t.Fatal(err)
	}
}

// The check runs nothing: with every driver set to a program that records
// its run, and core.fsmonitor set to one too, CheckGitDrivers leaves no
// record. The control shows the fixture would run them: a plain `git
// status` does.
func TestCheckGitDrivers_CheckRunsNoDriver(t *testing.T) {
	t.Parallel()
	skipOnWindows(t)
	repo, mark := driverRepo(t)
	setDriverPrograms(t, repo, mark)
	fsmonitor := filepath.Join(t.TempDir(), "fsmonitor.sh")
	if err := os.WriteFile(fsmonitor, []byte("#!/bin/sh\ntouch '"+filepath.Join(mark, "core.fsmonitor")+"'\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	gitRun(t, repo, "config", "core.fsmonitor", fsmonitor)

	staleStat(t, filepath.Join(repo, "t1.yaml"))
	gitOut(t, repo, "status", "--porcelain")
	// (filter.x.process, when set, replaces clean: git runs either.)
	if ran := strings.Join(ranPrograms(t, mark), " "); !strings.Contains(ran, "filter.x.") || !strings.Contains(ran, "core.fsmonitor") {
		t.Fatalf("control: a plain git status ran %q; the fixture does not reach the filter and fsmonitor", ran)
	}
	if err := os.RemoveAll(mark); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(mark, 0o755); err != nil {
		t.Fatal(err)
	}

	staleStat(t, filepath.Join(repo, "t1.yaml"))
	if err := newTestWriter(repo, repo).CheckGitDrivers(); !errors.Is(err, ErrGitDriverConfigured) {
		t.Fatalf("CheckGitDrivers() = %v, want ErrGitDriverConfigured", err)
	}
	if ran := ranPrograms(t, mark); len(ran) > 0 {
		t.Errorf("CheckGitDrivers ran %v", ran)
	}
}

// A driver added after startup (an init container or a sidecar editing
// .git/config) refuses the next write before git touches a file: nothing
// is written, nothing committed, and the driver never runs. The same for
// the preview (which runs `git status` for a read-only caller) and the
// PR-mode tree check.
func TestWrites_RefusedWhenDriverAddedAfterStartup(t *testing.T) {
	t.Parallel()
	skipOnWindows(t)
	ops := map[string]func(t *testing.T, w *Writer) error{
		"Write":        func(t *testing.T, w *Writer) error { return writeVia(t, "Write", w, "t1") },
		"WriteMerged":  func(t *testing.T, w *Writer) error { return writeVia(t, "WriteMerged", w, "t1") },
		"WritePR":      func(t *testing.T, w *Writer) error { return writeVia(t, "WritePR", w, "t1") },
		"WritePRBatch": func(t *testing.T, w *Writer) error { return writeVia(t, "WritePRBatch", w, "t1") },
		"WriteBatch": func(t *testing.T, w *Writer) error {
			_, err := w.WriteBatch(context.Background(), []BatchOp{setOp("t1", silentBody("t1", "critical"))}, "op@example.com")
			return err
		},
		"PreviewPut": func(t *testing.T, w *Writer) error {
			_, _, err := w.PreviewPut(context.Background(), "t1", silentBody("t1", "critical"))
			return err
		},
		"TreeOnBase": func(t *testing.T, w *Writer) error { return w.TreeOnBase() },
	}
	for name, op := range ops {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			repo, mark := driverRepo(t)
			w := newTestWriter(repo, repo)
			if err := w.CheckGitDrivers(); err != nil {
				t.Fatalf("startup check refused a clean repository: %v", err)
			}
			head := gitOut(t, repo, "rev-parse", "HEAD")
			before, err := os.ReadFile(filepath.Join(repo, "t1.yaml"))
			if err != nil {
				t.Fatal(err)
			}

			setDriverPrograms(t, repo, mark)
			staleStat(t, filepath.Join(repo, "t1.yaml"))

			err = op(t, w)
			if !errors.Is(err, ErrGitDriverConfigured) {
				t.Fatalf("%s = %v, want ErrGitDriverConfigured", name, err)
			}
			if ran := ranPrograms(t, mark); len(ran) > 0 {
				t.Errorf("%s ran the driver programs %v", name, ran)
			}
			if got := gitOut(t, repo, "rev-parse", "HEAD"); got != head {
				t.Errorf("HEAD moved from %s to %s", head, got)
			}
			if got, _ := os.ReadFile(filepath.Join(repo, "t1.yaml")); string(got) != string(before) {
				t.Errorf("t1.yaml changed:\n%s", got)
			}
			if got := gitOut(t, repo, "branch", "--list"); strings.Count(got, "\n") > 0 {
				t.Errorf("a branch was created:\n%s", got)
			}
		})
	}
}

// core.fsmonitor set in the conf repo's config would run on every git
// command that looks at the worktree; tenant-api's run with it off. The
// control shows a plain `git status` does run it.
func TestGitCommands_RunNoFSMonitor(t *testing.T) {
	t.Parallel()
	skipOnWindows(t)
	repo := seedTreeRepo(t, map[string]string{"t1.yaml": tenantBody("t1")})
	mark := filepath.Join(t.TempDir(), "fsmonitor-ran")
	fsmonitor := filepath.Join(t.TempDir(), "fsmonitor.sh")
	if err := os.WriteFile(fsmonitor, []byte("#!/bin/sh\ntouch '"+mark+"'\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	gitRun(t, repo, "config", "core.fsmonitor", fsmonitor)
	gitOut(t, repo, "status", "--porcelain")
	if _, err := os.Stat(mark); err != nil {
		t.Fatalf("control: a plain git status did not run core.fsmonitor: %v", err)
	}
	if err := os.Remove(mark); err != nil {
		t.Fatal(err)
	}

	w := newTestWriter(repo, repo)
	if _, _, err := w.PreviewPut(context.Background(), "t1", silentBody("t1", "critical")); err != nil {
		t.Fatalf("PreviewPut: %v", err)
	}
	if _, err := w.Write(context.Background(), "t1", "op@example.com", silentBody("t1", "critical")); err != nil {
		t.Fatalf("Write: %v", err)
	}
	if _, err := w.WriteBatch(context.Background(), []BatchOp{setOp("t1", silentBody("t1", "warning"))}, "op@example.com"); err != nil {
		t.Fatalf("WriteBatch: %v", err)
	}
	if err := w.TreeOnBase(); err != nil {
		t.Fatalf("TreeOnBase: %v", err)
	}
	if _, err := os.Stat(mark); err == nil {
		t.Errorf("a tenant-api git command ran core.fsmonitor")
	}
}

// diff.external and GIT_EXTERNAL_DIFF name a program from config or the
// environment alone — no .gitattributes needed, and not a driver key the
// check refuses — so the /diff preview's `git diff --no-index` passes
// --no-ext-diff. Each source gets a control: a plain `git diff --no-index`
// in the same environment does run the program.
func TestDiffPreview_RunsNoExternalDiff(t *testing.T) {
	t.Parallel()
	skipOnWindows(t)
	for _, src := range []string{"diff.external", "GIT_EXTERNAL_DIFF"} {
		t.Run(src, func(t *testing.T) {
			t.Parallel()
			repo := seedTreeRepo(t, map[string]string{"t1.yaml": tenantBody("t1")})
			mark := filepath.Join(t.TempDir(), "ext-diff-ran")
			prog := filepath.Join(t.TempDir(), "extdiff.sh")
			if err := os.WriteFile(prog, []byte("#!/bin/sh\ntouch '"+mark+"'\n"), 0o755); err != nil {
				t.Fatal(err)
			}
			env := []string{"GIT_EXTERNAL_DIFF=" + prog}
			if src == "diff.external" {
				env = []string{"GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=diff.external", "GIT_CONFIG_VALUE_0=" + prog}
			}
			w := newTestWriter(repo, repo)
			w.gitTestEnv = env

			// Control: the fixture's program runs for a plain diff.
			ctl := t.TempDir()
			writeIn(t, ctl, "a", "x\n")
			writeIn(t, ctl, "b", "y\n")
			cmd, _, cancel := w.gitCmd("diff", "--no-index", "--", "a", "b")
			cmd.Dir = ctl
			_, _ = cmd.Output()
			cancel()
			if _, err := os.Stat(mark); err != nil {
				t.Fatalf("control: a plain git diff --no-index did not run the %s program: %v", src, err)
			}
			if err := os.Remove(mark); err != nil {
				t.Fatal(err)
			}

			res, errs, err := w.PreviewPut(context.Background(), "t1", silentBody("t1", "critical"))
			if err != nil || len(errs) > 0 {
				t.Fatalf("PreviewPut: %v %v", err, errs)
			}
			diff, err := w.DiffTexts("t1", string(res.Existing), res.Exists, res.Content)
			if err != nil {
				t.Fatalf("DiffTexts: %v", err)
			}
			if !strings.Contains(diff, "\n+") {
				t.Errorf("the preview shows no diff: %q", diff)
			}
			if _, err := os.Stat(mark); err == nil {
				t.Errorf("the /diff preview ran the %s program", src)
			}
		})
	}
}

// A conditional include applies by the context git runs in: `onbranch:` by
// the branch HEAD names, `gitdir:` by the repository directory (a batch's
// staging worktree has its own). Checked on base in the serving tree, such
// a driver is invisible; it applies once a PR write is on its feature
// branch or a batch is in its staging worktree. Each write re-checks there
// before any file is staged or checked out: refused, the driver never runs,
// and the tree is left as it was — on base, no branch, no worktree.
func TestWrites_RefusedWhenConditionalIncludeAppliesLater(t *testing.T) {
	t.Parallel()
	skipOnWindows(t)
	cases := []struct {
		name, include string
		op            func(t *testing.T, w *Writer) error
	}{
		{"WritePR onbranch", "onbranch:tenant-api/**",
			func(t *testing.T, w *Writer) error { return writeVia(t, "WritePR", w, "t1") }},
		{"WritePRBatch onbranch", "onbranch:tenant-api/**",
			func(t *testing.T, w *Writer) error { return writeVia(t, "WritePRBatch", w, "t1") }},
		{"WriteBatch gitdir", "gitdir:**/.git/worktrees/**",
			func(t *testing.T, w *Writer) error {
				_, err := w.WriteBatch(context.Background(), []BatchOp{setOp("t1", silentBody("t1", "critical"))}, "op@example.com")
				return err
			}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			repo, mark := driverRepo(t)
			drv := filepath.Join(t.TempDir(), "drivers.gitconfig")
			var conf strings.Builder
			for _, key := range gitDriverKeys {
				conf.WriteString(configFileFor(key, "\"touch '"+filepath.Join(mark, key)+"'; cat\""))
			}
			if err := os.WriteFile(drv, []byte(conf.String()), 0o644); err != nil {
				t.Fatal(err)
			}
			gitRun(t, repo, "config", "includeIf."+tc.include+".path", drv)

			w := newTestWriter(repo, repo)
			if err := w.CheckGitDrivers(); err != nil {
				t.Fatalf("fixture: the check on base already sees the driver: %v", err)
			}
			head := gitOut(t, repo, "rev-parse", "HEAD")
			before, err := os.ReadFile(filepath.Join(repo, "t1.yaml"))
			if err != nil {
				t.Fatal(err)
			}

			err = tc.op(t, w)
			if !errors.Is(err, ErrGitDriverConfigured) {
				t.Fatalf("%s = %v, want ErrGitDriverConfigured", tc.name, err)
			}
			if ran := ranPrograms(t, mark); len(ran) > 0 {
				t.Errorf("the driver programs ran: %v", ran)
			}
			if got := gitOut(t, repo, "symbolic-ref", "--short", "HEAD"); got != "main" {
				t.Errorf("HEAD is %q, want main", got)
			}
			if got := gitOut(t, repo, "rev-parse", "HEAD"); got != head {
				t.Errorf("HEAD moved from %s to %s", head, got)
			}
			if got := gitOut(t, repo, "branch", "--list"); got != "* main" {
				t.Errorf("branches left:\n%s", got)
			}
			if got := gitOut(t, repo, "worktree", "list", "--porcelain"); strings.Count(got, "worktree ") != 1 {
				t.Errorf("a staging worktree was left:\n%s", got)
			}
			if got, _ := os.ReadFile(filepath.Join(repo, "t1.yaml")); string(got) != string(before) {
				t.Errorf("t1.yaml changed:\n%s", got)
			}
		})
	}
}

// HistoricalMaxUint's `git log -p` reads the blobs' own lines: a textconv
// driver would run (git log -p applies one by default) and rewrite them.
// The driver check refuses the key at startup; --no-textconv is the second
// layer. (`git log -p` runs no external diff without --ext-diff, and `git
// diff --cached --quiet` runs neither — measured with git 2.43 — so their
// --no-ext-diff is defensive and has no test that can fail.)
func TestHistoricalMax_RunsNoTextconv(t *testing.T) {
	t.Parallel()
	skipOnWindows(t)
	repo := seedTreeRepo(t, map[string]string{
		".gitattributes": "*.yaml diff=t\n",
		"_reg.yaml":      "next_id: 5\n",
	})
	writeIn(t, repo, "_reg.yaml", "next_id: 6\n")
	gitRun(t, repo, "commit", "-qam", "bump")
	mark := filepath.Join(t.TempDir(), "textconv-ran")
	w := newTestWriter(repo, repo)
	w.gitTestEnv = []string{"GIT_CONFIG_COUNT=1", "GIT_CONFIG_KEY_0=diff.t.textconv",
		"GIT_CONFIG_VALUE_0=touch '" + mark + "'; cat"}

	got, err := w.HistoricalMaxUint(context.Background(), "_reg.yaml", "next_id")
	if err != nil || got != 6 {
		t.Fatalf("HistoricalMaxUint = %d, %v; want 6", got, err)
	}
	if _, err := os.Stat(mark); err == nil {
		t.Errorf("git log -p ran the textconv driver")
	}
}

// The same conditional include, seen from the other side: the check under
// the lock runs on whatever branch HEAD is on when the lock is taken — an
// operator's checkout, or a PR branch a failed return to base left behind —
// and `onbranch:main` applies only once the write has moved HEAD to base.
// Every move to base re-checks there (checkoutBaseClean) before git runs on
// files under base's config. Without it, WritePR's `checkout -b <branch>
// origin/main` would run the smudge filter under main's config: origin/main
// is ahead of the local main in t1.yaml, so that checkout rewrites it.
//
// Refused, the tree is left on base (where the move put it, with base's
// files: the checkout that put them there ran under the old branch's
// config, which the lock's check passed), no feature branch or worktree is
// left, and the next write is refused under the lock, before any git.
func TestWrites_RefusedWhenHEADStrandedOffBaseAtLock(t *testing.T) {
	t.Parallel()
	skipOnWindows(t)
	cases := []struct {
		name, stranded, mode string
	}{
		{"WritePR from an operator branch", "operator", "WritePR"},
		{"WritePRBatch from an operator branch", "operator", "WritePRBatch"},
		{"Write from a stranded PR branch", "tenant-api/t1/20260101-000000-0000", "Write"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			repo, mark := driverRepo(t)
			// origin/main one commit ahead of the local main, in t1.yaml.
			gitRun(t, repo, "checkout", "-q", "-b", "ahead")
			writeIn(t, repo, "t1.yaml", silentBody("t1", "critical"))
			gitRun(t, repo, "commit", "-qam", "ahead")
			gitRun(t, repo, "push", "-q", "origin", "ahead:main")
			gitRun(t, repo, "checkout", "-q", "main")
			gitRun(t, repo, "branch", "-q", "-D", "ahead")
			gitRun(t, repo, "checkout", "-q", "-b", tc.stranded)

			drv := filepath.Join(t.TempDir(), "drivers.gitconfig")
			var conf strings.Builder
			for _, key := range gitDriverKeys {
				conf.WriteString(configFileFor(key, "\"touch '"+filepath.Join(mark, key)+"'; cat\""))
			}
			if err := os.WriteFile(drv, []byte(conf.String()), 0o644); err != nil {
				t.Fatal(err)
			}
			gitRun(t, repo, "config", "includeIf.onbranch:main.path", drv)

			w := newTestWriter(repo, repo)
			if err := w.CheckGitDrivers(); err != nil {
				t.Fatalf("fixture: the check on %s already sees the driver: %v", tc.stranded, err)
			}
			mainHead := gitOut(t, repo, "rev-parse", "main")
			want := gitOut(t, repo, "cat-file", "-p", "main:t1.yaml")

			for _, attempt := range []string{"first", "next"} {
				err := writeVia(t, tc.mode, w, "t1")
				if ran := ranPrograms(t, mark); len(ran) > 0 {
					t.Errorf("%s %s: the driver programs ran: %v", attempt, tc.mode, ran)
				}
				if !errors.Is(err, ErrGitDriverConfigured) {
					t.Fatalf("%s %s = %v, want ErrGitDriverConfigured", attempt, tc.mode, err)
				}
				// The tree is on base: not the "retry" of a stranded tree.
				if errors.Is(err, ErrTreeNotOnBase) {
					t.Errorf("%s %s = %v, which says the tree is not on base", attempt, tc.mode, err)
				}
			}
			// None of these reads runs a driver: no file content is read.
			if got := gitOut(t, repo, "symbolic-ref", "--short", "HEAD"); got != "main" {
				t.Errorf("HEAD is %q, want main", got)
			}
			if got := gitOut(t, repo, "rev-parse", "main"); got != mainHead {
				t.Errorf("main moved from %s to %s", mainHead, got)
			}
			if got := gitOut(t, repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/"); got != "main\n"+tc.stranded && got != tc.stranded+"\nmain" {
				t.Errorf("branches left:\n%s", got)
			}
			if got := gitOut(t, repo, "worktree", "list", "--porcelain"); strings.Count(got, "worktree ") != 1 {
				t.Errorf("a staging worktree was left:\n%s", got)
			}
			if got, _ := os.ReadFile(filepath.Join(repo, "t1.yaml")); strings.TrimSpace(string(got)) != want {
				t.Errorf("t1.yaml is not main's:\n%s", got)
			}
			if ran := ranPrograms(t, mark); len(ran) > 0 {
				t.Errorf("the driver programs ran: %v", ran)
			}
		})
	}
}

// A driver that applies on base only from after a PR write's commit (the
// config changed mid-write) is found by the return to base
// (checkoutBaseClean's check). The tree is on base by then and the branch
// committed and pushed, so the write completes — failing it would make a
// client retry a pushed write — but the return is not retried (a retry's
// reset would run under base's config), and the next write is refused.
func TestWritePR_DriverOnBaseFoundOnReturnIsNotRetried(t *testing.T) {
	t.Parallel()
	skipOnWindows(t)
	repo, mark := driverRepo(t)
	drv := filepath.Join(t.TempDir(), "drivers.gitconfig")
	var conf strings.Builder
	for _, key := range gitDriverKeys {
		conf.WriteString(configFileFor(key, "\"touch '"+filepath.Join(mark, key)+"'; cat\""))
	}
	if err := os.WriteFile(drv, []byte(conf.String()), 0o644); err != nil {
		t.Fatal(err)
	}
	w := newTestWriter(repo, repo)
	attempts := 0
	w.beforeBaseRestore = func(int) {
		attempts++
		gitRun(t, repo, "config", "includeIf.onbranch:main.path", drv)
	}

	res, err := w.WritePR(context.Background(), "t1", "op@example.com", silentBody("t1", "critical"))
	if err != nil {
		t.Fatalf("WritePR = %v, want success", err)
	}
	if ran := ranPrograms(t, mark); len(ran) > 0 {
		t.Errorf("the driver programs ran: %v", ran)
	}
	if attempts != 1 {
		t.Errorf("the return to base ran %d times, want 1", attempts)
	}
	if got := gitOut(t, repo, "symbolic-ref", "--short", "HEAD"); got != "main" {
		t.Errorf("HEAD is %q, want main", got)
	}
	if got := gitOut(t, repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/"); got != "main" {
		t.Errorf("branches left (%s pushed):\n%s", res.BranchName, got)
	}
	if err := writeVia(t, "WritePR", w, "t1"); !errors.Is(err, ErrGitDriverConfigured) {
		t.Errorf("next WritePR = %v, want ErrGitDriverConfigured", err)
	}
	if ran := ranPrograms(t, mark); len(ran) > 0 {
		t.Errorf("the driver programs ran: %v", ran)
	}
}

// unreadableOnMain makes the git config git reads on base (onbranch:main)
// unreadable — an include it cannot parse — while every other branch's
// stays readable: the check on base fails to read it (exit 128), while the
// commands that move HEAD there, which read their config before the move,
// run. It returns the include, to make readable again.
func unreadableOnMain(t *testing.T, repo string) string {
	t.Helper()
	bad := filepath.Join(t.TempDir(), "bad.gitconfig")
	if err := os.WriteFile(bad, []byte("[[[ not a config line\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitRun(t, repo, "config", "includeIf.onbranch:main.path", bad)
	return bad
}

// #3226: the return to base (restoreBase) whose check after the move cannot
// read base's config. The check is checkoutBaseClean's last step, so the
// tree IS on base, and the branch is committed and pushed: the write
// completes, as for a driver found there (failing it would make a client
// retry a pushed write), the return is not retried (its reset would run
// under a config not shown clean), and the next write checks again under
// the lock — refused as unreadable, never as a configured driver.
func TestWritePR_UnreadableConfigOnBaseFoundOnReturnIsNotRetried(t *testing.T) {
	t.Parallel()
	skipOnWindows(t)
	repo := seedTreeRepo(t, map[string]string{"t1.yaml": tenantBody("t1")})
	w := newTestWriter(repo, repo)
	attempts := 0
	var bad string
	w.beforeBaseRestore = func(int) {
		attempts++
		bad = unreadableOnMain(t, repo)
	}

	res, err := w.WritePR(context.Background(), "t1", "op@example.com", silentBody("t1", "critical"))
	if err != nil {
		t.Fatalf("WritePR = %v, want success", err)
	}
	if attempts != 1 {
		t.Errorf("the return to base ran %d times, want 1", attempts)
	}
	err = writeVia(t, "WritePR", w, "t1")
	if !errors.Is(err, ErrGitConfigUnreadable) {
		t.Errorf("next WritePR = %v, want ErrGitConfigUnreadable", err)
	}
	if errors.Is(err, ErrGitDriverConfigured) {
		t.Errorf("next WritePR = %v, which says a driver is configured", err)
	}

	// Readable again, for the checks below.
	if err := os.WriteFile(bad, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if got := gitOut(t, repo, "symbolic-ref", "--short", "HEAD"); got != "main" {
		t.Errorf("HEAD is %q, want main", got)
	}
	// Step 8's `branch -D` runs under the same unreadable config and fails
	// (logged): the pushed branch stays as a local ref, which no reader
	// looks at and the next write's branch does not reuse.
	if got := gitOut(t, repo, "for-each-ref", "--format=%(refname:short)", "refs/heads/"); got != "main\n"+res.BranchName {
		t.Errorf("branches = %q, want main and the pushed %s", got, res.BranchName)
	}
}

// #3226: a direct write that finds the tree on a stranded PR branch returns
// it to base (leavePRBranch) and meets a config it cannot read there. The
// answer is that refusal — retryable, the retry's check under the lock
// reads the config again — not ErrTreeNotOnBase (the tree is on base) and
// not the configured-driver refusal (nothing says one is). Nothing is
// written.
func TestWrite_StrandedPRBranchReturnMeetsUnreadableConfig(t *testing.T) {
	t.Parallel()
	skipOnWindows(t)
	repo := seedTreeRepo(t, map[string]string{"t1.yaml": tenantBody("t1")})
	mainHead := gitOut(t, repo, "rev-parse", "main")
	stranded := "tenant-api/t1/20260101-000000-0000"
	gitRun(t, repo, "checkout", "-q", "-b", stranded)
	bad := unreadableOnMain(t, repo)
	w := newTestWriter(repo, repo)
	if err := w.CheckGitDrivers(); err != nil {
		t.Fatalf("fixture: the check on %s already fails: %v", stranded, err)
	}

	err := writeVia(t, "Write", w, "t1")
	if !errors.Is(err, ErrGitConfigUnreadable) {
		t.Fatalf("Write = %v, want ErrGitConfigUnreadable", err)
	}
	if errors.Is(err, ErrTreeNotOnBase) || errors.Is(err, ErrGitDriverConfigured) {
		t.Errorf("Write = %v, want the unreadable refusal alone", err)
	}

	if err := os.WriteFile(bad, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if got := gitOut(t, repo, "symbolic-ref", "--short", "HEAD"); got != "main" {
		t.Errorf("HEAD is %q, want main", got)
	}
	if got := gitOut(t, repo, "rev-parse", "main"); got != mainHead {
		t.Errorf("main moved from %s to %s", mainHead, got)
	}
}
