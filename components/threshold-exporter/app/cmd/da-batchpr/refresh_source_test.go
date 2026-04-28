package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/batchpr"
)

func fixtureRefreshSourceInputJSON() []byte {
	in := batchpr.RefreshSourceInput{
		Repo: batchpr.Repo{Owner: "o", Name: "r", BaseBranch: "main"},
		Targets: []batchpr.RefreshSourceTarget{
			{PRNumber: 101, BranchName: "tenant-a", SourceRuleIDs: []string{"r1"}},
			{PRNumber: 102, BranchName: "tenant-b", SourceRuleIDs: []string{"r2"}},
		},
	}
	body, _ := json.Marshal(in)
	return body
}

func TestRefreshSource_MissingPatchesDir(t *testing.T) {
	stderr := &bytes.Buffer{}
	code := cmdRefreshSource([]string{}, &bytes.Buffer{}, stderr)
	if code != exitCallerErr {
		t.Errorf("exit code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr.String(), "--patches-dir is required") {
		t.Errorf("expected --patches-dir error; got %q", stderr.String())
	}
}

func TestRefreshSource_HelpExitsOK(t *testing.T) {
	code := cmdRefreshSource([]string{"--help"}, &bytes.Buffer{}, &bytes.Buffer{})
	if code != exitOK {
		t.Errorf("exit code = %d, want %d", code, exitOK)
	}
}

// --- loadTargetPatches happy path + edge cases -----------------

func TestLoadTargetPatches_HappyPath(t *testing.T) {
	tmp := t.TempDir()
	mustWriteFile(t, filepath.Join(tmp, "101/conf.d/foo/_defaults.yaml"), []byte("body-a"))
	mustWriteFile(t, filepath.Join(tmp, "101/conf.d/foo/tenant.yaml"), []byte("body-b"))
	mustWriteFile(t, filepath.Join(tmp, "102/conf.d/bar/tenant.yaml"), []byte("body-c"))

	in := &batchpr.RefreshSourceInput{
		Targets: []batchpr.RefreshSourceTarget{
			{PRNumber: 101, BranchName: "a"},
			{PRNumber: 102, BranchName: "b"},
		},
	}
	if err := loadTargetPatches(in, tmp); err != nil {
		t.Fatalf("loadTargetPatches: %v", err)
	}
	if len(in.Targets[0].Files) != 2 {
		t.Errorf("PR 101 should have 2 files; got %d", len(in.Targets[0].Files))
	}
	if string(in.Targets[0].Files["conf.d/foo/_defaults.yaml"]) != "body-a" {
		t.Errorf("PR 101 file content mismatch")
	}
	if len(in.Targets[1].Files) != 1 {
		t.Errorf("PR 102 should have 1 file; got %d", len(in.Targets[1].Files))
	}
}

func TestLoadTargetPatches_MissingPRSubdirIsNoChange(t *testing.T) {
	tmp := t.TempDir()
	// No subdir for PR 101 (no patches for that tenant) → Files
	// stays nil → orchestration records PatchSkippedNoChange.
	mustWriteFile(t, filepath.Join(tmp, "102/x.yaml"), []byte("y"))

	in := &batchpr.RefreshSourceInput{
		Targets: []batchpr.RefreshSourceTarget{
			{PRNumber: 101, BranchName: "a"},
			{PRNumber: 102, BranchName: "b"},
		},
	}
	if err := loadTargetPatches(in, tmp); err != nil {
		t.Fatalf("loadTargetPatches: %v", err)
	}
	if in.Targets[0].Files != nil {
		t.Errorf("PR 101 (no subdir) should leave Files=nil; got %v", in.Targets[0].Files)
	}
	if len(in.Targets[1].Files) != 1 {
		t.Errorf("PR 102 should have 1 file; got %d", len(in.Targets[1].Files))
	}
}

func TestLoadTargetPatches_PatchesDirMissing(t *testing.T) {
	in := &batchpr.RefreshSourceInput{
		Targets: []batchpr.RefreshSourceTarget{{PRNumber: 1, BranchName: "x"}},
	}
	err := loadTargetPatches(in, filepath.Join(t.TempDir(), "nope"))
	if err == nil {
		t.Error("expected error for missing patches-dir, got nil")
	}
}

func TestLoadTargetPatches_PatchesDirIsAFile(t *testing.T) {
	tmp := t.TempDir()
	f := filepath.Join(tmp, "f.txt")
	mustWriteFile(t, f, []byte("not a dir"))

	in := &batchpr.RefreshSourceInput{
		Targets: []batchpr.RefreshSourceTarget{{PRNumber: 1, BranchName: "x"}},
	}
	err := loadTargetPatches(in, f)
	if err == nil || !strings.Contains(err.Error(), "not a directory") {
		t.Errorf("expected not-a-directory error; got %v", err)
	}
}

// --- runRefreshSource happy path --------------------------------

func TestRunRefreshSource_HappyPath_StubClients(t *testing.T) {
	tmp := t.TempDir()
	report := filepath.Join(tmp, "report.md")
	resultJSON := filepath.Join(tmp, "result.json")

	flags := &refreshSourceFlags{
		reportPath:     report,
		resultJSONPath: resultJSON,
	}
	in := batchpr.RefreshSourceInput{
		Repo: batchpr.Repo{Owner: "o", Name: "r", BaseBranch: "main"},
		Targets: []batchpr.RefreshSourceTarget{
			{
				PRNumber:      101,
				BranchName:    "tenant-a",
				SourceRuleIDs: []string{"rules.yaml#groups[0].rules[0]"},
				Files: map[string][]byte{
					"conf.d/foo/_defaults.yaml": []byte("new defaults"),
				},
			},
		},
	}
	stderr := &bytes.Buffer{}
	code := runRefreshSource(flags, in, &bytes.Buffer{}, stderr, &stubGit{}, &stubPR{})
	if code != exitOK {
		t.Errorf("exit code = %d, want %d (stderr: %s)", code, exitOK, stderr.String())
	}
	body, err := os.ReadFile(report)
	if err != nil {
		t.Fatalf("read report: %v", err)
	}
	if !strings.Contains(string(body), "# Patch plan") {
		t.Errorf("report missing patch-plan header; got %q", body)
	}
	rawJSON, err := os.ReadFile(resultJSON)
	if err != nil {
		t.Fatalf("read result.json: %v", err)
	}
	var result batchpr.RefreshSourceResult
	if err := json.Unmarshal(rawJSON, &result); err != nil {
		t.Fatalf("parse result JSON: %v", err)
	}
	if result.Summary.UpdatedCount != 1 {
		t.Errorf("UpdatedCount: got %d, want 1", result.Summary.UpdatedCount)
	}
}

// --- exitCodeForRefreshSource mapping ---------------------------

func TestExitCodeForRefreshSource(t *testing.T) {
	cases := []struct {
		name string
		s    batchpr.RefreshSourceSummary
		want int
	}{
		{"all updated", batchpr.RefreshSourceSummary{TotalTargets: 2, UpdatedCount: 2}, exitOK},
		{"with failures", batchpr.RefreshSourceSummary{TotalTargets: 2, UpdatedCount: 1, FailedCount: 1}, exitFailures},
		{"all skipped", batchpr.RefreshSourceSummary{TotalTargets: 2, SkippedCount: 2}, exitOK},
		{"all no-change", batchpr.RefreshSourceSummary{TotalTargets: 2, NoChangeCount: 2}, exitOK},
		{"all dry-run", batchpr.RefreshSourceSummary{TotalTargets: 2, DryRunCount: 2}, exitOK},
	}
	for _, tc := range cases {
		got := exitCodeForRefreshSource(tc.s)
		if got != tc.want {
			t.Errorf("%s: got %d, want %d", tc.name, got, tc.want)
		}
	}
}

// --- end-to-end: cmdRefreshSource via input JSON + patches dir ---

func TestCmdRefreshSource_EndToEnd_WithStubsViaWorkdir(t *testing.T) {
	// We can't inject clients into cmdRefreshSource without
	// constructing a real workdir. This test just exercises the
	// flag-parsing + patches-loading flow up to makeClients (which
	// will succeed because tmp/workdir exists). It doesn't run the
	// orchestration — that's covered by TestRunRefreshSource above.
	tmp := t.TempDir()
	patchesDir := filepath.Join(tmp, "patches")
	mustWriteFile(t, filepath.Join(patchesDir, "101/x.yaml"), []byte("y"))

	inputFile := filepath.Join(tmp, "in.json")
	mustWriteFile(t, inputFile, fixtureRefreshSourceInputJSON())

	workdir := filepath.Join(tmp, "repo")
	if err := os.MkdirAll(workdir, 0o755); err != nil {
		t.Fatalf("mkdir workdir: %v", err)
	}

	stderr := &bytes.Buffer{}
	code := cmdRefreshSource([]string{
		"--input", inputFile,
		"--patches-dir", patchesDir,
		"--workdir", workdir,
		"--report", filepath.Join(tmp, "report.md"),
		"--result-json", filepath.Join(tmp, "result.json"),
	}, &bytes.Buffer{}, stderr)
	// Either exitOK (if shell git/gh happen to work in CI) or
	// exitFailures / exitCallerErr (if they don't); the test just
	// verifies the flag-parsing + patches-loading reached the
	// orchestration call without panicking. Stderr should NOT
	// contain caller-error markers from the parse/load phases.
	if strings.Contains(stderr.String(), "is required") {
		t.Errorf("flag parsing failed unexpectedly: %s", stderr.String())
	}
	_ = code
}
