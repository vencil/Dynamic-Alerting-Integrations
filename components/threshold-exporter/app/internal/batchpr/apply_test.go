package batchpr

// PR-2 — Apply() orchestration tests. In-memory stubs for
// GitClient / PRClient let us exercise every status path without
// touching disk or the network.
//
// Coverage axes:
//   - Happy path: create base PR + N tenant PRs + <base> rewrite.
//   - Idempotency: branch already exists on remote → SkippedExisting.
//   - DryRun: no client side-effects; statuses all DryRun.
//   - EmptyFiles: ItemFiles[i] missing/empty → EmptyFiles status.
//   - Per-step failure: failures at each pipeline step → Failed
//     status with descriptive ErrorMessage; subsequent items still
//     processed.
//   - Input validation: nil Plan / nil clients / missing Repo
//     fields → hard error before any item work.
//   - Context cancellation: cancelled mid-loop → remaining items
//     marked Failed with cancellation reason.
//   - <base> placeholder: rewrite happens iff base PR opened AND
//     tenant body still contains the literal placeholder.

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"testing"
	"time"
)

// --- in-memory stubs ---

// fakeGit records calls and provides scripted responses.
type fakeGit struct {
	mu              sync.Mutex
	createCalls     []string
	writeCalls      []string
	commitCalls     []string
	pushCalls       []string
	branchExists    map[string]bool              // branch → exists?
	branchExistsErr error                        // injected to fail the check
	createErr       map[string]error             // branch → fail CreateBranch
	writeErr        map[string]error             // branch → fail WriteFiles
	commitErr       map[string]error             // branch → fail Commit
	pushErr         map[string]error             // branch → fail Push
	lastWriteFiles  map[string]map[string][]byte // branch → files

	// PR-3 — Refresh-related fakes.
	rebaseCalls       []string                 // formatted "branch:oldBase->newBase"
	rebaseOutcomes    map[string]*RebaseOutcome // branch → outcome to return
	rebaseErr         map[string]error          // branch → fail RebaseOnto
	forcePushCalls    []string                 // branch
	forcePushErr      map[string]error          // branch → fail ForcePushWithLease

	// PR-4 — RefreshSource-related fakes.
	checkoutCalls   []string         // branch
	checkoutErr     map[string]error // branch → fail CheckoutBranch
	commitMessages  map[string][]string // branch → commit messages received (PR-4 self-review)
}

func newFakeGit() *fakeGit {
	return &fakeGit{
		branchExists:   map[string]bool{},
		createErr:      map[string]error{},
		writeErr:       map[string]error{},
		commitErr:      map[string]error{},
		pushErr:        map[string]error{},
		lastWriteFiles: map[string]map[string][]byte{},
		rebaseOutcomes: map[string]*RebaseOutcome{},
		rebaseErr:      map[string]error{},
		forcePushErr:   map[string]error{},
		checkoutErr:    map[string]error{},
		commitMessages: map[string][]string{},
	}
}

func (g *fakeGit) CreateBranch(ctx context.Context, name, base string) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.createCalls = append(g.createCalls, fmt.Sprintf("%s<-%s", name, base))
	return g.createErr[name]
}

func (g *fakeGit) WriteFiles(ctx context.Context, branch string, files map[string][]byte) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.writeCalls = append(g.writeCalls, branch)
	if err := g.writeErr[branch]; err != nil {
		return err
	}
	g.lastWriteFiles[branch] = files
	return nil
}

func (g *fakeGit) Commit(ctx context.Context, branch, message, author string) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.commitCalls = append(g.commitCalls, branch)
	g.commitMessages[branch] = append(g.commitMessages[branch], message)
	return g.commitErr[branch]
}

func (g *fakeGit) Push(ctx context.Context, branch string) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.pushCalls = append(g.pushCalls, branch)
	return g.pushErr[branch]
}

