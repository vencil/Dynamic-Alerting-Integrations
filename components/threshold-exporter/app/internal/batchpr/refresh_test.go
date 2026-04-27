package batchpr

import (
	"context"
	"errors"
	"strings"
	"testing"
)

// fixtureRefreshInput builds a Refresh input with N targets all
// pointing at fresh fakeGit/fakePR defaults.
func fixtureRefreshInput(targetNums ...int) RefreshInput {
	targets := make([]RefreshTarget, 0, len(targetNums))
	for _, n := range targetNums {
		targets = append(targets, RefreshTarget{
			PRNumber:   n,
			BranchName: branchFor(n),
		})
	}
	return RefreshInput{
		Repo:               Repo{Owner: "o", Name: "r", BaseBranch: "main"},
		BaseMergedSHA:      "abcdef1234567890",
		BaseMergedPRNumber: 100,
		Targets:            targets,
	}
}

func branchFor(num int) string { return "tenant-branch-" + itoa(num) }

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	digits := []byte{}
	neg := n < 0
	if neg {
		n = -n
	}
	for n > 0 {
		digits = append([]byte{byte('0' + n%10)}, digits...)
		n /= 10
	}
	if neg {
		digits = append([]byte{'-'}, digits...)
	}
	return string(digits)
}

func runRefresh(t *testing.T, mutate func(in *RefreshInput, g *fakeGit, p *fakePR)) (*RefreshResult, *fakeGit, *fakePR) {
	t.Helper()
	in := fixtureRefreshInput(101, 102)
	g := newFakeGit()
	p := newFakePR()
	if mutate != nil {
		mutate(&in, g, p)
	}
	result, err := Refresh(context.Background(), in, g, p)
	if err != nil {
		t.Fatalf("Refresh: unexpected error: %v", err)
	}
	return result, g, p
}

// --- Validation -------------------------------------------------

func TestRefresh_RejectsEmptyRepoOwner(t *testing.T) {
	in := fixtureRefreshInput(101)
	in.Repo.Owner = ""
	_, err := Refresh(context.Background(), in, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "Repo.Owner") {
		t.Errorf("expected Repo.Owner error; got %v", err)
	}
}

func TestRefresh_RejectsEmptyBaseBranch(t *testing.T) {
	in := fixtureRefreshInput(101)
	in.Repo.BaseBranch = ""
	_, err := Refresh(context.Background(), in, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "Repo.BaseBranch") {
		t.Errorf("expected Repo.BaseBranch error; got %v", err)
	}
}

func TestRefresh_RejectsEmptyBaseMergedSHA(t *testing.T) {
	in := fixtureRefreshInput(101)
	in.BaseMergedSHA = ""
	_, err := Refresh(context.Background(), in, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "BaseMergedSHA") {
		t.Errorf("expected BaseMergedSHA error; got %v", err)
	}
}

func TestRefresh_RejectsNilGitClient(t *testing.T) {
	in := fixtureRefreshInput(101)
	_, err := Refresh(context.Background(), in, nil, newFakePR())
	if err == nil || !strings.Contains(err.Error(), "GitClient") {
		t.Errorf("expected GitClient error; got %v", err)
	}
}

func TestRefresh_RejectsNilPRClient(t *testing.T) {
	in := fixtureRefreshInput(101)
	_, err := Refresh(context.Background(), in, newFakeGit(), nil)
	if err == nil || !strings.Contains(err.Error(), "PRClient") {
		t.Errorf("expected PRClient error; got %v", err)
	}
}

func TestRefresh_RejectsZeroTargetPRNumber(t *testing.T) {
	in := fixtureRefreshInput()
	in.Targets = []RefreshTarget{{PRNumber: 0, BranchName: "x"}}
	_, err := Refresh(context.Background(), in, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "PRNumber must be > 0") {
		t.Errorf("expected PRNumber>0 error; got %v", err)
	}
}

func TestRefresh_RejectsEmptyTargetBranch(t *testing.T) {
	in := fixtureRefreshInput()
	in.Targets = []RefreshTarget{{PRNumber: 5, BranchName: ""}}
	_, err := Refresh(context.Background(), in, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "BranchName is required") {
		t.Errorf("expected BranchName error; got %v", err)
	}
}

// --- Empty Targets is a no-op ------------------------------------

func TestRefresh_EmptyTargetsNoOp(t *testing.T) {
	in := fixtureRefreshInput()
	in.Targets = nil
	r, err := Refresh(context.Background(), in, newFakeGit(), newFakePR())
	if err != nil {
		t.Fatalf("empty targets should not error: %v", err)
	}
	if r.Summary.TotalTargets != 0 {
		t.Errorf("TotalTargets=%d, want 0", r.Summary.TotalTargets)
	}
	if !strings.Contains(r.ReportMarkdown, "no-op") {
		t.Errorf("report should note no-op for empty targets; got %q", r.ReportMarkdown)
	}
}

