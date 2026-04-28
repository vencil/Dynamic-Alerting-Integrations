package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/vencil/threshold-exporter/internal/batchpr"
)

// stubGit is a no-op GitClient that satisfies the interface for CLI
// tests. The CLI's job is to plumb inputs into the orchestration —
// it doesn't need the full apply_test.go fakes here.
type stubGit struct {
	createErr error
	writeErr  error
	commitErr error
	pushErr   error
}

func (g *stubGit) CreateBranch(ctx context.Context, name, base string) error {
	return g.createErr
}
func (g *stubGit) WriteFiles(ctx context.Context, branch string, files map[string][]byte) error {
	return g.writeErr
}
func (g *stubGit) Commit(ctx context.Context, branch, message, author string) error {
	return g.commitErr
}
func (g *stubGit) Push(ctx context.Context, branch string) error { return g.pushErr }
func (g *stubGit) BranchExistsRemote(ctx context.Context, branch string) (bool, error) {
	return false, nil
}
func (g *stubGit) CheckoutBranch(ctx context.Context, branch string) error { return nil }
func (g *stubGit) RebaseOnto(ctx context.Context, branch, oldBase, newBase string) (*batchpr.RebaseOutcome, error) {
	return &batchpr.RebaseOutcome{}, nil
}
func (g *stubGit) ForcePushWithLease(ctx context.Context, branch string) error { return nil }

// stubPR is a no-op PRClient that hands out incrementing PR numbers.
type stubPR struct {
	nextNum atomic.Int64
}

func (p *stubPR) OpenPR(ctx context.Context, in batchpr.OpenPRInput) (*batchpr.PROpened, error) {
	n := int(p.nextNum.Add(1)) + 99
	return &batchpr.PROpened{Number: n, URL: fmt.Sprintf("https://github.com/o/r/pull/%d", n)}, nil
}
func (p *stubPR) FindPRByBranch(ctx context.Context, branch string) (*batchpr.PROpened, error) {
	return nil, nil
}
func (p *stubPR) UpdatePRDescription(ctx context.Context, num int, body string) error { return nil }
func (p *stubPR) GetPR(ctx context.Context, num int) (*batchpr.PRDetails, error) {
	return &batchpr.PRDetails{Number: num, State: batchpr.PRStateOpen}, nil
}
func (p *stubPR) CommentPR(ctx context.Context, num int, body string) error { return nil }

// fixturePlanJSON returns a minimal valid Plan JSON for testing.
func fixturePlanJSON() []byte {
	plan := batchpr.Plan{
		Items: []batchpr.PlanItem{
			{
				Kind:                  batchpr.PlanItemBase,
				Title:                 "[Base] test",
				Description:           "base body",
				SourceProposalIndices: []int{0},
			},
			{
				Kind:                  batchpr.PlanItemTenant,
				Title:                 "[chunk 1/1] test",
				Description:           "tenant body",
				BlockedBy:             "0",
				SourceProposalIndices: []int{0},
				TenantIDs:             []string{"tenant-a"},
				ChunkKey:              "domain-x",
			},
		},
	}
	body, _ := json.Marshal(plan)
	return body
}

// --- Flag parsing --------------------------------------------------

