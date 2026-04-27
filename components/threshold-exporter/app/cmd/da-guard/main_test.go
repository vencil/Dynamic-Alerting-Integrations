package main

// main_test.go — integration tests for the da-guard CLI.
//
// We exercise run() directly (no subprocess) so coverage and
// race-detector instrumentation flow through the same binary the
// rest of the threshold-exporter test suite uses. Tests use
// fabricated conf.d/ trees written to t.TempDir() rather than
// shared fixtures: each test pins a single guard scenario in its
// own tree, making failures easy to diff.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// runOnce is the test harness — wraps run() with captured stdout
// + stderr and returns (exitCode, stdoutBody, stderrBody).
func runOnce(t *testing.T, args ...string) (int, string, string) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	code := run(args, &stdout, &stderr)
	return code, stdout.String(), stderr.String()
}

// writeTree replicates the helper from pkg/config/scope_test.go
// because the cmd package can't import test-only helpers from
// pkg/config (Go's test isolation rule).
func writeTree(t *testing.T, tmp string, files map[string]string) {
	t.Helper()
	for rel, body := range files {
		clean := filepath.Join(tmp, filepath.FromSlash(rel))
		if body == "" {
			if err := os.MkdirAll(clean, 0o755); err != nil {
				t.Fatalf("mkdir %q: %v", clean, err)
			}
			continue
		}
		if err := os.MkdirAll(filepath.Dir(clean), 0o755); err != nil {
			t.Fatalf("mkdir parent of %q: %v", clean, err)
		}
		if err := os.WriteFile(clean, []byte(body), 0o644); err != nil {
			t.Fatalf("write %q: %v", clean, err)
		}
	}
}

// --- happy path / clean tree --------------------------------------

func TestRun_CleanTree_ExitsZero(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  cpu: 70\n",
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
	})
	code, stdout, stderr := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--required-fields", "cpu",
	)
	if code != exitOK {
		t.Errorf("exit = %d, want %d. stderr=%q", code, exitOK, stderr)
	}
	if !strings.Contains(stdout, "## Dangling Defaults Guard") {
		t.Errorf("stdout missing report header: %q", stdout)
	}
	if !strings.Contains(stdout, "Tenants in scope: **1**") {
		t.Errorf("stdout missing tenants count: %q", stdout)
	}
	if !strings.Contains(stdout, "✅ No findings") {
		t.Errorf("stdout missing all-clear marker: %q", stdout)
	}
	if !strings.Contains(stdout, "Scanned files") {
		t.Errorf("stdout should list scanned files in <details>: %q", stdout)
	}
}

// --- schema error: missing required field ------------------------

func TestRun_MissingRequired_ExitsOne(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  cpu: 70\n",
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
		// tenant-b has cpu null in its override → ADR-018 says null
		// deletes the inherited default, so the merged map ends up
		// with cpu absent. The guard should flag this as missing.
		"conf.d/tenant-b.yaml": "tenants:\n  tenant-b:\n    cpu: ~\n",
	})
	code, stdout, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--required-fields", "cpu",
	)
	if code != exitFindings {
		t.Errorf("exit = %d, want %d (errors found). stdout=%q", code, exitFindings, stdout)
	}
	if !strings.Contains(stdout, "tenant-b") {
		t.Errorf("stdout should name the failing tenant: %q", stdout)
	}
	if !strings.Contains(stdout, "missing_required") {
		t.Errorf("stdout should list missing_required kind: %q", stdout)
	}
}

// --- routing: unknown receiver type --------------------------------

func TestRun_UnknownReceiverType_ExitsOne(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults: {}\n",
		"conf.d/tenant-a.yaml": `tenants:
  tenant-a:
    cpu: 80
    _routing:
      receiver:
        type: telegram
        url: https://x.example.com
`,
	})
	code, stdout, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
	)
	if code != exitFindings {
		t.Errorf("exit = %d, want %d. stdout=%q", code, exitFindings, stdout)
	}
	if !strings.Contains(stdout, "unknown_receiver_type") {
		t.Errorf("stdout should surface routing finding: %q", stdout)
	}
}

// --- cardinality limit: warn ratio exit semantics -----------------

