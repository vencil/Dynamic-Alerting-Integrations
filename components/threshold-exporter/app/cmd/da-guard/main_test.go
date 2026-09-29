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
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// runOnce is the test harness — wraps run() with captured stdout
// + stderr and returns (exitCode, stdoutBody, stderrBody).
func runOnce(t *testing.T, args ...string) (int, string, string) {
	t.Helper()
	var stdout, stderr bytes.Buffer
	code := run(args, &stdout, &stderr)
	return code, stdout.String(), stderr.String()
}

// --- happy path / clean tree --------------------------------------

func TestRun_CleanTree_ExitsZero(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		// The platform declares cpu but supplies no default for it (the
		// `optional_overrides:` shape). tenant-a sets its own; tenant-b
		// never does, so cpu is absent from tenant-b's effective config
		// and the guard must flag it.
		//
		// This used to be written as `tenant-b: {cpu: ~}`, on the old
		// blanket ADR-017 rule that a null deletes the inherited default.
		// #1339 narrowed that rule: on a threshold key a null is a no-op,
		// because collector.go keeps emitting the platform default for it
		// — so that fixture no longer describes a tenant that is missing
		// anything. See TestRun_NullThreshold_IsNotMissing below.
		"conf.d/_defaults.yaml": "defaults:\n  mem: 70\n",
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
		"conf.d/tenant-b.yaml":  "tenants:\n  tenant-b:\n    mem: 75\n",
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

// TestRun_NullThreshold_IsNotMissing — #1339. A null on a threshold key is
// not an opt-out, so the guard must NOT report the tenant as missing the
// field: collector.go still exports the platform default for it, and a guard
// that blocked the merge here would be blocking on a phantom.
func TestRun_NullThreshold_IsNotMissing(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  cpu: 70\n",
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
		"conf.d/tenant-b.yaml":  "tenants:\n  tenant-b:\n    cpu: ~\n",
	})
	code, stdout, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--required-fields", "cpu",
	)
	if code != exitOK {
		t.Errorf("exit = %d, want %d (cpu is inherited, not missing). stdout=%q", code, exitOK, stdout)
	}
	if strings.Contains(stdout, "missing_required") {
		t.Errorf("null on a threshold key must not be reported as missing: %q", stdout)
	}
}

// --- routing: unknown receiver type --------------------------------

func TestRun_UnknownReceiverType_ExitsOne(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
	// Build a tenant with 6 metrics; limit at 3 → tenant exceeds.
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
	// 5 metrics, limit 6, warn-ratio default 0.8 → 4.8 → tenant at
	// 5 trips warning but no error. Default exit is 0.
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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

// --- a file the exporter's decode rejects: exit 3 (#2123) ---------

// dupKeyTenant writes one threshold twice in one mapping — the shape
// yaml.v3 rejects (`mapping key "cpu" already defined`), so the exporter's
// walker skips the file and the tenant in it is never checked.
const dupKeyTenant = "tenants:\n  tenant-b:\n    cpu: 80\n    cpu: 90\n"

// Broken file + readable file: the readable tenant is checked and clean,
// but the run must not report the tree as fine — exit 3, file named by its
// path relative to --config-dir, in the report and on stderr. The second
// half pins precedence: a real finding on the readable tenant still yields
// 3, not 1 (the findings cover only part of the tree).
func TestRun_ParseFailedFileBesideGoodFile_ExitsThree(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name     string
		required string
	}{
		{"good file clean", "cpu"},
		{"good file has a finding", "cpu,mem"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tmp := t.TempDir()
			testutil.WriteTree(t, tmp, map[string]string{
				"conf.d/_defaults.yaml":     "defaults:\n  cpu: 70\n",
				"conf.d/tenant-a.yaml":      "tenants:\n  tenant-a:\n    cpu: 80\n",
				"conf.d/team/tenant-b.yaml": dupKeyTenant,
			})
			code, stdout, stderr := runOnce(t,
				"--config-dir", filepath.Join(tmp, "conf.d"),
				"--required-fields", tc.required,
			)
			if code != exitParseFailed {
				t.Fatalf("exit = %d, want %d. stdout=%q stderr=%q", code, exitParseFailed, stdout, stderr)
			}
			if !strings.Contains(stdout, "Files the exporter cannot parse") ||
				!strings.Contains(stdout, "`team/tenant-b.yaml`") {
				t.Errorf("report should name the rejected file by relative path: %q", stdout)
			}
			if !strings.Contains(stdout, "Tenants in scope: **1**") {
				t.Errorf("the readable tenant should still be checked: %q", stdout)
			}
			// #2123 round 5: exit 3 must not come with the library's
			// "safe to merge" all-clear (the "good file clean" arm is the
			// one that has no findings and would print it).
			if strings.Contains(stdout, "safe to merge") {
				t.Errorf("an exit-3 report must not call the change safe to merge: %q", stdout)
			}
			if !strings.Contains(stderr, "team/tenant-b.yaml") {
				t.Errorf("stderr should name the rejected file: %q", stderr)
			}
		})
	}
}