// --- Happy path: clean rebase across all targets ----------------

func TestRefresh_HappyPath_AllClean(t *testing.T) {
	r, g, p := runRefresh(t, nil)

	if r.Summary.CleanCount != 2 {
		t.Errorf("CleanCount=%d, want 2", r.Summary.CleanCount)
	}
	if r.Summary.TotalTargets != 2 {
		t.Errorf("TotalTargets=%d, want 2", r.Summary.TotalTargets)
	}
	// Each branch should have been rebased + force-pushed once.
	if len(g.rebaseCalls) != 2 || len(g.forcePushCalls) != 2 {
		t.Errorf("rebase=%d / force-push=%d, want 2 each", len(g.rebaseCalls), len(g.forcePushCalls))
	}
	// One success comment per PR.
	for _, num := range []int{101, 102} {
		if len(p.commentCalls[num]) != 1 {
			t.Errorf("PR #%d should have 1 comment; got %d", num, len(p.commentCalls[num]))
			continue
		}
		body := p.commentCalls[num][0]
		if !strings.Contains(body, "Auto-rebased") || !strings.Contains(body, "#100") {
			t.Errorf("PR #%d comment should reference Base PR #100 + Auto-rebased; got %q", num, body)
		}
	}
}

// --- Conflict path ----------------------------------------------

func TestRefresh_ConflictsRecordedAndReportListsFiles(t *testing.T) {
	r, _, p := runRefresh(t, func(in *RefreshInput, g *fakeGit, _ *fakePR) {
		// Make PR 101's rebase report conflicts with 2 files.
		g.rebaseOutcomes[branchFor(101)] = &RebaseOutcome{
			Conflicted:      true,
			ConflictedFiles: []string{"conf.d/foo/_defaults.yaml", "conf.d/foo/tenant-a.yaml"},
		}
		// PR 102 stays clean (default).
	})

	if r.Summary.ConflictsCount != 1 {
		t.Errorf("ConflictsCount=%d, want 1", r.Summary.ConflictsCount)
	}
	if r.Summary.CleanCount != 1 {
		t.Errorf("CleanCount=%d, want 1", r.Summary.CleanCount)
	}
	if r.Items[0].Status != RebaseConflicts {
		t.Errorf("Items[0].Status=%q, want %q", r.Items[0].Status, RebaseConflicts)
	}
	if got, want := len(r.Items[0].ConflictedFiles), 2; got != want {
		t.Errorf("Items[0].ConflictedFiles count=%d, want %d", got, want)
	}
	// Conflicted PR should NOT receive a force-push or success comment.
	if _, ok := p.commentCalls[101]; ok {
		t.Errorf("conflicted PR #101 should not receive a success comment")
	}
	// Report should include the conflict section + suggested commands.
	if !strings.Contains(r.ReportMarkdown, "## Conflicts") {
		t.Errorf("report should have a Conflicts section; got %q", r.ReportMarkdown)
	}
	if !strings.Contains(r.ReportMarkdown, "git rebase --onto") {
		t.Errorf("report should suggest a manual rebase command; got %q", r.ReportMarkdown)
	}
}

// --- Closed/merged PR skipped ---------------------------------

func TestRefresh_ClosedPRSkipped(t *testing.T) {
	r, g, p := runRefresh(t, func(in *RefreshInput, _ *fakeGit, p *fakePR) {
		p.prDetails[101] = &PRDetails{Number: 101, State: PRStateClosed, HeadBranch: branchFor(101)}
	})

	if r.Summary.SkippedCount != 1 {
		t.Errorf("SkippedCount=%d, want 1", r.Summary.SkippedCount)
	}
	if r.Items[0].Status != RebaseSkippedClosed {
		t.Errorf("Items[0].Status=%q, want %q", r.Items[0].Status, RebaseSkippedClosed)
	}
	if r.Items[0].PRState != PRStateClosed {
		t.Errorf("Items[0].PRState=%q, want %q", r.Items[0].PRState, PRStateClosed)
	}
	// No rebase / push / comment for the closed one.
	for _, c := range g.rebaseCalls {
		if strings.HasPrefix(c, branchFor(101)+":") {
			t.Errorf("closed PR #101 should not have rebase invocations; got %q", c)
		}
	}
	if len(p.commentCalls[101]) != 0 {
		t.Errorf("closed PR #101 should not receive a comment by default")
	}
}