func TestRun_CardinalityExceeded_ExitsOne(t *testing.T) {
	// Build a tenant with 6 metrics; limit at 3 → tenant exceeds.
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": `defaults:
  m1: 1
  m2: 2
  m3: 3
  m4: 4
  m5: 5
  m6: 6
`,
		"conf.d/tenant-a.yaml": `tenants:
  tenant-a:
    m1: 10
`,
	})
	code, stdout, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--cardinality-limit", "3",
	)
	if code != exitFindings {
		t.Errorf("exit = %d, want %d. stdout=%q", code, exitFindings, stdout)
	}
	if !strings.Contains(stdout, "cardinality_exceeded") {
		t.Errorf("stdout should surface cardinality finding: %q", stdout)
	}
}

func TestRun_CardinalityWarn_DoesNotExitOne_ByDefault(t *testing.T) {
	// 5 metrics, limit 6, warn-ratio default 0.8 → 4.8 → tenant at
	// 5 trips warning but no error. Default exit is 0.
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": `defaults:
  m1: 1
  m2: 2
  m3: 3
  m4: 4
  m5: 5
`,
		"conf.d/tenant-a.yaml": `tenants:
  tenant-a:
    m1: 10
`,
	})
	code, stdout, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--cardinality-limit", "6",
	)
	if code != exitOK {
		t.Errorf("exit = %d, want %d (warn does not block). stdout=%q",
			code, exitOK, stdout)
	}
	if !strings.Contains(stdout, "cardinality_warning") {
		t.Errorf("stdout should still surface the warning: %q", stdout)
	}
}

func TestRun_CardinalityWarn_WithWarnAsError_ExitsOne(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": `defaults:
  m1: 1
  m2: 2
  m3: 3
  m4: 4
  m5: 5
`,
		"conf.d/tenant-a.yaml": `tenants:
  tenant-a:
    m1: 10
`,
	})
	code, _, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--cardinality-limit", "6",
		"--warn-as-error",
	)
	if code != exitFindings {
		t.Errorf("--warn-as-error: exit = %d, want %d", code, exitFindings)
	}
}

// --- empty scope is vacuously safe --------------------------------

func TestRun_EmptyScope_ExitsZero(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults: {}\n",
		"conf.d/empty/":         "",
	})
	code, stdout, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--scope", filepath.Join(tmp, "conf.d", "empty"),
	)
	if code != exitOK {
		t.Errorf("exit = %d, want %d (empty scope vacuous). stdout=%q",
			code, exitOK, stdout)
	}
	if !strings.Contains(stdout, "vacuously safe") {
		t.Errorf("stdout should explain why the empty scope is OK: %q", stdout)
	}
}

// Regression: self-review caught writeEmptyReport silently ignoring
// IO errors. A bad --output path on an empty scope must surface as
// exitCallerErr, not exitOK.
func TestRun_EmptyScope_BadOutputPath_ExitsTwo(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults: {}\n",
		"conf.d/empty/":         "",
	})
	// Output points at a path whose parent directory does not exist
	// → os.WriteFile must fail.
	badOut := filepath.Join(tmp, "no-such-dir", "report.md")
	code, _, stderr := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--scope", filepath.Join(tmp, "conf.d", "empty"),
		"--output", badOut,
	)
	if code != exitCallerErr {
		t.Errorf("exit = %d, want %d (bad --output should fail loudly)", code, exitCallerErr)
	}
	if !strings.Contains(stderr, "write --output") {
		t.Errorf("stderr should mention write failure: %q", stderr)
	}
}

// --- caller-error paths -------------------------------------------

func TestRun_MissingConfigDir_ExitsTwo(t *testing.T) {
	code, _, stderr := runOnce(t)
	if code != exitCallerErr {
		t.Errorf("exit = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr, "--config-dir is required") {
		t.Errorf("stderr should explain the missing flag: %q", stderr)
	}
}

func TestRun_BadFormat_ExitsTwo(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults: {}\n",
	})
	code, _, stderr := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--format", "xml",
	)
	if code != exitCallerErr {
		t.Errorf("exit = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr, "--format must be 'md' or 'json'") {
		t.Errorf("stderr should explain --format options: %q", stderr)
	}
}

func TestRun_BadWarnRatio_ExitsTwo(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults: {}\n",
	})
	for _, ratio := range []string{"-0.1", "1.0", "1.5"} {
		t.Run(ratio, func(t *testing.T) {
			code, _, stderr := runOnce(t,
				"--config-dir", filepath.Join(tmp, "conf.d"),
				"--cardinality-warn-ratio", ratio,
			)
			if code != exitCallerErr {
				t.Errorf("ratio %s: exit = %d, want %d", ratio, code, exitCallerErr)
			}
			if !strings.Contains(stderr, "--cardinality-warn-ratio") {
				t.Errorf("stderr should call out the bad ratio: %q", stderr)
			}
		})
	}
}

func TestRun_ScopeOutsideRoot_ExitsTwo(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults: {}\n",
		"other/x.yaml":          "tenants:\n  x: {}\n",
	})
	code, _, stderr := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--scope", filepath.Join(tmp, "other"),
	)
	if code != exitCallerErr {
		t.Errorf("exit = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr, "outside configDir") {
		t.Errorf("stderr should mention containment violation: %q", stderr)
	}
}

func TestRun_ConfigDirMissing_ExitsTwo(t *testing.T) {
	code, _, stderr := runOnce(t,
		"--config-dir", filepath.Join(t.TempDir(), "nope"),
	)
	if code != exitCallerErr {
		t.Errorf("exit = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr, "stat configDir") {
		t.Errorf("stderr should mention stat failure: %q", stderr)
	}
}

// --- output formats and routing ----------------------------------

func TestRun_JSONOutput_IsValidJSON(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  cpu: 70\n",
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
	})
	code, stdout, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--format", "json",
	)
	if code != exitOK {
		t.Fatalf("exit = %d, want 0. stdout=%q", code, stdout)
	}
	var doc struct {
		ConfigDir   string   `json:"config_dir"`
		SourceFiles []string `json:"source_files"`
		Report      struct {
			Findings []any `json:"findings"`
			Summary  struct {
				TotalTenants      int `json:"total_tenants"`
				Errors            int `json:"errors"`
				Warnings          int `json:"warnings"`
				PassedTenantCount int `json:"passed_tenant_count"`
			} `json:"summary"`
		} `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("output is not valid JSON: %v\n%s", err, stdout)
	}
	if doc.Report.Summary.TotalTenants != 1 {
		t.Errorf("TotalTenants = %d, want 1", doc.Report.Summary.TotalTenants)
	}
	if doc.Report.Summary.PassedTenantCount != 1 {
		t.Errorf("PassedTenantCount = %d, want 1", doc.Report.Summary.PassedTenantCount)
	}
}