// Only a broken file in scope: before #2123 this was the vacuously-safe
// branch (exit 0, "defaults change is vacuously safe") — the tenant the
// file declares simply did not exist for the guard. JSON carries the list.
func TestRun_OnlyParseFailedFile_ExitsThreeNotVacuous(t *testing.T) {
	t.Parallel()
	for _, format := range []string{"md", "json"} {
		t.Run(format, func(t *testing.T) {
			t.Parallel()
			tmp := t.TempDir()
			testutil.WriteTree(t, tmp, map[string]string{
				"conf.d/_defaults.yaml":     "defaults: {}\n",
				"conf.d/team/tenant-b.yaml": dupKeyTenant,
			})
			code, stdout, stderr := runOnce(t,
				"--config-dir", filepath.Join(tmp, "conf.d"),
				"--scope", filepath.Join(tmp, "conf.d", "team"),
				"--format", format,
			)
			if code != exitParseFailed {
				t.Fatalf("exit = %d, want %d. stdout=%q stderr=%q", code, exitParseFailed, stdout, stderr)
			}
			if strings.Contains(stdout, "vacuously safe") {
				t.Errorf("a scope whose tenant file is broken is not vacuously safe: %q", stdout)
			}
			if format == "json" {
				var doc struct {
					ParseFailed []string `json:"parse_failed"`
				}
				if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
					t.Fatalf("invalid JSON: %v\n%s", err, stdout)
				}
				if len(doc.ParseFailed) != 1 || doc.ParseFailed[0] != "team/tenant-b.yaml" {
					t.Errorf("parse_failed = %v, want [team/tenant-b.yaml]", doc.ParseFailed)
				}
			} else if !strings.Contains(stdout, "`team/tenant-b.yaml`") {
				t.Errorf("report should name the rejected file: %q", stdout)
			}
		})
	}
}

// The two decode failures the walker cannot see (#2123 round 2): a
// `_defaults.yaml` (never decoded by the walker, `_`-prefixed) with a
// repeated key, and a tenant file whose repeated key sits under a top-level
// field the walker's typed decode ignores but the effective-config merge
// (into `any`) rejects. Both used to surface as a resolve error → exit 2
// ("caller error"); they are the author's file to fix → exit 3, file named.
func TestRun_DecodeFailureWhileResolving_ExitsThree(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, file string
		tree       map[string]string
	}{
		{"defaults duplicate key", "_defaults.yaml", map[string]string{
			"conf.d/_defaults.yaml": "defaults:\n  cpu: 70\n  cpu: 80\n",
			"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
		}},
		{"duplicate key under an unknown top-level field", "tenant-b.yaml", map[string]string{
			"conf.d/_defaults.yaml": "defaults:\n  cpu: 70\n",
			"conf.d/tenant-b.yaml":  "extra:\n  k: 1\n  k: 2\ntenants:\n  tenant-b:\n    cpu: 80\n",
		}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			for _, format := range []string{"md", "json"} {
				tmp := t.TempDir()
				testutil.WriteTree(t, tmp, tc.tree)
				code, stdout, stderr := runOnce(t,
					"--config-dir", filepath.Join(tmp, "conf.d"), "--format", format)
				if code != exitParseFailed {
					t.Fatalf("[%s] exit = %d, want %d. stdout=%q stderr=%q",
						format, code, exitParseFailed, stdout, stderr)
				}
				if !strings.Contains(stderr, tc.file) {
					t.Errorf("[%s] stderr should name %s: %q", format, tc.file, stderr)
				}
				if format == "json" {
					var doc struct {
						ParseFailed []string `json:"parse_failed"`
					}
					if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
						t.Fatalf("invalid JSON: %v\n%s", err, stdout)
					}
					if len(doc.ParseFailed) != 1 || doc.ParseFailed[0] != tc.file {
						t.Errorf("parse_failed = %v, want [%s]", doc.ParseFailed, tc.file)
					}
				} else if !strings.Contains(stdout, "`"+tc.file+"`") {
					t.Errorf("report should name %s: %q", tc.file, stdout)
				}
			}
		})
	}
}