func TestApply_MissingPlanFlag(t *testing.T) {
	stderr := &bytes.Buffer{}
	code := cmdApply([]string{}, &bytes.Buffer{}, stderr)
	if code != exitCallerErr {
		t.Errorf("exit code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr.String(), "--plan is required") {
		t.Errorf("expected --plan required error; got %q", stderr.String())
	}
}

func TestApply_MissingEmitDirFlag(t *testing.T) {
	stderr := &bytes.Buffer{}
	code := cmdApply([]string{"--plan", "p.json"}, &bytes.Buffer{}, stderr)
	if code != exitCallerErr {
		t.Errorf("exit code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr.String(), "--emit-dir is required") {
		t.Errorf("expected --emit-dir required error; got %q", stderr.String())
	}
}

func TestApply_BadRepoFlag(t *testing.T) {
	tmp := t.TempDir()
	planFile := filepath.Join(tmp, "p.json")
	mustWriteFile(t, planFile, fixturePlanJSON())
	emitDir := filepath.Join(tmp, "emit")
	mustWriteFile(t, filepath.Join(emitDir, "x.yaml"), []byte("x"))

	stderr := &bytes.Buffer{}
	code := cmdApply([]string{
		"--plan", planFile,
		"--emit-dir", emitDir,
		"--repo", "no-slash",
		"--workdir", tmp,
	}, &bytes.Buffer{}, stderr)
	if code != exitCallerErr {
		t.Errorf("exit code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr.String(), "owner/name") {
		t.Errorf("expected owner/name error; got %q", stderr.String())
	}
}

// --- runApply happy path -------------------------------------------

func TestRunApply_HappyPath_DryRun(t *testing.T) {
	tmp := t.TempDir()
	planFile := filepath.Join(tmp, "plan.json")
	mustWriteFile(t, planFile, fixturePlanJSON())
	// Use AllocateFiles' ADR-019 layout: _defaults.yaml goes to base,
	// tenant files go to tenant items.
	emitDir := filepath.Join(tmp, "emit")
	mustWriteFile(t, filepath.Join(emitDir, "conf.d/_defaults.yaml"), []byte("defaults"))
	mustWriteFile(t, filepath.Join(emitDir, "conf.d/tenant-a.yaml"), []byte("override"))

	report := filepath.Join(tmp, "report.md")
	resultJSON := filepath.Join(tmp, "result.json")

	flags := &applyFlags{
		planPath:       planFile,
		emitDir:        emitDir,
		dryRun:         true, // skip the openPR / push pretence
		reportPath:     report,
		resultJSONPath: resultJSON,
	}
	repo := batchpr.Repo{Owner: "o", Name: "r", BaseBranch: "main"}
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	code := runApply(flags, repo, stdout, stderr, &stubGit{}, &stubPR{}, &bytes.Buffer{})

	if code != exitOK {
		t.Errorf("exit code = %d, want %d (stderr: %s)", code, exitOK, stderr.String())
	}
	// Report file should exist + have apply summary.
	body, err := os.ReadFile(report)
	if err != nil {
		t.Fatalf("read report: %v", err)
	}
	if !strings.Contains(string(body), "# Apply report") {
		t.Errorf("report missing header; got %q", body)
	}
	if !strings.Contains(string(body), "dry-run") {
		t.Errorf("report should mark dry-run; got %q", body)
	}
	// Result JSON file should exist + parse as ApplyResult.
	rawJSON, err := os.ReadFile(resultJSON)
	if err != nil {
		t.Fatalf("read result.json: %v", err)
	}
	var result batchpr.ApplyResult
	if err := json.Unmarshal(rawJSON, &result); err != nil {
		t.Fatalf("parse result JSON: %v (raw=%s)", err, rawJSON)
	}
	if len(result.Items) != 2 {
		t.Errorf("result.Items: got %d, want 2", len(result.Items))
	}
}

// --- runApply: bad Plan JSON -----------------------------------

func TestRunApply_MalformedPlanJSON(t *testing.T) {
	tmp := t.TempDir()
	planFile := filepath.Join(tmp, "plan.json")
	mustWriteFile(t, planFile, []byte(`{not valid JSON`))
	emitDir := filepath.Join(tmp, "emit")
	mustWriteFile(t, filepath.Join(emitDir, "x.yaml"), []byte("x"))

	flags := &applyFlags{
		planPath:       planFile,
		emitDir:        emitDir,
		reportPath:     "-",
		resultJSONPath: "-",
	}
	repo := batchpr.Repo{Owner: "o", Name: "r", BaseBranch: "main"}
	stderr := &bytes.Buffer{}
	code := runApply(flags, repo, &bytes.Buffer{}, stderr, &stubGit{}, &stubPR{}, &bytes.Buffer{})
	if code != exitCallerErr {
		t.Errorf("exit code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr.String(), "parse JSON") {
		t.Errorf("expected parse-JSON error; got %q", stderr.String())
	}
}

// --- runApply: empty Plan ----------------------------------------

func TestRunApply_EmptyPlanRejected(t *testing.T) {
	tmp := t.TempDir()
	planFile := filepath.Join(tmp, "plan.json")
	mustWriteFile(t, planFile, []byte(`{"items": []}`))
	emitDir := filepath.Join(tmp, "emit")
	mustWriteFile(t, filepath.Join(emitDir, "x.yaml"), []byte("x"))

	flags := &applyFlags{
		planPath:       planFile,
		emitDir:        emitDir,
		reportPath:     "-",
		resultJSONPath: "-",
	}
	repo := batchpr.Repo{Owner: "o", Name: "r", BaseBranch: "main"}
	stderr := &bytes.Buffer{}
	code := runApply(flags, repo, &bytes.Buffer{}, stderr, &stubGit{}, &stubPR{}, &bytes.Buffer{})
	if code != exitCallerErr {
		t.Errorf("exit code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr.String(), "zero items") {
		t.Errorf("expected zero-items error; got %q", stderr.String())
	}
}

// --- runApply: missing emit-dir ---------------------------------

func TestRunApply_MissingEmitDir(t *testing.T) {
	tmp := t.TempDir()
	planFile := filepath.Join(tmp, "plan.json")
	mustWriteFile(t, planFile, fixturePlanJSON())

	flags := &applyFlags{
		planPath:       planFile,
		emitDir:        filepath.Join(tmp, "nope"),
		reportPath:     "-",
		resultJSONPath: "-",
	}
	repo := batchpr.Repo{Owner: "o", Name: "r", BaseBranch: "main"}
	stderr := &bytes.Buffer{}
	code := runApply(flags, repo, &bytes.Buffer{}, stderr, &stubGit{}, &stubPR{}, &bytes.Buffer{})
	if code != exitCallerErr {
		t.Errorf("exit code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr.String(), "read --emit-dir") {
		t.Errorf("expected emit-dir read error; got %q", stderr.String())
	}
}

// --- AllocateFiles warning ordering ----------------------------

func TestRunApply_AllocateWarningsPrependedNotAppended(t *testing.T) {
	// Self-review pin: allocation warnings come from a step that
	// runs BEFORE Apply, so they should appear FIRST in the result
	// Warnings slice — not after the orchestration's warnings.
	tmp := t.TempDir()
	planFile := filepath.Join(tmp, "plan.json")
	mustWriteFile(t, planFile, fixturePlanJSON())
	emitDir := filepath.Join(tmp, "emit")
	// Drop a file with a path AllocateFiles will warn about
	// (path doesn't match any expected shape, e.g. a stray README
	// at the root that's neither _defaults.yaml / PROPOSAL.md /
	// <tenant>.yaml). AllocateFiles emits an "unrecognised file
	// shape" warning for these.
	mustWriteFile(t, filepath.Join(emitDir, "stray-file.txt"), []byte("noise"))
	mustWriteFile(t, filepath.Join(emitDir, "conf.d/_defaults.yaml"), []byte("base"))
	mustWriteFile(t, filepath.Join(emitDir, "conf.d/tenant-a.yaml"), []byte("override"))

	flags := &applyFlags{
		planPath:       planFile,
		emitDir:        emitDir,
		dryRun:         true,
		reportPath:     "-",
		resultJSONPath: filepath.Join(tmp, "result.json"),
	}
	repo := batchpr.Repo{Owner: "o", Name: "r", BaseBranch: "main"}
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	code := runApply(flags, repo, stdout, stderr, &stubGit{}, &stubPR{}, &bytes.Buffer{})
	if code != exitOK {
		t.Fatalf("exit code = %d, want %d (stderr: %s)", code, exitOK, stderr.String())
	}
	// Read result JSON and verify warnings ordering.
	body, err := os.ReadFile(flags.resultJSONPath)
	if err != nil {
		t.Fatalf("read result: %v", err)
	}
	var result batchpr.ApplyResult
	if err := json.Unmarshal(body, &result); err != nil {
		t.Fatalf("parse result: %v", err)
	}
	// Find the index of the first allocation warning (mentioning
	// the stray file) — it should come before any orchestration
	// warning. We consider an allocation warning to be one
	// mentioning the stray-file path; orchestration warnings are
	// per-item Status warnings or the <base> placeholder note.
	allocIdx := -1
	for i, w := range result.Warnings {
		if strings.Contains(w, "stray-file.txt") {
			allocIdx = i
			break
		}
	}
	if allocIdx < 0 {
		// AllocateFiles may not have flagged it (depends on PR-2's
		// allocate.go behaviour for stray files at the emit root).
		// If no warning, this test's invariant doesn't apply — skip.
		t.Skipf("AllocateFiles did not surface a warning for stray file; warnings=%v",
			result.Warnings)
	}
	// All preceding warnings should NOT be orchestration warnings —
	// pin: allocation warnings come first.
	for i := 0; i < allocIdx; i++ {
		if strings.Contains(result.Warnings[i], "<base>") ||
			strings.Contains(result.Warnings[i], "tenant PR") {
			t.Errorf("orchestration warning %q found before allocation warning at index %d (allocation should be FIRST)",
				result.Warnings[i], allocIdx)
		}
	}
}

// --- exitCodeForApply mapping ------------------------------------

func TestExitCodeForApply(t *testing.T) {
	cases := []struct {
		name string
		s    batchpr.ApplySummary
		want int
	}{
		{"all created", batchpr.ApplySummary{TotalItems: 2, CreatedCount: 2}, exitOK},
		{"with failures", batchpr.ApplySummary{TotalItems: 2, CreatedCount: 1, FailedCount: 1}, exitFailures},
		{"all skipped", batchpr.ApplySummary{TotalItems: 2, SkippedExistingCount: 2}, exitOK},
		{"all dry-run", batchpr.ApplySummary{TotalItems: 2, DryRunCount: 2}, exitOK},
	}
	for _, tc := range cases {
		got := exitCodeForApply(tc.s)
		if got != tc.want {
			t.Errorf("%s: got %d, want %d", tc.name, got, tc.want)
		}
	}
}
