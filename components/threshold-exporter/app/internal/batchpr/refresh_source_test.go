package batchpr

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
)

// fixtureRefreshSourceInput builds an input with N targets, each
// carrying 2 files + 1 source rule ID for the test name.
func fixtureRefreshSourceInput(targetNums ...int) RefreshSourceInput {
	targets := make([]RefreshSourceTarget, 0, len(targetNums))
	for _, n := range targetNums {
		targets = append(targets, RefreshSourceTarget{
			PRNumber:   n,
			BranchName: branchFor(n),
			Files: map[string][]byte{
				fmt.Sprintf("conf.d/foo/tenant-%d.yaml", n): []byte("bytes-a"),
				fmt.Sprintf("conf.d/foo/_defaults.yaml"):    []byte("bytes-b"),
			},
			SourceRuleIDs: []string{fmt.Sprintf("rules.yaml#groups[0].rules[%d]", n)},
		})
	}
	return RefreshSourceInput{
		Repo:    Repo{Owner: "o", Name: "r", BaseBranch: "main"},
		Targets: targets,
	}
}

func runRefreshSource(t *testing.T, mutate func(in *RefreshSourceInput, g *fakeGit, p *fakePR)) (*RefreshSourceResult, *fakeGit, *fakePR) {
	t.Helper()
	in := fixtureRefreshSourceInput(101, 102)
	g := newFakeGit()
	p := newFakePR()
	if mutate != nil {
		mutate(&in, g, p)
	}
	result, err := RefreshSource(context.Background(), in, g, p)
	if err != nil {
		t.Fatalf("RefreshSource: unexpected error: %v", err)
	}
	return result, g, p
}

// --- Validation ---------------------------------------------------

func TestRefreshSource_RejectsEmptyRepoOwner(t *testing.T) {
	in := fixtureRefreshSourceInput(101)
	in.Repo.Owner = ""
	_, err := RefreshSource(context.Background(), in, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "Repo.Owner") {
		t.Errorf("expected Repo.Owner error; got %v", err)
	}
}

func TestRefreshSource_RejectsEmptyBaseBranch(t *testing.T) {
	in := fixtureRefreshSourceInput(101)
	in.Repo.BaseBranch = ""
	_, err := RefreshSource(context.Background(), in, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "Repo.BaseBranch") {
		t.Errorf("expected Repo.BaseBranch error; got %v", err)
	}
}

func TestRefreshSource_RejectsNilClients(t *testing.T) {
	in := fixtureRefreshSourceInput(101)
	if _, err := RefreshSource(context.Background(), in, nil, newFakePR()); err == nil {
		t.Errorf("expected error for nil GitClient")
	}
	if _, err := RefreshSource(context.Background(), in, newFakeGit(), nil); err == nil {
		t.Errorf("expected error for nil PRClient")
	}
}

func TestRefreshSource_RejectsZeroTargetPRNumber(t *testing.T) {
	in := fixtureRefreshSourceInput()
	in.Targets = []RefreshSourceTarget{{PRNumber: 0, BranchName: "x"}}
	_, err := RefreshSource(context.Background(), in, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "PRNumber must be > 0") {
		t.Errorf("expected PRNumber>0 error; got %v", err)
	}
}

func TestRefreshSource_RejectsEmptyTargetBranch(t *testing.T) {
	in := fixtureRefreshSourceInput()
	in.Targets = []RefreshSourceTarget{{PRNumber: 5, BranchName: ""}}
	_, err := RefreshSource(context.Background(), in, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "BranchName is required") {
		t.Errorf("expected BranchName error; got %v", err)
	}
}

// --- Empty Targets is a no-op ------------------------------------