// Paired control: the broken file is OUTSIDE --scope, so it is not this
// run's business — the empty scope stays the vacuous exit 0.
func TestRun_ParseFailedFileOutsideScope_DoesNotCount(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":      "defaults: {}\n",
		"conf.d/other/tenant-b.yaml": dupKeyTenant,
		"conf.d/empty/":              "",
	})
	code, stdout, stderr := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--scope", filepath.Join(tmp, "conf.d", "empty"),
	)
	if code != exitOK {
		t.Errorf("exit = %d, want %d. stdout=%q stderr=%q", code, exitOK, stdout, stderr)
	}
}

// PR-5: redundant-override warn-tier should surface in the report.
// Tenant overrides cpu=80 with the same value as the merged defaults
// → guard.checkRedundantOverrides emits a SeverityWarn. Default exit
// code stays 0 (warnings don't block); --warn-as-error flips to 1.
func TestRun_RedundantOverride_SurfacesAsWarning(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  cpu: 80\n",
		// tenant-a explicitly sets cpu=80 — same as the inherited
		// default → redundant override.
		"conf.d/tenant-a.yaml": "tenants:\n  tenant-a:\n    cpu: 80\n",
		// tenant-b sets cpu=99 — meaningful override → NO finding.
		"conf.d/tenant-b.yaml": "tenants:\n  tenant-b:\n    cpu: 99\n",
	})
	code, stdout, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
	)
	if code != exitOK {
		t.Errorf("exit = %d, want %d (warnings don't block by default)", code, exitOK)
	}
	if !strings.Contains(stdout, "redundant_override") {
		t.Errorf("stdout should surface redundant_override finding: %q", stdout)
	}
	if !strings.Contains(stdout, "tenant-a") {
		t.Errorf("stdout should name tenant-a: %q", stdout)
	}
	// tenant-b override is meaningful — it should NOT appear in
	// findings (we sanity-check with substring proximity).
	if strings.Contains(stdout, "tenant-b") &&
		strings.Contains(stdout, "redundant_override") {
		// Verify no row mentions tenant-b alongside redundant_override.
		// A loose check is fine here since the report uses GFM tables
		// and findings sort errors-first then by tenant.
		lines := strings.Split(stdout, "\n")
		for _, line := range lines {
			if strings.Contains(line, "tenant-b") &&
				strings.Contains(line, "redundant_override") {
				t.Errorf("tenant-b should not have redundant_override; line: %q", line)
			}
		}
	}
}

func TestRun_RedundantOverride_WithWarnAsError_ExitsOne(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": "defaults:\n  cpu: 80\n",
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    cpu: 80\n",
	})
	code, _, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--warn-as-error",
	)
	if code != exitFindings {
		t.Errorf("exit = %d, want %d (warn-as-error promotes redundant warns)", code, exitFindings)
	}
}

// PR-5: cascading defaults — tenants under different sub-trees see
// different merged defaults. This verifies da-guard threads
// per-tenant defaults (NewDefaultsByTenant) through to the guard.
func TestRun_RedundantOverride_HonorsCascadingDefaults(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":    "defaults:\n  cpu: 70\n",
		"conf.d/db/_defaults.yaml": "defaults:\n  cpu: 80\n",
		// tenant-db overrides cpu=80 — matches db/ inherited → redundant
		"conf.d/db/tenant-db.yaml": "tenants:\n  tenant-db:\n    cpu: 80\n",
		// tenant-web overrides cpu=80 but inherits root cpu=70 → meaningful
		"conf.d/web/tenant-web.yaml": "tenants:\n  tenant-web:\n    cpu: 80\n",
	})
	code, stdout, _ := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
	)
	if code != exitOK {
		t.Errorf("exit = %d, want %d", code, exitOK)
	}
	// tenant-db redundant; tenant-web NOT redundant. We assert this
	// at the row level rather than via raw stdout substring because
	// both names also appear in the "Scanned files" details list,
	// which would make a substring check vacuously pass.
	lines := strings.Split(stdout, "\n")
	tenantDBFlagged := false
	for _, line := range lines {
		hasRedundant := strings.Contains(line, "redundant_override")
		if !hasRedundant {
			continue
		}
		if strings.Contains(line, "tenant-web") {
			t.Errorf("tenant-web should NOT be flagged (its 80 ≠ inherited 70); line: %q", line)
		}
		if strings.Contains(line, "tenant-db") {
			tenantDBFlagged = true
		}
	}
	if !tenantDBFlagged {
		t.Errorf("tenant-db should appear in a redundant_override row (its 80 = inherited 80); stdout: %q", stdout)
	}
}