func (g *fakeGit) BranchExistsRemote(ctx context.Context, branch string) (bool, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	if g.branchExistsErr != nil {
		return false, g.branchExistsErr
	}
	return g.branchExists[branch], nil
}

func (g *fakeGit) CheckoutBranch(ctx context.Context, branch string) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.checkoutCalls = append(g.checkoutCalls, branch)
	return g.checkoutErr[branch]
}

func (g *fakeGit) RebaseOnto(ctx context.Context, branch, oldBase, newBase string) (*RebaseOutcome, error) {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.rebaseCalls = append(g.rebaseCalls, fmt.Sprintf("%s:%s->%s", branch, oldBase, newBase))
	if err := g.rebaseErr[branch]; err != nil {
		return nil, err
	}
	if outcome, ok := g.rebaseOutcomes[branch]; ok {
		return outcome, nil
	}
	// Default: clean rebase, no conflicts, not already-up-to-date.
	return &RebaseOutcome{}, nil
}

func (g *fakeGit) ForcePushWithLease(ctx context.Context, branch string) error {
	g.mu.Lock()
	defer g.mu.Unlock()
	g.forcePushCalls = append(g.forcePushCalls, branch)
	return g.forcePushErr[branch]
}

// fakePR records OpenPR + UpdatePRDescription invocations and
// hands out incrementing PR numbers (starting at 100 so they're
// distinguishable from PlanItem indices in test output).
type fakePR struct {
	mu                   sync.Mutex
	nextNum              int
	openCalls            []OpenPRInput
	updates              map[int]string
	openErr              map[string]error // head branch → fail OpenPR
	openErrAll           error
	findByBranchExisting map[string]*PROpened // branch → existing PR (for idempotency)
	updateErr            map[int]error

	// PR-3 — Refresh-related fakes.
	prDetails    map[int]*PRDetails // num → details to return from GetPR
	getPRErr     map[int]error      // num → fail GetPR
	commentCalls map[int][]string   // num → posted bodies
	commentErr   map[int]error      // num → fail CommentPR
}

func newFakePR() *fakePR {
	return &fakePR{
		nextNum:              100,
		updates:              map[int]string{},
		openErr:              map[string]error{},
		findByBranchExisting: map[string]*PROpened{},
		updateErr:            map[int]error{},
		prDetails:            map[int]*PRDetails{},
		getPRErr:             map[int]error{},
		commentCalls:         map[int][]string{},
		commentErr:           map[int]error{},
	}
}

func (p *fakePR) OpenPR(ctx context.Context, in OpenPRInput) (*PROpened, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.openCalls = append(p.openCalls, in)
	if p.openErrAll != nil {
		return nil, p.openErrAll
	}
	if err := p.openErr[in.Head]; err != nil {
		return nil, err
	}
	num := p.nextNum
	p.nextNum++
	return &PROpened{Number: num, URL: fmt.Sprintf("https://github.com/o/r/pull/%d", num)}, nil
}

func (p *fakePR) FindPRByBranch(ctx context.Context, branch string) (*PROpened, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.findByBranchExisting[branch], nil
}

func (p *fakePR) UpdatePRDescription(ctx context.Context, num int, body string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if err := p.updateErr[num]; err != nil {
		return err
	}
	p.updates[num] = body
	return nil
}

func (p *fakePR) GetPR(ctx context.Context, num int) (*PRDetails, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if err := p.getPRErr[num]; err != nil {
		return nil, err
	}
	if d, ok := p.prDetails[num]; ok {
		return d, nil
	}
	// Default: open PR. The test sets prDetails explicitly when
	// it needs closed/merged or a specific HeadBranch.
	return &PRDetails{
		Number:     num,
		State:      PRStateOpen,
		HeadBranch: fmt.Sprintf("default-branch-%d", num),
		URL:        fmt.Sprintf("https://github.com/o/r/pull/%d", num),
	}, nil
}