func TestRefresh_MergedPRSkipped(t *testing.T) {
	r, _, _ := runRefresh(t, func(in *RefreshInput, _ *fakeGit, p *fakePR) {
		p.prDetails[101] = &PRDetails{Number: 101, State: PRStateMerged, HeadBranch: branchFor(101)}
	})
	if r.Items[0].PRState != PRStateMerged {
		t.Errorf("merged PR should report PRStateMerged; got %q", r.Items[0].PRState)
	}
	if r.Items[0].Status != RebaseSkippedClosed {
		t.Errorf("merged PR should map to RebaseSkippedClosed; got %q", r.Items[0].Status)
	}
}

func TestRefresh_PostCommentOnSkippedFlagPostsComment(t *testing.T) {
	r, _, p := runRefresh(t, func(in *RefreshInput, _ *fakeGit, p *fakePR) {
		in.PostCommentOnSkipped = true
		p.prDetails[101] = &PRDetails{Number: 101, State: PRStateClosed, HeadBranch: branchFor(101)}
	})
	if r.Summary.SkippedCount != 1 {
		t.Fatalf("SkippedCount=%d, want 1", r.Summary.SkippedCount)
	}
	if len(p.commentCalls[101]) == 0 {
		t.Errorf("PostCommentOnSkipped=true should post a comment on closed PR #101")
	}
	if !strings.Contains(p.commentCalls[101][0], "skipped") {
		t.Errorf("skip-comment should mention 'skipped'; got %q", p.commentCalls[101][0])
	}
}

// --- Dry-run ----------------------------------------------------

func TestRefresh_DryRunDoesNoRemoteWork(t *testing.T) {
	r, g, p := runRefresh(t, func(in *RefreshInput, _ *fakeGit, _ *fakePR) {
		in.DryRun = true
	})
	if r.Summary.DryRunCount != 2 {
		t.Errorf("DryRunCount=%d, want 2", r.Summary.DryRunCount)
	}
	if len(g.rebaseCalls) != 0 || len(g.forcePushCalls) != 0 {
		t.Errorf("DryRun should not run rebase or push; got rebase=%d push=%d",
			len(g.rebaseCalls), len(g.forcePushCalls))
	}
	if len(p.commentCalls) != 0 {
		t.Errorf("DryRun should not post comments; got %d", len(p.commentCalls))
	}
	// Report header should call out dry-run mode.
	if !strings.Contains(r.ReportMarkdown, "dry-run") {
		t.Errorf("report should mark dry-run; got %q", r.ReportMarkdown)
	}
}

// --- Failure paths ---------------------------------------------

func TestRefresh_GetPRFailureRecordsStep(t *testing.T) {
	r, _, _ := runRefresh(t, func(in *RefreshInput, _ *fakeGit, p *fakePR) {
		p.getPRErr[101] = errors.New("API rate limited")
	})
	if r.Summary.FailedCount != 1 {
		t.Errorf("FailedCount=%d, want 1", r.Summary.FailedCount)
	}
	if r.Items[0].Status != RebaseFailed {
		t.Errorf("Items[0].Status=%q, want %q", r.Items[0].Status, RebaseFailed)
	}
	if r.Items[0].Step != "get_pr" {
		t.Errorf("Items[0].Step=%q, want 'get_pr'", r.Items[0].Step)
	}
	if !strings.Contains(r.Items[0].ErrorMessage, "rate limited") {
		t.Errorf("ErrorMessage should include the underlying cause; got %q", r.Items[0].ErrorMessage)
	}
}

func TestRefresh_RebaseHardErrorRecordsStep(t *testing.T) {
	r, _, _ := runRefresh(t, func(in *RefreshInput, g *fakeGit, _ *fakePR) {
		g.rebaseErr[branchFor(101)] = errors.New("not a git repo")
	})
	if r.Items[0].Status != RebaseFailed {
		t.Errorf("Items[0].Status=%q, want %q", r.Items[0].Status, RebaseFailed)
	}
	if r.Items[0].Step != "rebase" {
		t.Errorf("Items[0].Step=%q, want 'rebase'", r.Items[0].Step)
	}
}

func TestRefresh_PushFailureRecordsStep(t *testing.T) {
	r, _, _ := runRefresh(t, func(in *RefreshInput, g *fakeGit, _ *fakePR) {
		g.forcePushErr[branchFor(101)] = errors.New("remote rejected")
	})
	if r.Items[0].Status != RebaseFailed {
		t.Errorf("Items[0].Status=%q, want %q", r.Items[0].Status, RebaseFailed)
	}
	if r.Items[0].Step != "push" {
		t.Errorf("Items[0].Step=%q, want 'push'", r.Items[0].Step)
	}
}

