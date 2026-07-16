package batchpr

// Unit tests for ShellGitClient (git_shell.go). We never shell out to
// real `git` here — instead we reuse the package-local `stubRunner`
// (declared in pr_gh_test.go) that records (dir, name, args) calls and
// replays scripted responses keyed by `args[0] + " " + args[1]`. Each
// git subcommand ShellGitClient issues maps to a distinct key
// (e.g. "ls-remote --exit-code", "status --porcelain", "rebase --onto",
// "rebase --abort", "rev-parse --abbrev-ref"), so per-call scripting is
// unambiguous and the recorded call slice lets us assert the call
// SEQUENCE.
//
// Fidelity note for BranchExistsRemote (git_shell.go:148-158): the
// exit-2 -> false branch depends on unwrapping a wrapped *exec.ExitError
// via errors.As. Go cannot synthesize an *exec.ExitError with a chosen
// ExitCode in-process (ProcessState's fields are unexported), so we run
// a real subprocess to obtain one and wrap it exactly the way
// defaultRunner.run does (fmt.Errorf("...%w", err); git_shell.go:370) —
// otherwise the test would exercise its own wrapping rather than the
// production errors.As-through-fmt-wrapper path.

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// wrappedExitErr returns an error that wraps a REAL *exec.ExitError
// whose ExitCode() == code, using the same fmt.Errorf("...%w", err)
// wrapping shape as defaultRunner.run (git_shell.go:359-372). On a
// platform without `sh` (e.g. a bare Windows host) the subprocess fails
// to start and yields an *exec.Error rather than *exec.ExitError, in
// which case the calling test is skipped — the package's Go tests are
// intended to run in the Linux dev container.
func wrappedExitErr(t *testing.T, code int) error {
	t.Helper()
	// defaultRunner uses cmd.Output(); mirror that so the ExitError we
	// obtain is byte-for-byte the kind production produces.
	_, err := exec.CommandContext(context.Background(), "sh", "-c", fmt.Sprintf("exit %d", code)).Output()
	var ee *exec.ExitError
	if !errors.As(err, &ee) {
		t.Skipf("cannot obtain a real *exec.ExitError on this platform (got %T: %v)", err, err)
	}
	if ee.ExitCode() != code {
		t.Fatalf("subprocess ExitCode() = %d, want %d", ee.ExitCode(), code)
	}
	// No-stderr branch of defaultRunner.run (git_shell.go:370): the
	// `exit N` subprocess writes nothing to stderr, so production would
	// take exactly this form.
	return fmt.Errorf("git ls-remote --exit-code origin refs/heads/x: %w", ee)
}

// gitVerbs projects the recorded call slice to the leading subcommand of
// each call (args[0]) so a test can assert the invocation SEQUENCE.
func gitVerbs(calls []stubCall) []string {
	verbs := make([]string, 0, len(calls))
	for _, c := range calls {
		if len(c.args) == 0 {
			verbs = append(verbs, c.name)
			continue
		}
		verbs = append(verbs, c.args[0])
	}
	return verbs
}

// --- BranchExistsRemote: exit-0 / exit-2 / other tri-state -------------
//
// git_shell.go:148-158. exit 0 -> (true,nil); a wrapped *exec.ExitError
// with ExitCode()==2 -> (false,nil); anything else -> (false,error).

func TestShellGitClient_BranchExistsRemote_TriState(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name    string
		errFn   func(t *testing.T) error // nil -> success (err == nil)
		want    bool
		wantErr bool
	}{
		{
			name:  "found_exit0",
			errFn: nil, // ls-remote succeeded -> branch present
			want:  true,
		},
		{
			name:  "absent_exit2_realExitError",
			errFn: func(t *testing.T) error { return wrappedExitErr(t, 2) },
			want:  false,
		},
		{
			name:    "other_exit1_realExitError_is_error",
			errFn:   func(t *testing.T) error { return wrappedExitErr(t, 1) },
			want:    false,
			wantErr: true,
		},
		{
			name:    "non_exit_error_is_error",
			errFn:   func(t *testing.T) error { return errors.New("network is unreachable") },
			want:    false,
			wantErr: true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			stub := newStubRunner()
			resp := stubResponse{}
			if tc.errFn == nil {
				// A realistic ls-remote hit line (sha \t ref).
				resp.stdout = "0123456789abcdef0123456789abcdef01234567\trefs/heads/feature\n"
			} else {
				resp.err = tc.errFn(t) // may t.Skip if platform lacks `sh`
			}
			stub.responses["ls-remote --exit-code"] = resp

			g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
			got, err := g.BranchExistsRemote(context.Background(), "feature")

			if (err != nil) != tc.wantErr {
				t.Fatalf("err = %v, wantErr = %v", err, tc.wantErr)
			}
			if got != tc.want {
				t.Errorf("BranchExistsRemote = %v, want %v", got, tc.want)
			}
			// Regardless of outcome, exactly one ls-remote call is made
			// with the fully-qualified ref.
			if len(stub.calls) != 1 {
				t.Fatalf("calls = %d, want 1", len(stub.calls))
			}
			args := stub.calls[0].args
			for _, w := range []string{"ls-remote", "--exit-code", "origin", "refs/heads/feature"} {
				if !contains(args, w) {
					t.Errorf("ls-remote args missing %q: %v", w, args)
				}
			}
		})
	}
}