// Regression: self-review caught writeEmptyReport silently ignoring
// IO errors. A bad --output path on an empty scope must surface as
// exitCallerErr, not exitOK.
func TestRun_EmptyScope_BadOutputPath_ExitsTwo(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
	code, _, stderr := runOnce(t)
	if code != exitCallerErr {
		t.Errorf("exit = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr, "--config-dir is required") {
		t.Errorf("stderr should explain the missing flag: %q", stderr)
	}
}

func TestRun_BadFormat_ExitsTwo(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
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
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
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
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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
	t.Parallel()
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
	t.Parallel()
	if runtime.GOOS != "windows" {
		t.Skip("only relevant on Windows where filepath.Separator differs")
	}
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
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

// --- #2280: resolved routing, routes, domain policies --------------------

// adr007Tree is the ADR-007 worked example from the parity matrix.
func adr007Tree(t *testing.T) map[string]string {
	t.Helper()
	for _, tree := range loadRoutingPolicyMatrix(t) {
		if tree.Name == "adr007-five-tenants" {
			return tree.Files
		}
	}
	t.Fatal("matrix has no adr007-five-tenants tree")
	return nil
}

// Before #2280 da-guard read the tenant's raw `_routing` only: t-ovr (a
// profile tenant with its own overrides) was missing_receiver_field, and the
// profile-only tenants and the domain policy were never looked at. The exit 1
// must now come from the policy, and from nothing else.
func TestRun_ADR007_ExitOneComesFromDomainPolicy(t *testing.T) {
	t.Parallel()
	files := adr007Tree(t)
	code, findings, _ := runTreeJSON(t, files)
	if code != exitFindings {
		t.Fatalf("exit = %d, want %d", code, exitFindings)
	}
	perTenant := map[string]int{}
	var noEscalation []string
	for _, f := range findings {
		if f.Kind == "missing_receiver_field" {
			t.Errorf("profile routing not resolved: %+v", f)
		}
		if f.Severity == "error" && f.Kind != "domain_policy_violation" && f.Kind != "critical_escalation_missing" {
			t.Errorf("error that is not a policy violation: %+v", f)
		}
		if f.Kind == "domain_policy_violation" {
			perTenant[f.TenantID]++
		}
		if f.Kind == "critical_escalation_missing" {
			noEscalation = append(noEscalation, f.TenantID)
		}
	}
	// #2325: the example policy also sets require_critical_escalation; the
	// two tenants with no pagerduty path break it.
	sort.Strings(noEscalation)
	if !equalStrings(noEscalation, []string{"t-dba", "t-livedbb"}) {
		t.Errorf("critical_escalation_missing tenants = %v, want [t-dba t-livedbb]", noEscalation)
	}
	// slack / webhook main receivers break both forbidden and allowed.
	want := map[string]int{"t-sre": 2, "t-dba": 2, "t-ovr": 2, "t-livedbb": 2}
	if len(perTenant) != len(want) {
		t.Errorf("violations per tenant = %v, want %v", perTenant, want)
	}
	for tenant, n := range want {
		if perTenant[tenant] != n {
			t.Errorf("violations per tenant = %v, want %v", perTenant, want)
			break
		}
	}

	// Control: the same tree without the policy file is clean — so the
	// exit 1 above is the policy's.
	delete(files, "_domain_policy.yaml")
	if code, findings, _ := runTreeJSON(t, files); code != exitOK {
		t.Errorf("without _domain_policy.yaml: exit = %d, want 0; findings %+v", code, findings)
	}
}

// A policy file whose structure cannot be used is ONE finding (TenantID "")
// and the other checks still run: exit 1, never 3, and parse_failed stays the
// exporter's own list (#1654).
func TestRun_UnusablePolicyStructure_NamedAndOtherChecksRun(t *testing.T) {
	t.Parallel()
	code, findings, parseFailed := runTreeJSON(t, map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 70\n",
		"_domain_policy.yaml": "domain_policies:\n  finance:\n    tenants: t-pol\n" +
			"    constraints:\n      forbidden_receiver_types: [slack]\n",
		"_routing_profiles.yaml": "routing_profiles: [not, a, mapping]\n",
		"t-pol.yaml": "tenants:\n  t-pol:\n    cpu: 80\n    _routing:\n" +
			"      receiver: {type: telegram, url: 'https://x.example/h'}\n",
	})
	if code != exitFindings || len(parseFailed) != 0 {
		t.Fatalf("exit = %d parse_failed = %v, want exit 1 and no parse_failed", code, parseFailed)
	}
	var got []string
	for _, f := range findings {
		got = append(got, f.Severity+"/"+f.Kind+"/"+f.TenantID+"/"+f.Field)
	}
	sort.Strings(got)
	want := []string{
		"error/domain_policy_unusable//_domain_policy.yaml:domain_policies.finance.tenants",
		"error/unknown_receiver_type/t-pol/receiver.type",
		"warn/routing_profiles_unusable//_routing_profiles.yaml:routing_profiles",
	}
	if !equalStrings(got, want) {
		t.Errorf("findings %v\nwant %v", got, want)
	}
}

// A `!!null`-tagged require_critical_escalation (#2325) is judged as the
// generator's PyYAML reads it: a scalar is None — the constraint is off and
// forbidden_receiver_types still refuses slack — and a collection is refused
// with the whole file (the generator drops every domain policy in it).
func TestRun_TaggedNullEscalation_AsTheGenerator(t *testing.T) {
	t.Parallel()
	for v, want := range map[string]string{
		"!!null x":                    "error/domain_policy_violation/t-pol/receiver.type",
		"!<tag:yaml.org,2002:null> x": "error/domain_policy_violation/t-pol/receiver.type",
		`!!null ""`:                   "error/domain_policy_violation/t-pol/receiver.type",
		"!!null {}":                   "error/domain_policy_unusable//_domain_policy.yaml",
		"!!null [1]":                  "error/domain_policy_unusable//_domain_policy.yaml",
	} {
		code, findings, _ := runTreeJSON(t, map[string]string{
			"_defaults.yaml": "defaults:\n  cpu: 70\n",
			"_domain_policy.yaml": "domain_policies:\n  finance:\n    tenants: [t-pol]\n    constraints:\n" +
				"      require_critical_escalation: " + v + "\n      forbidden_receiver_types: [slack]\n",
			"t-pol.yaml": "tenants:\n  t-pol:\n    cpu: 80\n    _routing:\n" +
				"      receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n",
		})
		var got []string
		for _, f := range findings {
			got = append(got, f.Severity+"/"+f.Kind+"/"+f.TenantID+"/"+f.Field)
		}
		if code != exitFindings || !equalStrings(got, []string{want}) {
			t.Errorf("%s: exit = %d, findings %v; want exit %d and [%s]", v, code, got, exitFindings, want)
		}
	}
}

// taggedNullOutsidePolicyTrees carry `require_critical_escalation: !!null {}`
// in a platform file that is NOT the domain policy (#2325): the PyYAML
// reading of that key is the policy file's only, so the profiles / defaults
// file is read as before and the tenant's slack receiver still violates the
// policy — not dropped with the file, which left the tenant receiver-less and
// every violation gone.
var taggedNullOutsidePolicyTrees = map[string]map[string]string{
	"profiles file, top-level key": {
		"_routing_profiles.yaml": "routing_profiles:\n  p1:\n" +
			"    receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n" +
			"meta: {require_critical_escalation: !!null {}}\n",
		"t-pol.yaml": "tenants:\n  t-pol:\n    cpu: 80\n    _routing_profile: p1\n",
	},
	"profiles file, inside a profile": {
		"_routing_profiles.yaml": "routing_profiles:\n  p1:\n" +
			"    receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n" +
			"    require_critical_escalation: !!null {}\n",
		"t-pol.yaml": "tenants:\n  t-pol:\n    cpu: 80\n    _routing_profile: p1\n",
	},
	"defaults file carrying _routing_defaults": {
		"_defaults.yaml": "defaults:\n  cpu: 70\n_routing_defaults:\n" +
			"  receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n" +
			"meta: {require_critical_escalation: !!null {}}\n",
		"t-pol.yaml": "tenants:\n  t-pol:\n    cpu: 80\n    _routing:\n      group_wait: 30s\n",
	},
}

const taggedNullOutsidePolicyDomain = "domain_policies:\n  finance:\n    tenants: [t-pol]\n    constraints:\n" +
	"      require_critical_escalation: true\n      forbidden_receiver_types: [slack]\n"

func TestRun_TaggedNullEscalationOutsidePolicyFile_StillEnforced(t *testing.T) {
	t.Parallel()
	for name, tree := range taggedNullOutsidePolicyTrees {
		files := map[string]string{"_defaults.yaml": "defaults:\n  cpu: 70\n", "_domain_policy.yaml": taggedNullOutsidePolicyDomain}
		for k, v := range tree {
			files[k] = v
		}
		code, findings, _ := runTreeJSON(t, files)
		var got []string
		for _, f := range findings {
			got = append(got, f.Severity+"/"+f.Kind+"/"+f.TenantID+"/"+f.Field)
		}
		sort.Strings(got)
		want := []string{"error/critical_escalation_missing/t-pol/receiver.type", "error/domain_policy_violation/t-pol/receiver.type"}
		if code != exitFindings || !equalStrings(got, want) {
			t.Errorf("%s: exit = %d, findings %v; want exit %d and %v", name, code, got, exitFindings, want)
		}
	}
}

// A profiles / policy file that fails YAML syntax is a file the exporter
// drops: exit 3 names it, and the routing loader skips it rather than naming
// it a second time as an unusable structure.
func TestRun_SyntaxBrokenPlatformFile_ExitThreeNamedOnce(t *testing.T) {
	t.Parallel()
	for _, name := range []string{"_routing_profiles.yaml", "_domain_policy.yaml"} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			code, findings, parseFailed := runTreeJSON(t, map[string]string{
				"_defaults.yaml": "defaults:\n  cpu: 70\n",
				name:             "routing_profiles: [\n",
				"t-a.yaml":       "tenants:\n  t-a:\n    cpu: 80\n",
			})
			if code != exitParseFailed || !equalStrings(parseFailed, []string{name}) {
				t.Fatalf("exit = %d parse_failed = %v, want 3 naming %s", code, parseFailed, name)
			}
			for _, f := range findings {
				if f.TenantID == "" {
					t.Errorf("%s named twice: %+v", name, f)
				}
			}
		})
	}
}