func (p *fakePR) CommentPR(ctx context.Context, num int, body string) error {
	p.mu.Lock()
	defer p.mu.Unlock()
	if err := p.commentErr[num]; err != nil {
		return err
	}
	p.commentCalls[num] = append(p.commentCalls[num], body)
	return nil
}

// --- fixture helpers ---

// fixtureBasePlusTwoTenants returns a Plan with 1 base PR + 2
// tenant chunk PRs. Tenant PR descriptions reference `<base>`.
func fixtureBasePlusTwoTenants() *Plan {
	return &Plan{
		Items: []PlanItem{
			{
				Kind:                  PlanItemBase,
				Title:                 "[Base Infrastructure] Import 2 profiles",
				Description:           "Base body",
				SourceProposalIndices: []int{0, 1},
			},
			{
				Kind:                  PlanItemTenant,
				Title:                 "[chunk 1/2] Import to db",
				Description:           "Tenant body 1\n\nBlocked by: <base>",
				BlockedBy:             "0",
				SourceProposalIndices: []int{0},
				TenantIDs:             []string{"tenant-a", "tenant-b"},
				ChunkKey:              "db",
			},
			{
				Kind:                  PlanItemTenant,
				Title:                 "[chunk 2/2] Import to web",
				Description:           "Tenant body 2\n\nBlocked by: <base>",
				BlockedBy:             "0",
				SourceProposalIndices: []int{1},
				TenantIDs:             []string{"tenant-c"},
				ChunkKey:              "web",
			},
		},
	}
}

// fixtureItemFiles maps each Plan.Items[i] index to a small file map.
func fixtureItemFiles() map[int]map[string][]byte {
	return map[int]map[string][]byte{
		0: {"db/_defaults.yaml": []byte("defaults: {cpu: 80}\n")},
		1: {"db/tenant-a.yaml": []byte("tenants:\n  tenant-a: {cpu: \"95\"}\n")},
		2: {"web/tenant-c.yaml": []byte("tenants:\n  tenant-c: {cpu: \"99\"}\n")},
	}
}

func fixtureRepo() Repo {
	return Repo{Owner: "o", Name: "r", BaseBranch: "main"}
}

// runApply is a small wrapper: builds a default ApplyInput and
// stubs from the supplied plan + ItemFiles + per-test mutators.
func runApply(t *testing.T, mutate func(in *ApplyInput, g *fakeGit, p *fakePR)) (*ApplyResult, *fakeGit, *fakePR) {
	t.Helper()
	plan := fixtureBasePlusTwoTenants()
	files := fixtureItemFiles()
	g := newFakeGit()
	p := newFakePR()
	in := ApplyInput{
		Plan:      plan,
		ItemFiles: files,
		Repo:      fixtureRepo(),
	}
	if mutate != nil {
		mutate(&in, g, p)
	}
	res, err := Apply(context.Background(), in, g, p)
	if err != nil {
		t.Fatalf("Apply: %v", err)
	}
	return res, g, p
}

// --- input validation ---

func TestApply_NilPlan(t *testing.T) {
	_, err := Apply(context.Background(), ApplyInput{Repo: fixtureRepo()}, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "Plan is nil") {
		t.Errorf("err = %v, want Plan-is-nil", err)
	}
}

func TestApply_EmptyPlan(t *testing.T) {
	_, err := Apply(context.Background(), ApplyInput{
		Plan: &Plan{},
		Repo: fixtureRepo(),
	}, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "zero items") {
		t.Errorf("err = %v, want zero-items", err)
	}
}

func TestApply_MissingRepo(t *testing.T) {
	_, err := Apply(context.Background(), ApplyInput{
		Plan: fixtureBasePlusTwoTenants(),
		Repo: Repo{},
	}, newFakeGit(), newFakePR())
	if err == nil || !strings.Contains(err.Error(), "Repo.Owner") {
		t.Errorf("err = %v, want missing-Repo", err)
	}
}