// TestShellGitClient_BranchExistsRemote_Exit2UnwrapIsThroughFmtWrapper
// asserts the specific fidelity property the production comment warns
// about (git_shell.go:143-147): the *exec.ExitError is NOT at the top of
// the error chain — it sits behind a fmt.Errorf wrapper — so only
// errors.As (not a direct type assertion) recovers it. We prove the
// injected error genuinely requires unwrapping.
func TestShellGitClient_BranchExistsRemote_Exit2UnwrapIsThroughFmtWrapper(t *testing.T) {
	t.Parallel()
	wrapped := wrappedExitErr(t, 2) // t.Skip if no `sh`

	// A direct assertion on the wrapped error must FAIL (this is the bug
	// the production code was fixed to avoid); errors.As must SUCCEED.
	if _, ok := wrapped.(*exec.ExitError); ok {
		t.Fatal("wrapped error should not be a *exec.ExitError at top level")
	}
	var ee *exec.ExitError
	if !errors.As(wrapped, &ee) || ee.ExitCode() != 2 {
		t.Fatalf("errors.As should recover ExitCode 2, got ee=%v", ee)
	}

	stub := newStubRunner()
	stub.responses["ls-remote --exit-code"] = stubResponse{err: wrapped}
	g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

	got, err := g.BranchExistsRemote(context.Background(), "feature")
	if err != nil {
		t.Fatalf("exit-2 must map to (false,nil), got err=%v", err)
	}
	if got {
		t.Error("exit-2 must map to false (branch absent)")
	}
}

// --- collectRebaseConflicts: porcelain status-code table ---------------
//
// git_shell.go:269-296. A line is conflicted iff its 2-char status
// contains 'U' on either side OR is exactly "AA" / "DD". Lines shorter
// than 4 chars are skipped; results are sorted.

func TestShellGitClient_collectRebaseConflicts_StatusCodes(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name         string
		status       string // 2-char porcelain XY code
		wantConflict bool
	}{
		{"UU_both_unmerged", "UU", true},
		{"AA_both_added", "AA", true},
		{"DD_both_deleted", "DD", true},
		{"UD_unmerged_deleted", "UD", true},
		{"DU_deleted_unmerged", "DU", true},
		{"AU_added_unmerged", "AU", true},
		{"UA_unmerged_added", "UA", true},
		{"space_M_modified_not_conflict", " M", false},
		{"A_space_staged_add_not_conflict", "A ", false},
		{"question_untracked_not_conflict", "??", false},
		{"space_D_deleted_not_conflict", " D", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			stub := newStubRunner()
			// "XY path" — index 2 is the separator space, path starts at 3.
			stub.responses["status --porcelain"] = stubResponse{
				stdout: tc.status + " config/app.yaml\n",
			}
			g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

			files, err := g.collectRebaseConflicts(context.Background())
			if err != nil {
				t.Fatalf("collectRebaseConflicts: %v", err)
			}
			if tc.wantConflict {
				if !reflect.DeepEqual(files, []string{"config/app.yaml"}) {
					t.Errorf("status %q -> files = %v, want [config/app.yaml]", tc.status, files)
				}
			} else if len(files) != 0 {
				t.Errorf("status %q -> files = %v, want none", tc.status, files)
			}
		})
	}
}