func TestRefreshSource_EmptyTargetsNoOp(t *testing.T) {
	in := fixtureRefreshSourceInput()
	in.Targets = nil
	r, err := RefreshSource(context.Background(), in, newFakeGit(), newFakePR())
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

// --- Happy path: all targets updated ----------------------------

func TestRefreshSource_HappyPath_AllUpdated(t *testing.T) {
	r, g, p := runRefreshSource(t, nil)

	if r.Summary.UpdatedCount != 2 {
		t.Errorf("UpdatedCount=%d, want 2", r.Summary.UpdatedCount)
	}
	// Two files per target × 2 targets = 4 total.
	if r.Summary.TotalFilesPatch != 4 {
		t.Errorf("TotalFilesPatch=%d, want 4", r.Summary.TotalFilesPatch)
	}
	// Each branch checked out + written + committed + pushed once.
	if len(g.checkoutCalls) != 2 || len(g.writeCalls) != 2 ||
		len(g.commitCalls) != 2 || len(g.pushCalls) != 2 {
		t.Errorf("expected 2 of each git op; got checkout=%d write=%d commit=%d push=%d",
			len(g.checkoutCalls), len(g.writeCalls), len(g.commitCalls), len(g.pushCalls))
	}
	// One success comment per PR, mentioning the source rule ID.
	for _, num := range []int{101, 102} {
		if len(p.commentCalls[num]) != 1 {
			t.Errorf("PR #%d should have 1 comment; got %d", num, len(p.commentCalls[num]))
			continue
		}
		body := p.commentCalls[num][0]
		if !strings.Contains(body, "Data-layer hot-fix") {
			t.Errorf("PR #%d comment should mention hot-fix; got %q", num, body)
		}
		if !strings.Contains(body, "rules.yaml#groups[0].rules") {
			t.Errorf("PR #%d comment should reference source rule IDs; got %q", num, body)
		}
	}
	// FilesUpdated populated.
	for i, it := range r.Items {
		if it.FilesUpdated != 2 {
			t.Errorf("Items[%d].FilesUpdated=%d, want 2", i, it.FilesUpdated)
		}
	}
}

func TestRefreshSource_FileContentReachesGitClient(t *testing.T) {
	// Pin the contract: caller's Files map flows verbatim into
	// GitClient.WriteFiles. Without this guard a future refactor
	// could "helpfully" filter or transform the content.
	wantContent := []byte("the-quick-brown-fox-2026")
	in := fixtureRefreshSourceInput(101)
	in.Targets[0].Files = map[string][]byte{
		"conf.d/foo/tenant-101.yaml": wantContent,
	}
	g := newFakeGit()
	p := newFakePR()
	if _, err := RefreshSource(context.Background(), in, g, p); err != nil {
		t.Fatalf("RefreshSource: %v", err)
	}
	got, ok := g.lastWriteFiles[branchFor(101)]
	if !ok {
		t.Fatalf("WriteFiles not invoked for branch %q; calls=%v", branchFor(101), g.writeCalls)
	}
	if string(got["conf.d/foo/tenant-101.yaml"]) != string(wantContent) {
		t.Errorf("file content mismatch: got %q, want %q", got["conf.d/foo/tenant-101.yaml"], wantContent)
	}
}

// --- Empty Files → PatchSkippedNoChange -------------------------

func TestRefreshSource_EmptyFilesMarksNoChange(t *testing.T) {
	r, g, _ := runRefreshSource(t, func(in *RefreshSourceInput, _ *fakeGit, _ *fakePR) {
		in.Targets[0].Files = nil // empty diff for tenant 101
	})
	if r.Items[0].Status != PatchSkippedNoChange {
		t.Errorf("Items[0].Status=%q, want %q", r.Items[0].Status, PatchSkippedNoChange)
	}
	if r.Summary.NoChangeCount != 1 {
		t.Errorf("NoChangeCount=%d, want 1", r.Summary.NoChangeCount)
	}
	// Tenant 101 should NOT have triggered checkout/write/commit/push.
	for _, b := range g.checkoutCalls {
		if b == branchFor(101) {
			t.Errorf("no-change tenant should not have triggered checkout; got %v", g.checkoutCalls)
		}
	}
	// Tenant 102 still updated.
	if r.Items[1].Status != PatchUpdated {
		t.Errorf("Items[1].Status=%q, want %q", r.Items[1].Status, PatchUpdated)
	}
}

// --- Closed/merged PR skipped ---------------------------------

func TestRefreshSource_ClosedPRSkipped(t *testing.T) {
	r, g, p := runRefreshSource(t, func(in *RefreshSourceInput, _ *fakeGit, p *fakePR) {
		p.prDetails[101] = &PRDetails{Number: 101, State: PRStateClosed, HeadBranch: branchFor(101)}
	})
	if r.Summary.SkippedCount != 1 {
		t.Errorf("SkippedCount=%d, want 1", r.Summary.SkippedCount)
	}
	if r.Items[0].Status != PatchSkippedClosed {
		t.Errorf("Items[0].Status=%q, want %q", r.Items[0].Status, PatchSkippedClosed)
	}
	if r.Items[0].PRState != PRStateClosed {
		t.Errorf("Items[0].PRState=%q, want %q", r.Items[0].PRState, PRStateClosed)
	}
	for _, c := range g.checkoutCalls {
		if c == branchFor(101) {
			t.Errorf("closed PR #101 should not be checked out; got %q", c)
		}
	}
	if len(p.commentCalls[101]) != 0 {
		t.Errorf("closed PR #101 should not get a comment by default")
	}
}

func TestRefreshSource_MergedPRSkipped(t *testing.T) {
	r, _, _ := runRefreshSource(t, func(in *RefreshSourceInput, _ *fakeGit, p *fakePR) {
		p.prDetails[101] = &PRDetails{Number: 101, State: PRStateMerged, HeadBranch: branchFor(101)}
	})
	if r.Items[0].PRState != PRStateMerged {
		t.Errorf("merged PR should report PRStateMerged; got %q", r.Items[0].PRState)
	}
	if r.Items[0].Status != PatchSkippedClosed {
		t.Errorf("merged PR should map to PatchSkippedClosed; got %q", r.Items[0].Status)
	}
}

func TestRefreshSource_PostCommentOnSkippedFlagPostsComment(t *testing.T) {
	r, _, p := runRefreshSource(t, func(in *RefreshSourceInput, _ *fakeGit, p *fakePR) {
		in.PostCommentOnSkipped = true
		p.prDetails[101] = &PRDetails{Number: 101, State: PRStateClosed, HeadBranch: branchFor(101)}
	})
	if r.Summary.SkippedCount != 1 {
		t.Fatalf("SkippedCount=%d, want 1", r.Summary.SkippedCount)
	}
	if len(p.commentCalls[101]) == 0 {
		t.Errorf("PostCommentOnSkipped=true should comment on closed PR")
	}
	if !strings.Contains(p.commentCalls[101][0], "skipped") {
		t.Errorf("skip-comment should mention 'skipped'; got %q", p.commentCalls[101][0])
	}
}

// --- Dry-run ----------------------------------------------------

func TestRefreshSource_DryRunDoesNoRemoteWork(t *testing.T) {
	r, g, p := runRefreshSource(t, func(in *RefreshSourceInput, _ *fakeGit, _ *fakePR) {
		in.DryRun = true
	})
	if r.Summary.DryRunCount != 2 {
		t.Errorf("DryRunCount=%d, want 2", r.Summary.DryRunCount)
	}
	if len(g.checkoutCalls) != 0 || len(g.writeCalls) != 0 ||
		len(g.commitCalls) != 0 || len(g.pushCalls) != 0 {
		t.Errorf("DryRun should not perform any git ops; got checkout=%d write=%d commit=%d push=%d",
			len(g.checkoutCalls), len(g.writeCalls), len(g.commitCalls), len(g.pushCalls))
	}
	if len(p.commentCalls) != 0 {
		t.Errorf("DryRun should not post comments")
	}
	if !strings.Contains(r.ReportMarkdown, "dry-run") {
		t.Errorf("report should mark dry-run; got %q", r.ReportMarkdown)
	}
	// FilesUpdated populated even in DryRun (so the report shows
	// "would update N files").
	for i, it := range r.Items {
		if it.FilesUpdated != 2 {
			t.Errorf("Items[%d].FilesUpdated=%d in DryRun, want 2", i, it.FilesUpdated)
		}
	}
}

// --- Failure paths ---------------------------------------------

func TestRefreshSource_GetPRFailureRecordsStep(t *testing.T) {
	r, _, _ := runRefreshSource(t, func(in *RefreshSourceInput, _ *fakeGit, p *fakePR) {
		p.getPRErr[101] = errors.New("API rate limited")
	})
	if r.Items[0].Status != PatchFailed {
		t.Errorf("Items[0].Status=%q, want %q", r.Items[0].Status, PatchFailed)
	}
	if r.Items[0].Step != "get_pr" {
		t.Errorf("Items[0].Step=%q, want 'get_pr'", r.Items[0].Step)
	}
	if !strings.Contains(r.Items[0].ErrorMessage, "rate limited") {
		t.Errorf("ErrorMessage should include underlying cause; got %q", r.Items[0].ErrorMessage)
	}
}

func TestRefreshSource_CheckoutFailureRecordsStep(t *testing.T) {
	r, _, _ := runRefreshSource(t, func(in *RefreshSourceInput, g *fakeGit, _ *fakePR) {
		g.checkoutErr[branchFor(101)] = errors.New("branch not found")
	})
	if r.Items[0].Step != "checkout" {
		t.Errorf("Items[0].Step=%q, want 'checkout'", r.Items[0].Step)
	}
}

func TestRefreshSource_WriteFailureRecordsStep(t *testing.T) {
	r, _, _ := runRefreshSource(t, func(in *RefreshSourceInput, g *fakeGit, _ *fakePR) {
		g.writeErr[branchFor(101)] = errors.New("disk full")
	})
	if r.Items[0].Step != "write" {
		t.Errorf("Items[0].Step=%q, want 'write'", r.Items[0].Step)
	}
}

func TestRefreshSource_CommitFailureRecordsStep(t *testing.T) {
	r, _, _ := runRefreshSource(t, func(in *RefreshSourceInput, g *fakeGit, _ *fakePR) {
		g.commitErr[branchFor(101)] = errors.New("nothing to commit")
	})
	if r.Items[0].Step != "commit" {
		t.Errorf("Items[0].Step=%q, want 'commit'", r.Items[0].Step)
	}
}

func TestRefreshSource_PushFailureRecordsStep(t *testing.T) {
	r, _, _ := runRefreshSource(t, func(in *RefreshSourceInput, g *fakeGit, _ *fakePR) {
		g.pushErr[branchFor(101)] = errors.New("remote rejected")
	})
	if r.Items[0].Step != "push" {
		t.Errorf("Items[0].Step=%q, want 'push'", r.Items[0].Step)
	}
}

func TestRefreshSource_CommentFailureRecordsStep(t *testing.T) {
	r, _, _ := runRefreshSource(t, func(in *RefreshSourceInput, _ *fakeGit, p *fakePR) {
		p.commentErr[101] = errors.New("API timeout")
	})
	if r.Items[0].Step != "comment" {
		t.Errorf("Items[0].Step=%q, want 'comment'", r.Items[0].Step)
	}
	// Hint that the underlying refresh succeeded.
	if !strings.Contains(r.Items[0].ErrorMessage, "commit + push DID succeed") {
		t.Errorf("comment failure should clarify commit + push succeeded; got %q", r.Items[0].ErrorMessage)
	}
	// FilesUpdated should be populated even on comment failure
	// (the patch did land on the branch).
	if r.Items[0].FilesUpdated != 2 {
		t.Errorf("FilesUpdated should be set on comment-only failure; got %d", r.Items[0].FilesUpdated)
	}
}

// --- Per-target loop continues after one failure ---------------

func TestRefreshSource_OneFailureDoesNotSinkBatch(t *testing.T) {
	r, _, p := runRefreshSource(t, func(in *RefreshSourceInput, g *fakeGit, _ *fakePR) {
		g.commitErr[branchFor(101)] = errors.New("hook rejected")
	})
	if r.Items[0].Status != PatchFailed {
		t.Errorf("Items[0].Status=%q, want %q", r.Items[0].Status, PatchFailed)
	}
	if r.Items[1].Status != PatchUpdated {
		t.Errorf("Items[1].Status=%q, want %q (one bad apple shouldn't sink batch)",
			r.Items[1].Status, PatchUpdated)
	}
	if len(p.commentCalls[102]) != 1 {
		t.Errorf("PR #102 should still receive a success comment; got %d",
			len(p.commentCalls[102]))
	}
}

// --- Custom CommitMessageOverride + CommentBody substitution ---

func TestRefreshSource_CustomCommitMessageSubstitutesSourceRuleIDs(t *testing.T) {
	// End-to-end assertion: the override flows through the
	// orchestration into the actual Commit call, with
	// `<source-rule-ids>` substituted. Earlier draft of this test
	// only spot-checked patchCommitMessage in isolation, missing
	// the orchestration wiring (self-review caught).
	r, g, _ := runRefreshSource(t, func(in *RefreshSourceInput, _ *fakeGit, _ *fakePR) {
		in.CommitMessageOverride = "PATCH(<source-rule-ids>): re-emit"
	})
	if r.Items[0].Status != PatchUpdated {
		t.Fatalf("Items[0].Status=%q, want %q", r.Items[0].Status, PatchUpdated)
	}
	msgs := g.commitMessages[branchFor(101)]
	if len(msgs) != 1 {
		t.Fatalf("expected 1 commit on branch %q; got %d", branchFor(101), len(msgs))
	}
	want := "PATCH(rules.yaml#groups[0].rules[101]): re-emit"
	if msgs[0] != want {
		t.Errorf("commit message: got %q, want %q", msgs[0], want)
	}
	// Tenant 102 also sees its own substituted message.
	wantTenant2 := "PATCH(rules.yaml#groups[0].rules[102]): re-emit"
	if got := g.commitMessages[branchFor(102)][0]; got != wantTenant2 {
		t.Errorf("PR #102 commit message: got %q, want %q", got, wantTenant2)
	}
}

func TestRefreshSource_DefaultCommitMessageIncludesSourceRuleIDs(t *testing.T) {
	// Pin the default commit message format end-to-end (no override).
	_, g, _ := runRefreshSource(t, nil)
	msgs := g.commitMessages[branchFor(101)]
	if len(msgs) != 1 {
		t.Fatalf("expected 1 commit on branch %q; got %d", branchFor(101), len(msgs))
	}
	if !strings.Contains(msgs[0], "Data-layer hot-fix") {
		t.Errorf("default commit message should mention hot-fix; got %q", msgs[0])
	}
	if !strings.Contains(msgs[0], "rules.yaml#groups[0].rules[101]") {
		t.Errorf("default commit message should include source rule ID; got %q", msgs[0])
	}
}

func TestRefreshSource_CustomCommentBodySubstitutesSourceRuleIDs(t *testing.T) {
	_, _, p := runRefreshSource(t, func(in *RefreshSourceInput, _ *fakeGit, _ *fakePR) {
		in.CommentBody = "Re-review please — affected: <source-rule-ids>"
	})
	body := p.commentCalls[101][0]
	if !strings.Contains(body, "rules.yaml#groups[0].rules[101]") {
		t.Errorf("custom body should substitute <source-rule-ids>; got %q", body)
	}
	if !strings.Contains(body, "Re-review please") {
		t.Errorf("custom body should preserve verbatim text; got %q", body)
	}
}

// --- Empty SourceRuleIDs → graceful default messages ----------

func TestRefreshSource_EmptySourceRuleIDsHandledGracefully(t *testing.T) {
	_, _, p := runRefreshSource(t, func(in *RefreshSourceInput, _ *fakeGit, _ *fakePR) {
		in.Targets[0].SourceRuleIDs = nil
		in.Targets[1].SourceRuleIDs = nil
	})
	for _, num := range []int{101, 102} {
		body := p.commentCalls[num][0]
		// Should NOT contain a dangling "for source rules" with
		// nothing after it — fallback message handles empty case.
		if strings.Contains(body, "for source rules ") &&
			!strings.Contains(body, "for source rules .") {
			// Allow legitimate "for source rules X." but not the
			// dangling "for source rules " (note trailing space, no IDs).
		}
		if strings.Contains(body, "for source rules \n") ||
			strings.HasSuffix(strings.TrimSpace(body), "for source rules") {
			t.Errorf("PR #%d comment has dangling 'for source rules' with no IDs; got %q",
				num, body)
		}
		if !strings.Contains(body, "Data-layer hot-fix applied") {
			t.Errorf("PR #%d comment fallback should still mention hot-fix; got %q",
				num, body)
		}
	}
}

// --- Context cancellation -----------------------------------

func TestRefreshSource_ContextCancellationStopsBatch(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	in := fixtureRefreshSourceInput(101, 102, 103)
	cancel() // cancel immediately

	r, err := RefreshSource(ctx, in, newFakeGit(), newFakePR())
	if err != nil {
		t.Fatalf("RefreshSource should not hard-error on cancel: %v", err)
	}
	if r.Summary.FailedCount != 3 {
		t.Errorf("cancelled batch should mark all failed; got FailedCount=%d", r.Summary.FailedCount)
	}
	for _, it := range r.Items {
		if !strings.Contains(it.ErrorMessage, "context cancelled") {
			t.Errorf("cancelled item should mention context cancellation; got %q", it.ErrorMessage)
		}
	}
}

// --- Report rendering ------------------------------------------

func TestRefreshSource_ReportIncludesPerPRTableAndCrossRef(t *testing.T) {
	r, _, _ := runRefreshSource(t, nil)
	if !strings.Contains(r.ReportMarkdown, "## Per-PR outcomes") {
		t.Errorf("report should have Per-PR outcomes section")
	}
	if !strings.Contains(r.ReportMarkdown, "## Source-rules → tenant PRs") {
		t.Errorf("report should have Source-rules → tenant PRs section")
	}
	// The cross-ref should mention both PR numbers.
	if !strings.Contains(r.ReportMarkdown, "#101") || !strings.Contains(r.ReportMarkdown, "#102") {
		t.Errorf("report should reference both PRs; got %q", r.ReportMarkdown)
	}
	// Header should report 2 unique source rules.
	if !strings.Contains(r.ReportMarkdown, "2 unique rule ID(s)") {
		t.Errorf("report should count unique rule IDs; got %q", r.ReportMarkdown)
	}
}

func TestRefreshSource_ReportFiltersOnSharedRuleAcrossTargets(t *testing.T) {
	// Two targets share one source rule + each have one unique ID.
	in := fixtureRefreshSourceInput(101, 102)
	in.Targets[0].SourceRuleIDs = []string{"shared-rule", "unique-101"}
	in.Targets[1].SourceRuleIDs = []string{"shared-rule", "unique-102"}
	r, err := RefreshSource(context.Background(), in, newFakeGit(), newFakePR())
	if err != nil {
		t.Fatalf("RefreshSource: %v", err)
	}
	report := r.ReportMarkdown
	// Should have 3 unique rule IDs.
	if !strings.Contains(report, "3 unique rule ID(s)") {
		t.Errorf("expected 3 unique rule IDs; got report %q", report)
	}
	// The shared rule should map to both PRs.
	if !strings.Contains(report, "`shared-rule` → #101, #102") {
		t.Errorf("shared rule should map to both PRs; got %q", report)
	}
}

// --- joinSourceRuleIDs helper -----------------------------------

func TestJoinSourceRuleIDs_TruncatesLongLists(t *testing.T) {
	// Empty.
	if got := joinSourceRuleIDs(nil); got != "" {
		t.Errorf("empty: got %q, want ''", got)
	}
	// Short.
	if got := joinSourceRuleIDs([]string{"a", "b"}); got != "a, b" {
		t.Errorf("short: got %q, want 'a, b'", got)
	}
	// Long: 30 IDs each ~12 chars → ~360 chars; should truncate.
	long := make([]string, 30)
	for i := range long {
		long[i] = fmt.Sprintf("rule-id-%03d", i)
	}
	got := joinSourceRuleIDs(long)
	if !strings.Contains(got, "+ ") || !strings.Contains(got, "more") {
		t.Errorf("long list should produce '+ N more' tail; got %q", got)
	}
	if len(got) > 200 {
		t.Errorf("long list should cap render length; got %d chars", len(got))
	}
}

func TestJoinSourceRuleIDs_SingleVeryLongID(t *testing.T) {
	// Single ID exceeds the cap. Helper should still return it
	// untruncated (better to surface the full ID than to chop the
	// only piece of context the reviewer has).
	id := strings.Repeat("X", 200)
	got := joinSourceRuleIDs([]string{id, "extra"})
	if !strings.HasPrefix(got, id) {
		t.Errorf("very long single ID should be returned untruncated; got %q", got)
	}
	if !strings.Contains(got, "+ 1 more") {
		t.Errorf("should report the truncated tail; got %q", got)
	}
}

func TestJoinSourceRuleIDs_OnlyOneIDExceedingCap_NoBogusTail(t *testing.T) {
	// Self-review caught: when there's exactly ONE ID and it
	// exceeds cap, the original code returned "X ... + 0 more"
	// — the "+ 0 more" tail is meaningless. Fix: return just
	// the ID without the tail when there's nothing else to count.
	id := strings.Repeat("X", 200)
	got := joinSourceRuleIDs([]string{id})
	if got != id {
		t.Errorf("single very-long ID should return verbatim (no tail); got %q", got)
	}
	if strings.Contains(got, "+ 0 more") {
		t.Errorf("should not produce '+ 0 more' tail when there are no additional IDs; got %q", got)
	}
}