func TestApply_NilGitClient(t *testing.T) {
	_, err := Apply(context.Background(), ApplyInput{
		Plan: fixtureBasePlusTwoTenants(),
		Repo: fixtureRepo(),
	}, nil, newFakePR())
	if err == nil || !strings.Contains(err.Error(), "GitClient is nil") {
		t.Errorf("err = %v", err)
	}
}

func TestApply_NilPRClient(t *testing.T) {
	_, err := Apply(context.Background(), ApplyInput{
		Plan: fixtureBasePlusTwoTenants(),
		Repo: fixtureRepo(),
	}, newFakeGit(), nil)
	if err == nil || !strings.Contains(err.Error(), "PRClient is nil") {
		t.Errorf("err = %v", err)
	}
}

// --- happy path: 1 base + 2 tenant PRs, all created, base placeholder rewritten ---

func TestApply_HappyPath_AllCreated_BasePlaceholderRewritten(t *testing.T) {
	res, g, p := runApply(t, nil)

	if len(res.Items) != 3 {
		t.Fatalf("Items len = %d, want 3", len(res.Items))
	}
	for i, ir := range res.Items {
		if ir.Status != ApplyStatusCreated {
			t.Errorf("item %d: status = %q, want created", i, ir.Status)
		}
		if ir.PRNumber == 0 {
			t.Errorf("item %d: PRNumber not set", i)
		}
	}

	if res.Summary.CreatedCount != 3 {
		t.Errorf("CreatedCount = %d, want 3", res.Summary.CreatedCount)
	}
	if res.Summary.BasePlaceholderRewrites != 2 {
		t.Errorf("BasePlaceholderRewrites = %d, want 2", res.Summary.BasePlaceholderRewrites)
	}
	if res.BasePRNumber != res.Items[0].PRNumber {
		t.Errorf("BasePRNumber = %d, want %d", res.BasePRNumber, res.Items[0].PRNumber)
	}

	// Each tenant PR should have its description updated with the
	// base PR number substituted in.
	wantSub := fmt.Sprintf("Blocked by: #%d", res.BasePRNumber)
	for _, idx := range []int{1, 2} {
		body, ok := p.updates[res.Items[idx].PRNumber]
		if !ok {
			t.Errorf("tenant PR %d: no UpdatePRDescription call", res.Items[idx].PRNumber)
			continue
		}
		if !strings.Contains(body, wantSub) {
			t.Errorf("tenant PR body = %q, missing substituted %q", body, wantSub)
		}
		if strings.Contains(body, "<base>") {
			t.Errorf("tenant PR body still has literal <base>: %q", body)
		}
	}

	// All 3 items should have run through the full pipeline.
	if got := len(g.createCalls); got != 3 {
		t.Errorf("CreateBranch calls = %d, want 3", got)
	}
	if got := len(g.writeCalls); got != 3 {
		t.Errorf("WriteFiles calls = %d, want 3", got)
	}
	if got := len(g.commitCalls); got != 3 {
		t.Errorf("Commit calls = %d, want 3", got)
	}
	if got := len(g.pushCalls); got != 3 {
		t.Errorf("Push calls = %d, want 3", got)
	}
}

// --- DryRun: no side effects; all items DryRun status ---

func TestApply_DryRun_NoSideEffects(t *testing.T) {
	res, g, p := runApply(t, func(in *ApplyInput, _ *fakeGit, _ *fakePR) {
		in.DryRun = true
	})
	for i, ir := range res.Items {
		if ir.Status != ApplyStatusDryRun {
			t.Errorf("item %d: status = %q, want dry_run", i, ir.Status)
		}
	}
	if len(g.createCalls)+len(g.writeCalls)+len(g.commitCalls)+len(g.pushCalls) != 0 {
		t.Errorf("dry-run made git calls: c=%d w=%d co=%d p=%d",
			len(g.createCalls), len(g.writeCalls), len(g.commitCalls), len(g.pushCalls))
	}
	if len(p.openCalls) != 0 {
		t.Errorf("dry-run opened PRs: %d", len(p.openCalls))
	}
	if res.Summary.DryRunCount != 3 {
		t.Errorf("DryRunCount = %d, want 3", res.Summary.DryRunCount)
	}
}