// Multiple conflicts across states are de-noised (non-conflicts dropped)
// and returned sorted; short/blank lines are ignored.
func TestShellGitClient_collectRebaseConflicts_MixedAndSorted(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	// Deliberately unsorted, mixing conflict + non-conflict + a blank
	// line and a too-short line that must both be skipped.
	stub.responses["status --porcelain"] = stubResponse{stdout: strings.Join([]string{
		"UU config/gamma.yaml",
		" M config/clean.yaml", // not a conflict
		"AA config/alpha.yaml",
		"?? config/untracked.yaml", // not a conflict
		"DD config/delta.yaml",
		"UD config/beta.yaml",
		"",                    // blank -> skipped (len < 4)
		"M  config/staged.go", // not a conflict
	}, "\n") + "\n"}
	g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

	files, err := g.collectRebaseConflicts(context.Background())
	if err != nil {
		t.Fatalf("collectRebaseConflicts: %v", err)
	}
	want := []string{
		"config/alpha.yaml",
		"config/beta.yaml",
		"config/delta.yaml",
		"config/gamma.yaml",
	}
	if !reflect.DeepEqual(files, want) {
		t.Errorf("files = %v, want %v", files, want)
	}
}

// The underlying `git status --porcelain` failing surfaces as an error.
func TestShellGitClient_collectRebaseConflicts_StatusError(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["status --porcelain"] = stubResponse{err: errors.New("not a git repo")}
	g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

	if _, err := g.collectRebaseConflicts(context.Background()); err == nil {
		t.Fatal("expected error when git status fails")
	}
}

// --- RebaseOnto: input validation --------------------------------------
//
// git_shell.go:207-216. Empty branch/oldBase/newBase short-circuit
// before any git call.

func TestShellGitClient_RebaseOnto_ValidatesInputs(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name                     string
		branch, oldBase, newBase string
	}{
		{"empty_branch", "", "old", "new"},
		{"empty_oldBase", "feature", "", "new"},
		{"empty_newBase", "feature", "old", ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			stub := newStubRunner()
			g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
			out, err := g.RebaseOnto(context.Background(), tc.branch, tc.oldBase, tc.newBase)
			if err == nil {
				t.Fatal("expected validation error")
			}
			if out != nil {
				t.Errorf("outcome = %+v, want nil", out)
			}
			if len(stub.calls) != 0 {
				t.Errorf("validation should not shell out; calls = %v", gitVerbs(stub.calls))
			}
		})
	}
}

// fetch / checkout failures are hard errors surfaced before any rebase.
func TestShellGitClient_RebaseOnto_FetchOrCheckoutErrors(t *testing.T) {
	t.Parallel()

	t.Run("fetch_fails", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		stub.responses["fetch origin"] = stubResponse{err: errors.New("no route to host")}
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		out, err := g.RebaseOnto(context.Background(), "feature", "old", "new")
		if err == nil || !strings.Contains(err.Error(), "git fetch origin") {
			t.Fatalf("err = %v, want git-fetch-origin", err)
		}
		if out != nil {
			t.Errorf("outcome = %+v, want nil", out)
		}
		if verbs := gitVerbs(stub.calls); !reflect.DeepEqual(verbs, []string{"fetch"}) {
			t.Errorf("verbs = %v, want [fetch]", verbs)
		}
	})

	t.Run("checkout_fails", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		stub.responses["checkout feature"] = stubResponse{err: errors.New("pathspec did not match")}
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		out, err := g.RebaseOnto(context.Background(), "feature", "old", "new")
		if err == nil || !strings.Contains(err.Error(), "git checkout feature") {
			t.Fatalf("err = %v, want git-checkout-feature", err)
		}
		if out != nil {
			t.Errorf("outcome = %+v, want nil", out)
		}
		if verbs := gitVerbs(stub.calls); !reflect.DeepEqual(verbs, []string{"fetch", "checkout"}) {
			t.Errorf("verbs = %v, want [fetch checkout]", verbs)
		}
	})
}