func TestRefresh_CommentFailureRecordsStep(t *testing.T) {
	r, _, _ := runRefresh(t, func(in *RefreshInput, _ *fakeGit, p *fakePR) {
		p.commentErr[101] = errors.New("API timeout")
	})
	if r.Items[0].Status != RebaseFailed {
		t.Errorf("Items[0].Status=%q, want %q", r.Items[0].Status, RebaseFailed)
	}
	if r.Items[0].Step != "comment" {
		t.Errorf("Items[0].Step=%q, want 'comment'", r.Items[0].Step)
	}
	// Comment failure includes a hint that the underlying refresh succeeded.
	if !strings.Contains(r.Items[0].ErrorMessage, "rebase + push DID succeed") {
		t.Errorf("comment failure should clarify rebase succeeded; got %q", r.Items[0].ErrorMessage)
	}
}

// --- Per-target loop continues after one failure ---------------

func TestRefresh_OneFailureDoesNotSinkBatch(t *testing.T) {
	r, _, p := runRefresh(t, func(in *RefreshInput, g *fakeGit, _ *fakePR) {
		// PR 101 fails on rebase; PR 102 should still process cleanly.
		g.rebaseErr[branchFor(101)] = errors.New("disk full")
	})
	if r.Items[0].Status != RebaseFailed {
		t.Errorf("Items[0].Status=%q, want %q", r.Items[0].Status, RebaseFailed)
	}
	if r.Items[1].Status != RebaseClean {
		t.Errorf("Items[1].Status=%q, want %q (one bad apple shouldn't sink batch)", r.Items[1].Status, RebaseClean)
	}
	if len(p.commentCalls[102]) != 1 {
		t.Errorf("PR #102 should still receive a success comment; got %d", len(p.commentCalls[102]))
	}
}

// --- Custom comment body --------------------------------------

func TestRefresh_CustomCommentBodyUsedAndSubstitutesBaseSHA(t *testing.T) {
	_, _, p := runRefresh(t, func(in *RefreshInput, _ *fakeGit, _ *fakePR) {
		in.CommentBody = "rebased onto <base-sha> — please review at your convenience"
	})
	body := p.commentCalls[101][0]
	if !strings.Contains(body, "abcdef1234567890") {
		t.Errorf("custom body should substitute <base-sha>; got %q", body)
	}
	if !strings.Contains(body, "review at your convenience") {
		t.Errorf("custom body should preserve verbatim text; got %q", body)
	}
}

// --- Context cancellation -----------------------------------

func TestRefresh_ContextCancellationStopsBatch(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	in := fixtureRefreshInput(101, 102, 103)
	cancel() // cancel immediately

	g := newFakeGit()
	p := newFakePR()
	r, err := Refresh(ctx, in, g, p)
	if err != nil {
		t.Fatalf("Refresh should not hard-error on cancel: %v", err)
	}
	// All items recorded as failed with context reason.
	if r.Summary.FailedCount != 3 {
		t.Errorf("cancelled batch should mark all as failed; got FailedCount=%d", r.Summary.FailedCount)
	}
	for _, it := range r.Items {
		if !strings.Contains(it.ErrorMessage, "context cancelled") {
			t.Errorf("cancelled item should mention context cancellation; got %q", it.ErrorMessage)
		}
	}
}

// --- Report rendering ------------------------------------------

func TestRefresh_ReportIncludesBasePRNumberAndShortSHA(t *testing.T) {
	r, _, _ := runRefresh(t, nil)
	if !strings.Contains(r.ReportMarkdown, "#100") {
		t.Errorf("report should include Base PR number; got %q", r.ReportMarkdown)
	}
	if !strings.Contains(r.ReportMarkdown, "abcdef1") {
		t.Errorf("report should include short SHA; got %q", r.ReportMarkdown)
	}
}

func TestRefresh_ReportFallsBackToShortSHAWhenNoPRNumber(t *testing.T) {
	r, _, _ := runRefresh(t, func(in *RefreshInput, _ *fakeGit, _ *fakePR) {
		in.BaseMergedPRNumber = 0
	})
	if !strings.Contains(r.ReportMarkdown, "Base merged at") {
		t.Errorf("report should fall back to merge SHA header when no PR number; got %q", r.ReportMarkdown)
	}
}

// --- shortSHA helper -------------------------------------------

func TestShortSHA_TruncatesAndPassesShortInputs(t *testing.T) {
	if got := shortSHA("abcdef1234"); got != "abcdef1" {
		t.Errorf("shortSHA('abcdef1234') = %q, want 'abcdef1'", got)
	}
	if got := shortSHA("abc"); got != "abc" {
		t.Errorf("shortSHA('abc') = %q, want 'abc' (passthrough for short inputs)", got)
	}
	if got := shortSHA(""); got != "" {
		t.Errorf("shortSHA('') = %q, want ''", got)
	}
}