// --- Idempotency: branch already on remote → SkippedExisting ---

func TestApply_IdempotentSkipsExistingBranch(t *testing.T) {
	res, g, p := runApply(t, func(in *ApplyInput, fg *fakeGit, fp *fakePR) {
		// Pre-populate fake remote with the deterministic base
		// branch name + an "existing" open PR.
		hash := computePlanHash(in.Plan)
		baseBranch := fmt.Sprintf("%s/base-%s", defaultBranchPrefix, hash)
		fg.branchExists[baseBranch] = true
		fp.findByBranchExisting[baseBranch] = &PROpened{Number: 42, URL: "https://github.com/o/r/pull/42"}
	})
	if res.Items[0].Status != ApplyStatusSkippedExisting {
		t.Errorf("base item: status = %q, want skipped_existing", res.Items[0].Status)
	}
	if res.Items[0].PRNumber != 42 {
		t.Errorf("base item PRNumber = %d, want 42 (existing)", res.Items[0].PRNumber)
	}
	if res.BasePRNumber != 42 {
		t.Errorf("BasePRNumber = %d, want 42", res.BasePRNumber)
	}
	// No git create / push calls for the skipped item.
	for _, c := range g.createCalls {
		if strings.Contains(c, "/base-") {
			t.Errorf("base branch should NOT have been created; got %q", c)
		}
	}
	// Tenant items still should have created (only the base
	// branch was pre-populated).
	if got := res.Summary.CreatedCount; got != 2 {
		t.Errorf("CreatedCount = %d, want 2 (tenants)", got)
	}
	// Placeholder rewrite should still apply since base PR # 42
	// is known.
	if res.Summary.BasePlaceholderRewrites != 2 {
		t.Errorf("BasePlaceholderRewrites = %d, want 2", res.Summary.BasePlaceholderRewrites)
	}
	// Sanity: no openPR call against the base head branch.
	for _, oc := range p.openCalls {
		if strings.Contains(oc.Head, "/base-") {
			t.Errorf("base PR should NOT have been opened; got %+v", oc)
		}
	}
}

// --- Empty files: ItemFiles[i] missing → EmptyFiles status ---

func TestApply_EmptyFilesSkipsItem(t *testing.T) {
	res, _, p := runApply(t, func(in *ApplyInput, _ *fakeGit, _ *fakePR) {
		delete(in.ItemFiles, 1) // Plan.Items[1] has no files.
	})
	if res.Items[1].Status != ApplyStatusEmptyFiles {
		t.Errorf("item 1: status = %q, want empty_files", res.Items[1].Status)
	}
	// No PR opened for item 1.
	for _, oc := range p.openCalls {
		if strings.Contains(oc.Head, "tenant-db-") {
			t.Errorf("item 1 should not have opened PR; got %+v", oc)
		}
	}
	// Other items unaffected.
	if res.Items[0].Status != ApplyStatusCreated || res.Items[2].Status != ApplyStatusCreated {
		t.Errorf("expected items 0 + 2 created; got %v", res.Items)
	}
}

// --- Failure isolation: one tenant fails, others continue ---

func TestApply_FailureIsolation_OneTenantFails(t *testing.T) {
	res, _, _ := runApply(t, func(in *ApplyInput, fg *fakeGit, _ *fakePR) {
		hash := computePlanHash(in.Plan)
		failBranch := fmt.Sprintf("%s/tenant-db-%s", defaultBranchPrefix, hash)
		fg.pushErr[failBranch] = errors.New("simulated push failure")
	})
	if res.Items[1].Status != ApplyStatusFailed {
		t.Errorf("item 1: status = %q, want failed", res.Items[1].Status)
	}
	if !strings.Contains(res.Items[1].ErrorMessage, "push") {
		t.Errorf("item 1 ErrorMessage = %q, want 'push' mention", res.Items[1].ErrorMessage)
	}
	if res.Items[0].Status != ApplyStatusCreated {
		t.Errorf("item 0 (base) should still be created; got %q", res.Items[0].Status)
	}
	if res.Items[2].Status != ApplyStatusCreated {
		t.Errorf("item 2 should still be created; got %q", res.Items[2].Status)
	}
	if res.Summary.FailedCount != 1 || res.Summary.CreatedCount != 2 {
		t.Errorf("Summary = %+v, want 1 failed + 2 created", res.Summary)
	}
}