// Clean rebase (a real rebase happened, no up-to-date marker) ->
// AlreadyUpToDate=false, Conflicted=false, sequence fetch/checkout/rebase.
func TestShellGitClient_RebaseOnto_Clean(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["rebase --onto"] = stubResponse{
		stdout: "Successfully rebased and updated refs/heads/feature.\n",
	}
	g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

	out, err := g.RebaseOnto(context.Background(), "feature", "old", "new")
	if err != nil {
		t.Fatalf("RebaseOnto: %v", err)
	}
	if out == nil || out.Conflicted || out.AlreadyUpToDate {
		t.Fatalf("outcome = %+v, want clean (no conflict, not up-to-date)", out)
	}
	if verbs := gitVerbs(stub.calls); !reflect.DeepEqual(verbs, []string{"fetch", "checkout", "rebase"}) {
		t.Errorf("verbs = %v, want [fetch checkout rebase]", verbs)
	}
	// The rebase invocation carries the --onto operands in order.
	var rebaseArgs []string
	for _, c := range stub.calls {
		if len(c.args) > 0 && c.args[0] == "rebase" {
			rebaseArgs = c.args
		}
	}
	for _, w := range []string{"rebase", "--onto", "new", "old", "feature"} {
		if !contains(rebaseArgs, w) {
			t.Errorf("rebase args missing %q: %v", w, rebaseArgs)
		}
	}
}

// "Already up to date" detection exercises BOTH arms of the OR in
// git_shell.go:232-233: the literal "is up to date" substring, and the
// looser "Current branch" + "up to date" co-occurrence.
func TestShellGitClient_RebaseOnto_AlreadyUpToDate(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name    string
		stdout  string
		wantUTD bool
	}{
		{
			name:    "literal_is_up_to_date",
			stdout:  "Current branch feature is up to date.\n",
			wantUTD: true,
		},
		{
			// No "is up to date" substring, but "Current branch" and
			// "up to date" both present -> second OR arm.
			name:    "current_branch_plus_up_to_date",
			stdout:  "Current branch feature is now up to date with the new base.\n",
			wantUTD: true,
		},
		{
			name:    "real_rebase_not_up_to_date",
			stdout:  "Successfully rebased and updated refs/heads/feature.\n",
			wantUTD: false,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			stub := newStubRunner()
			stub.responses["rebase --onto"] = stubResponse{stdout: tc.stdout}
			g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

			out, err := g.RebaseOnto(context.Background(), "feature", "old", "new")
			if err != nil {
				t.Fatalf("RebaseOnto: %v", err)
			}
			if out.Conflicted {
				t.Errorf("clean stdout must not be Conflicted: %+v", out)
			}
			if out.AlreadyUpToDate != tc.wantUTD {
				t.Errorf("AlreadyUpToDate = %v, want %v (stdout %q)", out.AlreadyUpToDate, tc.wantUTD, tc.stdout)
			}
		})
	}
}

// Conflict path: rebase errors -> collect conflicts via status --porcelain
// -> abort -> return a non-error Conflicted outcome. Call sequence and
// conflicted-file list are both asserted. git_shell.go:236-258.
func TestShellGitClient_RebaseOnto_ConflictAbortsAndReports(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["rebase --onto"] = stubResponse{err: errors.New("exit status 1: could not apply")}
	stub.responses["status --porcelain"] = stubResponse{stdout: strings.Join([]string{
		"UU config/app.yaml",
		"AA config/new.yaml",
	}, "\n") + "\n"}
	// "rebase --abort" left unscripted -> default (\"\", nil) = success.
	g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

	out, err := g.RebaseOnto(context.Background(), "feature", "old", "new")
	if err != nil {
		t.Fatalf("conflicts are an OUTCOME, not an error; got err=%v", err)
	}
	if out == nil || !out.Conflicted || out.AlreadyUpToDate {
		t.Fatalf("outcome = %+v, want Conflicted", out)
	}
	if !reflect.DeepEqual(out.ConflictedFiles, []string{"config/app.yaml", "config/new.yaml"}) {
		t.Errorf("ConflictedFiles = %v", out.ConflictedFiles)
	}
	// fetch -> checkout -> rebase -> status(collect) -> rebase(abort).
	if verbs := gitVerbs(stub.calls); !reflect.DeepEqual(verbs,
		[]string{"fetch", "checkout", "rebase", "status", "rebase"}) {
		t.Errorf("verbs = %v, want [fetch checkout rebase status rebase]", verbs)
	}
	// The final call must be the abort, not another --onto.
	last := stub.calls[len(stub.calls)-1].args
	if !contains(last, "--abort") {
		t.Errorf("last git call args = %v, want a rebase --abort", last)
	}
}