func TestRun_OutputToFile_WritesAndAnnouncesOnStderr(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults: {}\n",
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
	})
	out := filepath.Join(tmp, "guard-report.md")
	code, stdout, stderr := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--output", out,
	)
	if code != exitOK {
		t.Fatalf("exit = %d, want 0. stderr=%q", code, stderr)
	}
	if stdout != "" {
		t.Errorf("--output should suppress stdout report; got %q", stdout)
	}
	body, err := os.ReadFile(out)
	if err != nil {
		t.Fatalf("read --output file: %v", err)
	}
	if !strings.Contains(string(body), "## Dangling Defaults Guard") {
		t.Errorf("file body missing report header: %q", string(body))
	}
	if !strings.Contains(stderr, "wrote report to") {
		t.Errorf("stderr should announce the output path: %q", stderr)
	}
}

// --- determinism: two runs over the same input → byte-identical output

func TestRun_Determinism_TwoRunsByteIdentical(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":  "defaults:\n  cpu: 70\n",
		"conf.d/a/tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu: 80\n",
		"conf.d/b/tenant-b.yaml": "tenants:\n  tenant-b:\n    cpu: 90\n",
	})
	args := []string{
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--required-fields", "cpu",
		"--format", "json",
	}
	_, out1, _ := runOnce(t, args...)
	_, out2, _ := runOnce(t, args...)
	if out1 != out2 {
		t.Errorf("non-deterministic output:\nrun1=%q\nrun2=%q", out1, out2)
	}
}

// --- version flag --------------------------------------------------

func TestRun_VersionFlag(t *testing.T) {
	prev := Version
	Version = "0.0.0-test"
	defer func() { Version = prev }()
	code, stdout, _ := runOnce(t, "--version")
	if code != exitOK {
		t.Errorf("exit = %d, want 0", code)
	}
	if !strings.Contains(stdout, "0.0.0-test") {
		t.Errorf("stdout = %q, want version %q", stdout, "0.0.0-test")
	}
}

// --- duplicate tenant ID --------------------------------------

func TestRun_DuplicateTenantID_ExitsTwo(t *testing.T) {
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":       "defaults: {}\n",
		"conf.d/a/tenant-x.yaml":      "tenants:\n  tenant-x:\n    cpu: 1\n",
		"conf.d/b/tenant-x-copy.yaml": "tenants:\n  tenant-x:\n    cpu: 2\n",
	})
	code, _, stderr := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
	)
	if code != exitCallerErr {
		t.Errorf("exit = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr, "duplicate tenant ID") {
		t.Errorf("stderr should call out duplicate: %q", stderr)
	}
}

// --- splitNonEmpty unit test --------------------------------------

func TestSplitNonEmpty(t *testing.T) {
	cases := []struct {
		in   string
		want []string
	}{
		{"", nil},
		{",,", nil},
		{"a", []string{"a"}},
		{"a,b,c", []string{"a", "b", "c"}},
		{"a, b , ,c", []string{"a", "b", "c"}},
		{",a,", []string{"a"}},
	}
	for _, c := range cases {
		got := splitNonEmpty(c.in)
		if len(got) != len(c.want) {
			t.Errorf("splitNonEmpty(%q) = %v, want %v", c.in, got, c.want)
			continue
		}
		for i := range got {
			if got[i] != c.want[i] {
				t.Errorf("splitNonEmpty(%q)[%d] = %q, want %q", c.in, i, got[i], c.want[i])
			}
		}
	}
}

// --- cross-platform sanity ----------------------------------------

func TestRun_PathSeparators_OnAnyOS(t *testing.T) {
	if runtime.GOOS != "windows" {
		t.Skip("only relevant on Windows where filepath.Separator differs")
	}
	tmp := t.TempDir()
	writeTree(t, tmp, map[string]string{
		"conf.d/db/tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu: 80\n",
		"conf.d/_defaults.yaml":   "defaults: {}\n",
	})
	code, stdout, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
	)
	if code != exitOK {
		t.Errorf("exit = %d, want 0. stdout=%q", code, stdout)
	}
	// Markdown <details> list should use forward slashes (Markdown
	// is OS-agnostic; users review reports across platforms).
	if !strings.Contains(stdout, "db/tenant-a.yaml") {
		t.Errorf("scanned-files list should use forward slashes: %q", stdout)
	}
}