// --- Per-step failure: each pipeline step ---

func TestApply_PerStepFailureMessages(t *testing.T) {
	type setup struct {
		name        string
		injectErr   func(g *fakeGit, p *fakePR, branch string)
		wantInError string
	}
	cases := []setup{
		{"branch-exists-check", func(g *fakeGit, _ *fakePR, _ string) {
			g.branchExistsErr = errors.New("ls-remote boom")
		}, "branch existence"},
		{"create-branch", func(g *fakeGit, _ *fakePR, b string) {
			g.createErr[b] = errors.New("checkout boom")
		}, "create branch"},
		{"write-files", func(g *fakeGit, _ *fakePR, b string) {
			g.writeErr[b] = errors.New("write boom")
		}, "write files"},
		{"commit", func(g *fakeGit, _ *fakePR, b string) {
			g.commitErr[b] = errors.New("commit boom")
		}, "commit"},
		{"push", func(g *fakeGit, _ *fakePR, b string) {
			g.pushErr[b] = errors.New("push boom")
		}, "push"},
		{"open-pr", func(_ *fakeGit, p *fakePR, b string) {
			p.openErr[b] = errors.New("open boom")
		}, "open PR"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			plan := fixtureBasePlusTwoTenants()
			files := fixtureItemFiles()
			g := newFakeGit()
			p := newFakePR()
			hash := computePlanHash(plan)
			baseBranch := fmt.Sprintf("%s/base-%s", defaultBranchPrefix, hash)
			c.injectErr(g, p, baseBranch)
			res, err := Apply(context.Background(), ApplyInput{
				Plan: plan, ItemFiles: files, Repo: fixtureRepo(),
			}, g, p)
			if err != nil {
				t.Fatalf("Apply: %v", err)
			}
			if res.Items[0].Status != ApplyStatusFailed {
				t.Errorf("base item: status = %q, want failed", res.Items[0].Status)
			}
			if !strings.Contains(res.Items[0].ErrorMessage, c.wantInError) {
				t.Errorf("ErrorMessage = %q, want %q substring", res.Items[0].ErrorMessage, c.wantInError)
			}
		})
	}
}

// --- <base> placeholder NOT rewritten when base PR fails ---

func TestApply_BasePlaceholderNotRewrittenWhenBaseFails(t *testing.T) {
	res, _, _ := runApply(t, func(in *ApplyInput, fg *fakeGit, _ *fakePR) {
		hash := computePlanHash(in.Plan)
		baseBranch := fmt.Sprintf("%s/base-%s", defaultBranchPrefix, hash)
		fg.pushErr[baseBranch] = errors.New("base push failed")
	})
	if res.BasePRNumber != 0 {
		t.Errorf("BasePRNumber = %d, want 0 (base failed)", res.BasePRNumber)
	}
	if res.Summary.BasePlaceholderRewrites != 0 {
		t.Errorf("BasePlaceholderRewrites = %d, want 0", res.Summary.BasePlaceholderRewrites)
	}
	hasWarning := false
	for _, w := range res.Warnings {
		if strings.Contains(w, "literal `<base>` placeholder") {
			hasWarning = true
		}
	}
	if !hasWarning {
		t.Errorf("expected literal-<base> warning; got %v", res.Warnings)
	}
}

// --- Custom BranchPrefix is honoured ---