// If both the rebase AND the abort fail, the impl returns a hard error
// mentioning the abort failure (git_shell.go:241-247).
func TestShellGitClient_RebaseOnto_ConflictAbortFails(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["rebase --onto"] = stubResponse{err: errors.New("conflict")}
	stub.responses["status --porcelain"] = stubResponse{stdout: "UU config/app.yaml\n"}
	stub.responses["rebase --abort"] = stubResponse{err: errors.New("no rebase in progress")}
	g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

	out, err := g.RebaseOnto(context.Background(), "feature", "old", "new")
	if err == nil || !strings.Contains(err.Error(), "abort failed") {
		t.Fatalf("err = %v, want abort-failed", err)
	}
	if out != nil {
		t.Errorf("outcome = %+v, want nil on abort failure", out)
	}
}

// If the conflict-file query itself fails (status --porcelain errors) but
// abort succeeds, the impl returns a hard error mentioning the failed
// status query (git_shell.go:248-253).
func TestShellGitClient_RebaseOnto_ConflictStatusQueryFails(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["rebase --onto"] = stubResponse{err: errors.New("conflict")}
	stub.responses["status --porcelain"] = stubResponse{err: errors.New("status blew up")}
	// abort unscripted -> succeeds.
	g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

	out, err := g.RebaseOnto(context.Background(), "feature", "old", "new")
	if err == nil || !strings.Contains(err.Error(), "conflict-list query also failed") {
		t.Fatalf("err = %v, want conflict-list-query-also-failed", err)
	}
	if out != nil {
		t.Errorf("outcome = %+v, want nil", out)
	}
}

// --- WriteFiles: wrong-branch guard + real file write ------------------
//
// git_shell.go:80-101. WriteFiles refuses to write when HEAD is not on
// the expected branch, and otherwise writes files under Workdir and
// stages them.

func TestShellGitClient_WriteFiles_WrongBranchRefusesToWrite(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	stub := newStubRunner()
	stub.responses["rev-parse --abbrev-ref"] = stubResponse{stdout: "main\n"}
	g := &ShellGitClient{Workdir: dir, run: stub}

	err := g.WriteFiles(context.Background(), "feature", map[string][]byte{
		"config/app.yaml": []byte("threshold: 5"),
	})
	if err == nil || !strings.Contains(err.Error(), `expected branch "feature"`) {
		t.Fatalf("err = %v, want wrong-branch guard", err)
	}
	// Nothing should have been written or staged.
	if _, statErr := os.Stat(filepath.Join(dir, "config", "app.yaml")); !os.IsNotExist(statErr) {
		t.Errorf("file must not exist after guard trip; stat err = %v", statErr)
	}
	if verbs := gitVerbs(stub.calls); !reflect.DeepEqual(verbs, []string{"rev-parse"}) {
		t.Errorf("verbs = %v, want only [rev-parse] (no add)", verbs)
	}
}

func TestShellGitClient_WriteFiles_HappyPathWritesAndStages(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	stub := newStubRunner()
	stub.responses["rev-parse --abbrev-ref"] = stubResponse{stdout: "feature\n"}
	g := &ShellGitClient{Workdir: dir, run: stub}

	body := []byte("threshold: 5\n")
	err := g.WriteFiles(context.Background(), "feature", map[string][]byte{
		"config/rules/app.yaml": body,
	})
	if err != nil {
		t.Fatalf("WriteFiles: %v", err)
	}
	// File materialized on disk under Workdir with the slash path mapped
	// to the OS separator.
	got, readErr := os.ReadFile(filepath.Join(dir, "config", "rules", "app.yaml"))
	if readErr != nil {
		t.Fatalf("read written file: %v", readErr)
	}
	if !reflect.DeepEqual(got, body) {
		t.Errorf("file body = %q, want %q", got, body)
	}
	// It was staged with the original slash-form relpath after `--`.
	var addArgs []string
	for _, c := range stub.calls {
		if len(c.args) > 0 && c.args[0] == "add" {
			addArgs = c.args
		}
	}
	for _, w := range []string{"add", "--", "config/rules/app.yaml"} {
		if !contains(addArgs, w) {
			t.Errorf("add args missing %q: %v", w, addArgs)
		}
	}
}

func TestShellGitClient_WriteFiles_CurrentBranchReadError(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["rev-parse --abbrev-ref"] = stubResponse{err: errors.New("detached HEAD?")}
	g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

	err := g.WriteFiles(context.Background(), "feature", map[string][]byte{"a.txt": []byte("x")})
	if err == nil || !strings.Contains(err.Error(), "read current branch") {
		t.Fatalf("err = %v, want read-current-branch", err)
	}
}