// `{{tenant}}` is substituted before the receiver shape is judged, as the
// generator renders it; before #2280 the placeholder in a URL host was an
// invalid_receiver_field.
func TestRun_TenantPlaceholderSubstitutedBeforeShapeCheck(t *testing.T) {
	t.Parallel()
	code, findings, _ := runTreeJSON(t, map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 70\n_routing_defaults:\n  receiver:\n    type: slack\n" +
			"    api_url: 'https://{{tenant}}.hooks.slack.example/services/x'\n",
		"t-a.yaml": "tenants:\n  t-a:\n    cpu: 80\n",
	})
	if code != exitOK || len(findings) != 0 {
		t.Errorf("exit = %d findings %+v, want a clean run", code, findings)
	}
}

// `_routing_defaults.routes` is dropped before any merge (ADR-007 routes
// belong to a profile or the tenant). The route generator's --validate fails
// on it, so da-guard blocks too: exit 1 from one TenantID "" finding, the
// dropped route is not judged, and parse_failed stays the exporter's.
func TestRun_RoutingDefaultsRoutes_BlockAndAreNotRendered(t *testing.T) {
	t.Parallel()
	code, findings, parseFailed := runTreeJSON(t, map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 70\n_routing_defaults:\n" +
			"  receiver: {type: pagerduty, service_key: k}\n" +
			"  routes: [{match: {severity: critical}, receiver: {type: bogus}}]\n",
		"t-a.yaml": "tenants:\n  t-a:\n    cpu: 80\n",
	})
	if code != exitFindings || len(parseFailed) != 0 {
		t.Fatalf("exit = %d parse_failed = %v, want 1 and none", code, parseFailed)
	}
	if len(findings) != 1 || findings[0].Kind != "routing_defaults_routes_ignored" ||
		findings[0].TenantID != "" || findings[0].Field != "_defaults.yaml:_routing_defaults.routes" {
		t.Errorf("findings %+v, want only routing_defaults_routes_ignored on _defaults.yaml", findings)
	}
}