func TestApply_CustomBranchPrefix(t *testing.T) {
	res, g, _ := runApply(t, func(in *ApplyInput, _ *fakeGit, _ *fakePR) {
		in.BranchPrefix = "customer-fork/migration"
	})
	if res.Items[0].Status != ApplyStatusCreated {
		t.Fatalf("base status = %q", res.Items[0].Status)
	}
	if !strings.HasPrefix(res.Items[0].BranchName, "customer-fork/migration/base-") {
		t.Errorf("BranchName = %q, want custom-prefix", res.Items[0].BranchName)
	}
	for _, c := range g.createCalls {
		if !strings.HasPrefix(c, "customer-fork/migration/") {
			t.Errorf("CreateBranch arg %q does not honour BranchPrefix", c)
		}
	}
}

// --- Context cancellation mid-loop ---

func TestApply_ContextCancelledDuringLoop(t *testing.T) {
	plan := fixtureBasePlusTwoTenants()
	files := fixtureItemFiles()
	g := newFakeGit()
	p := newFakePR()
	ctx, cancel := context.WithCancel(context.Background())

	// Cancel after the FIRST OpenPR succeeds — items 1 and 2
	// should be marked failed-by-cancel without further work.
	originalNext := p.nextNum
	p.nextNum = originalNext // no change, just marking intent
	cancelled := false
	wrappedPR := &cancelOnFirstOpenPR{wrap: p, cancel: cancel, did: &cancelled}

	res, err := Apply(ctx, ApplyInput{
		Plan: plan, ItemFiles: files, Repo: fixtureRepo(),
	}, g, wrappedPR)
	if err != nil {
		t.Fatalf("Apply: %v", err)
	}
	if res.Items[0].Status != ApplyStatusCreated {
		t.Errorf("item 0: %q, want created (cancellation should fire AFTER first OpenPR)", res.Items[0].Status)
	}
	for i := 1; i < 3; i++ {
		if res.Items[i].Status != ApplyStatusFailed {
			t.Errorf("item %d: status = %q, want failed (cancellation)", i, res.Items[i].Status)
		}
		if !strings.Contains(res.Items[i].ErrorMessage, "context cancelled") {
			t.Errorf("item %d: ErrorMessage = %q", i, res.Items[i].ErrorMessage)
		}
	}
}

type cancelOnFirstOpenPR struct {
	wrap   PRClient
	cancel context.CancelFunc
	did    *bool
}

func (c *cancelOnFirstOpenPR) OpenPR(ctx context.Context, in OpenPRInput) (*PROpened, error) {
	out, err := c.wrap.OpenPR(ctx, in)
	if !*c.did {
		*c.did = true
		c.cancel()
		// Tiny sleep so cancellation propagates before the next
		// item iterates — without this the next loop tick races
		// the goroutine.
		time.Sleep(10 * time.Millisecond)
	}
	return out, err
}
func (c *cancelOnFirstOpenPR) FindPRByBranch(ctx context.Context, branch string) (*PROpened, error) {
	return c.wrap.FindPRByBranch(ctx, branch)
}
func (c *cancelOnFirstOpenPR) UpdatePRDescription(ctx context.Context, num int, body string) error {
	return c.wrap.UpdatePRDescription(ctx, num, body)
}
func (c *cancelOnFirstOpenPR) GetPR(ctx context.Context, num int) (*PRDetails, error) {
	return c.wrap.GetPR(ctx, num)
}
func (c *cancelOnFirstOpenPR) CommentPR(ctx context.Context, num int, body string) error {
	return c.wrap.CommentPR(ctx, num, body)
}

// --- Branch hash is deterministic across runs ---

func TestComputePlanHash_DeterministicAcrossRuns(t *testing.T) {
	plan := fixtureBasePlusTwoTenants()
	h1 := computePlanHash(plan)
	h2 := computePlanHash(plan)
	if h1 != h2 || len(h1) != 8 {
		t.Errorf("hash drift: %q vs %q (len=%d, want 8)", h1, h2, len(h1))
	}
}

