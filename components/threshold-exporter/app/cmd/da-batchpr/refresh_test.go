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

// fixtureRefreshInputJSON returns a minimal valid RefreshInput JSON.
func fixtureRefreshInputJSON() []byte {
	in := batchpr.RefreshInput{
		Repo:               batchpr.Repo{Owner: "o", Name: "r", BaseBranch: "main"},
		BaseMergedSHA:      "abc1234567890",
		BaseMergedPRNumber: 100,
		Targets: []batchpr.RefreshTarget{
			{PRNumber: 101, BranchName: "tenant-a"},
			{PRNumber: 102, BranchName: "tenant-b"},
		},
	}
	body, _ := json.Marshal(in)
	return body
}

func TestRefresh_HelpExitsOK(t *testing.T) {
	stderr := &bytes.Buffer{}
	stdout := &bytes.Buffer{}
	code := cmdRefresh([]string{"--help"}, stdout, stderr)
	if code != exitOK {
		t.Errorf("exit code = %d, want %d", code, exitOK)
	}
}

func TestRefresh_MalformedInputJSON(t *testing.T) {
	tmp := t.TempDir()
	bad := filepath.Join(tmp, "bad.json")
	mustWriteFile(t, bad, []byte(`{not json`))

	stderr := &bytes.Buffer{}
	code := cmdRefresh([]string{
		"--input", bad,
		"--workdir", tmp,
	}, &bytes.Buffer{}, stderr)
	if code != exitCallerErr {
		t.Errorf("exit code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr.String(), "parse JSON") {
		t.Errorf("expected parse-JSON error; got %q", stderr.String())
	}
}

// --- runRefresh happy path -------------------------------------

func TestRunRefresh_HappyPath_StubClients(t *testing.T) {
	tmp := t.TempDir()
	report := filepath.Join(tmp, "report.md")
	resultJSON := filepath.Join(tmp, "result.json")

	flags := &refreshFlags{
		reportPath:     report,
		resultJSONPath: resultJSON,
	}
	in := batchpr.RefreshInput{
		Repo:               batchpr.Repo{Owner: "o", Name: "r", BaseBranch: "main"},
		BaseMergedSHA:      "abc123",
		BaseMergedPRNumber: 100,
		Targets: []batchpr.RefreshTarget{
			{PRNumber: 101, BranchName: "tenant-a"},
		},
	}
	stderr := &bytes.Buffer{}
	code := runRefresh(flags, in, &bytes.Buffer{}, stderr, &stubGit{}, &stubPR{})
	if code != exitOK {
		t.Errorf("exit code = %d, want %d (stderr: %s)", code, exitOK, stderr.String())
	}
	body, err := os.ReadFile(report)
	if err != nil {
		t.Fatalf("read report: %v", err)
	}
	if !strings.Contains(string(body), "# Refresh report") {
		t.Errorf("report missing header; got %q", body)
	}
	rawJSON, err := os.ReadFile(resultJSON)
	if err != nil {
		t.Fatalf("read result.json: %v", err)
	}
	var result batchpr.RefreshResult
	if err := json.Unmarshal(rawJSON, &result); err != nil {
		t.Fatalf("parse result JSON: %v", err)
	}
	if len(result.Items) != 1 {
		t.Errorf("result.Items: got %d, want 1", len(result.Items))
	}
}

// --- runRefresh with library validation error ------------------

func TestRunRefresh_LibraryValidationError(t *testing.T) {
	flags := &refreshFlags{reportPath: "-", resultJSONPath: "-"}
	// Empty BaseMergedSHA → batchpr.Refresh returns hard error.
	in := batchpr.RefreshInput{
		Repo:    batchpr.Repo{Owner: "o", Name: "r", BaseBranch: "main"},
		Targets: []batchpr.RefreshTarget{{PRNumber: 1, BranchName: "x"}},
	}
	stderr := &bytes.Buffer{}
	code := runRefresh(flags, in, &bytes.Buffer{}, stderr, &stubGit{}, &stubPR{})
	if code != exitCallerErr {
		t.Errorf("exit code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr.String(), "BaseMergedSHA") {
		t.Errorf("expected BaseMergedSHA error; got %q", stderr.String())
	}
}

// --- exitCodeForRefresh mapping --------------------------------

func TestExitCodeForRefresh(t *testing.T) {
	cases := []struct {
		name string
		s    batchpr.RefreshSummary
		want int
	}{
		{"all clean", batchpr.RefreshSummary{TotalTargets: 2, CleanCount: 2}, exitOK},
		{"with conflicts", batchpr.RefreshSummary{TotalTargets: 2, CleanCount: 1, ConflictsCount: 1}, exitFailures},
		{"with failures", batchpr.RefreshSummary{TotalTargets: 2, CleanCount: 1, FailedCount: 1}, exitFailures},
		{"all skipped", batchpr.RefreshSummary{TotalTargets: 2, SkippedCount: 2}, exitOK},
		{"all dry-run", batchpr.RefreshSummary{TotalTargets: 2, DryRunCount: 2}, exitOK},
	}
	for _, tc := range cases {
		got := exitCodeForRefresh(tc.s)
		if got != tc.want {
			t.Errorf("%s: got %d, want %d", tc.name, got, tc.want)
		}
	}
}

// --- cmdRefresh: stdin input --------------------------------------

func TestCmdRefresh_ReadsFromStdinWhenInputDash(t *testing.T) {
	// We can't easily inject stdin into cmdRefresh (it uses os.Stdin),
	// so test the lower-level flow: parse flags + readInputJSON("-",
	// stdin, ...) confirms stdin support. (Stdin reading is exercised
	// via TestReadInputJSON_FromStdin in main_test.go.)
	t.Skip("stdin injection deferred; covered by TestReadInputJSON_FromStdin")
}