// --- Commit: wrong-branch guard + author handling ----------------------
//
// git_shell.go:108-124.

func TestShellGitClient_Commit_WrongBranchRefuses(t *testing.T) {
	t.Parallel()
	stub := newStubRunner()
	stub.responses["rev-parse --abbrev-ref"] = stubResponse{stdout: "main\n"}
	g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

	err := g.Commit(context.Background(), "feature", "msg", "")
	if err == nil || !strings.Contains(err.Error(), `expected branch "feature"`) {
		t.Fatalf("err = %v, want wrong-branch guard", err)
	}
	if verbs := gitVerbs(stub.calls); !reflect.DeepEqual(verbs, []string{"rev-parse"}) {
		t.Errorf("verbs = %v, want only [rev-parse] (no commit)", verbs)
	}
}

func TestShellGitClient_Commit_AuthorHandling(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name       string
		author     string
		wantAuthor bool
	}{
		{"with_author", "Alpha Dev <alpha@example.com>", true},
		{"empty_author_falls_back_to_git_config", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			stub := newStubRunner()
			stub.responses["rev-parse --abbrev-ref"] = stubResponse{stdout: "feature\n"}
			g := &ShellGitClient{Workdir: t.TempDir(), run: stub}

			if err := g.Commit(context.Background(), "feature", "a message", tc.author); err != nil {
				t.Fatalf("Commit: %v", err)
			}
			var commitArgs []string
			for _, c := range stub.calls {
				if len(c.args) > 0 && c.args[0] == "commit" {
					commitArgs = c.args
				}
			}
			for _, w := range []string{"commit", "-m", "a message"} {
				if !contains(commitArgs, w) {
					t.Errorf("commit args missing %q: %v", w, commitArgs)
				}
			}
			hasAuthor := false
			for _, a := range commitArgs {
				if strings.HasPrefix(a, "--author=") {
					hasAuthor = true
				}
			}
			if hasAuthor != tc.wantAuthor {
				t.Errorf("--author present = %v, want %v (args %v)", hasAuthor, tc.wantAuthor, commitArgs)
			}
		})
	}
}

// --- ForcePushWithLease: arg assertion (light) -------------------------
//
// git_shell.go:315-323.

func TestShellGitClient_ForcePushWithLease(t *testing.T) {
	t.Parallel()

	t.Run("builds_force_with_lease_args", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		if err := g.ForcePushWithLease(context.Background(), "feature"); err != nil {
			t.Fatalf("ForcePushWithLease: %v", err)
		}
		if len(stub.calls) != 1 {
			t.Fatalf("calls = %d, want 1", len(stub.calls))
		}
		for _, w := range []string{"push", "--force-with-lease", "origin", "feature"} {
			if !contains(stub.calls[0].args, w) {
				t.Errorf("args missing %q: %v", w, stub.calls[0].args)
			}
		}
	})

	t.Run("empty_branch_errors_without_shelling_out", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		if err := g.ForcePushWithLease(context.Background(), ""); err == nil {
			t.Fatal("empty branch must error")
		}
		if len(stub.calls) != 0 {
			t.Errorf("must not shell out on empty branch; calls = %v", gitVerbs(stub.calls))
		}
	})
}

// --- CreateBranch: validation + `checkout -B` args ----------------------
//
// git_shell.go:66-74. `-B` (create-or-reset) is the load-bearing detail:
// re-running Apply() after a partial failure must not trip over a stale
// local branch.

func TestShellGitClient_CreateBranch(t *testing.T) {
	t.Parallel()

	t.Run("empty_name_errors_without_shelling_out", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		if err := g.CreateBranch(context.Background(), "", "main"); err == nil {
			t.Fatal("empty branch name must error")
		}
		if len(stub.calls) != 0 {
			t.Errorf("must not shell out on empty name; calls = %v", gitVerbs(stub.calls))
		}
	})

	t.Run("issues_checkout_dash_B_from_base", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		if err := g.CreateBranch(context.Background(), "da-tools/c10/t-abc", "main"); err != nil {
			t.Fatalf("CreateBranch: %v", err)
		}
		if len(stub.calls) != 1 {
			t.Fatalf("calls = %d, want 1", len(stub.calls))
		}
		// Exact arg order matters: -B <name> <base> (base LAST — swapping
		// them would reset the base branch instead).
		want := []string{"checkout", "-B", "da-tools/c10/t-abc", "main"}
		if !reflect.DeepEqual(stub.calls[0].args, want) {
			t.Errorf("args = %v, want %v", stub.calls[0].args, want)
		}
	})

	t.Run("git_failure_is_wrapped", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		stub.responses["checkout -B"] = stubResponse{err: errors.New("fatal: not a git repository")}
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		err := g.CreateBranch(context.Background(), "feature", "main")
		if err == nil || !strings.Contains(err.Error(), "git checkout -B feature main") {
			t.Fatalf("err = %v, want wrapped checkout failure", err)
		}
	})
}