func TestComputePlanHash_StructuralChangeFlipsHash(t *testing.T) {
	a := fixtureBasePlusTwoTenants()
	b := fixtureBasePlusTwoTenants()
	b.Items[1].TenantIDs = append(b.Items[1].TenantIDs, "tenant-z")
	if computePlanHash(a) == computePlanHash(b) {
		t.Errorf("hash should change when TenantIDs change")
	}
}

func TestComputePlanHash_TitleDriftDoesNotFlipHash(t *testing.T) {
	a := fixtureBasePlusTwoTenants()
	b := fixtureBasePlusTwoTenants()
	b.Items[0].Title = "[Base Infrastructure] Different rendering"
	if computePlanHash(a) != computePlanHash(b) {
		t.Errorf("hash should NOT change when only Title (rendering) drifts")
	}
}

// --- self-review fix-up regressions ----------------------------------

// PR-2 self-review caught: branch exists on remote but no open PR
// found → SkippedExisting with PRNumber=0 + ErrorMessage explaining
// the anomaly. Without this assertion, a regression that silently
// re-opens orphaned branches would slip through.
func TestApply_BranchExistsButNoPR_SkippedWithExplanation(t *testing.T) {
	res, _, _ := runApply(t, func(in *ApplyInput, fg *fakeGit, fp *fakePR) {
		hash := computePlanHash(in.Plan)
		baseBranch := fmt.Sprintf("%s/base-%s", defaultBranchPrefix, hash)
		fg.branchExists[baseBranch] = true
		// No findByBranchExisting entry → returns (nil, nil) → "no
		// open PR" branch in apply.go.
	})
	if res.Items[0].Status != ApplyStatusSkippedExisting {
		t.Errorf("status = %q, want skipped_existing", res.Items[0].Status)
	}
	if res.Items[0].PRNumber != 0 {
		t.Errorf("PRNumber = %d, want 0 (no open PR found)", res.Items[0].PRNumber)
	}
	if !strings.Contains(res.Items[0].ErrorMessage, "no open PR") {
		t.Errorf("ErrorMessage = %q, want orphan-branch explanation", res.Items[0].ErrorMessage)
	}
}

// PR-2 self-review caught: BranchPrefix starting with `-` would
// turn into `git checkout -B --foo/...` which parses --foo as a
// flag. Trim-leading-`-` defense kicks in inside branchNameFor.
func TestBranchNameFor_LeadingDashesStripped(t *testing.T) {
	item := PlanItem{Kind: PlanItemBase}
	got := branchNameFor("---hostile/prefix", "abcd1234", item)
	if strings.HasPrefix(got, "-") {
		t.Errorf("branchNameFor returned %q (leading `-` not stripped)", got)
	}
	if !strings.HasPrefix(got, "hostile/prefix/") {
		t.Errorf("branchNameFor = %q, want prefix `hostile/prefix/`", got)
	}
}

func TestBranchNameFor_AllDashesPrefixFallsBackToDefault(t *testing.T) {
	item := PlanItem{Kind: PlanItemBase}
	got := branchNameFor("---", "abcd1234", item)
	if !strings.HasPrefix(got, defaultBranchPrefix+"/") {
		t.Errorf("all-dashes prefix should fall back to default; got %q", got)
	}
}

// --- safeBranchSegment ---

func TestSafeBranchSegment(t *testing.T) {
	cases := []struct {
		in, want string
	}{
		{"db", "db"},
		{"db/region-1", "db-region-1"},
		{"team:payments", "team-payments"},
		{"a..b", "a-b"},
		{"--leading--", "leading"},
		{"", ""},
		{"unicode-café", "unicode-caf"},
	}
	for _, c := range cases {
		got := safeBranchSegment(c.in)
		if got != c.want {
			t.Errorf("safeBranchSegment(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}