// --- Push: `--set-upstream origin <branch>` -----------------------------
//
// git_shell.go:129-134.

func TestShellGitClient_Push(t *testing.T) {
	t.Parallel()

	t.Run("issues_set_upstream_push", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		if err := g.Push(context.Background(), "feature"); err != nil {
			t.Fatalf("Push: %v", err)
		}
		if len(stub.calls) != 1 {
			t.Fatalf("calls = %d, want 1", len(stub.calls))
		}
		want := []string{"push", "--set-upstream", "origin", "feature"}
		if !reflect.DeepEqual(stub.calls[0].args, want) {
			t.Errorf("args = %v, want %v", stub.calls[0].args, want)
		}
		// Plain push — never a force variant (force-push is a separate,
		// deliberate API: ForcePushWithLease).
		for _, a := range stub.calls[0].args {
			if strings.Contains(a, "force") {
				t.Errorf("Push must never force; args = %v", stub.calls[0].args)
			}
		}
	})

	t.Run("git_failure_is_wrapped", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		stub.responses["push --set-upstream"] = stubResponse{err: errors.New("remote: permission denied")}
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		err := g.Push(context.Background(), "feature")
		if err == nil || !strings.Contains(err.Error(), "git push origin feature") {
			t.Fatalf("err = %v, want wrapped push failure", err)
		}
	})
}

// --- CheckoutBranch: fetch-then-checkout, bare form ----------------------
//
// git_shell.go:173-184. The checkout is deliberately the bare form (no
// -B): an existing branch with commits must be preserved, not reset.

func TestShellGitClient_CheckoutBranch(t *testing.T) {
	t.Parallel()

	t.Run("empty_name_errors_without_shelling_out", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		if err := g.CheckoutBranch(context.Background(), ""); err == nil {
			t.Fatal("empty branch name must error")
		}
		if len(stub.calls) != 0 {
			t.Errorf("must not shell out on empty name; calls = %v", gitVerbs(stub.calls))
		}
	})

	t.Run("fetch_then_bare_checkout", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		if err := g.CheckoutBranch(context.Background(), "feature"); err != nil {
			t.Fatalf("CheckoutBranch: %v", err)
		}
		if verbs := gitVerbs(stub.calls); !reflect.DeepEqual(verbs, []string{"fetch", "checkout"}) {
			t.Fatalf("verbs = %v, want [fetch checkout]", verbs)
		}
		checkoutArgs := stub.calls[1].args
		if !reflect.DeepEqual(checkoutArgs, []string{"checkout", "feature"}) {
			t.Errorf("checkout args = %v, want bare [checkout feature] (a -B here would reset existing commits)", checkoutArgs)
		}
	})

	t.Run("fetch_failure_stops_before_checkout", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		stub.responses["fetch origin"] = stubResponse{err: errors.New("no route to host")}
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		err := g.CheckoutBranch(context.Background(), "feature")
		if err == nil || !strings.Contains(err.Error(), "git fetch origin") {
			t.Fatalf("err = %v, want fetch failure", err)
		}
		if verbs := gitVerbs(stub.calls); !reflect.DeepEqual(verbs, []string{"fetch"}) {
			t.Errorf("verbs = %v, want [fetch] only", verbs)
		}
	})

	t.Run("checkout_failure_is_wrapped", func(t *testing.T) {
		t.Parallel()
		stub := newStubRunner()
		stub.responses["checkout feature"] = stubResponse{err: errors.New("pathspec did not match")}
		g := &ShellGitClient{Workdir: t.TempDir(), run: stub}
		err := g.CheckoutBranch(context.Background(), "feature")
		if err == nil || !strings.Contains(err.Error(), "git checkout feature") {
			t.Fatalf("err = %v, want checkout failure", err)
		}
	})
}
