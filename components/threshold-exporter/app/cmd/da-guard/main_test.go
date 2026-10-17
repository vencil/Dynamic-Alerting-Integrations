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

// Before #2904 an unknown receiver type was exit 1 here; the default check
// now judges no routing (ADR-036 §9 step 2): exit 4, the tenant's `_routing`
// named as undetermined, and no routing finding. The generator's
// --validate --strict reports the receiver.
func TestRun_UnknownReceiverType_IsUndetermined(t *testing.T) {
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
	if code != exitUndetermined {
		t.Errorf("exit = %d, want %d. stdout=%q", code, exitUndetermined, stdout)
	}
	if strings.Contains(stdout, "unknown_receiver_type") || !strings.Contains(stdout, "`tenants.tenant-a._routing` (routing_key)") {
		t.Errorf("stdout should name the routing as not judged, with no routing finding: %q", stdout)
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

// Two decode failures (#2123 round 2): a `_defaults.yaml` (never decoded by
// the walker, `_`-prefixed) with a repeated key, and a tenant file whose
// repeated key sits under a top-level field the typed decode ignores but the
// effective-config merge (into `any`) rejects. Both used to surface as a
// resolve error → exit 2 ("caller error"); they are the author's file to fix
// → exit 3, file named. Since #3063 the walker refuses the tenant file itself
// (ParseTenantFile runs the merge's generic decode), so it reaches exit 3 as
// a ParseFailed file rather than through the resolve; the verdict is the same.
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
// run's exit 3 — but the route generator refuses it whole and judges the
// whole tree, so since #2904 the empty scope is undetermined (exit 4), never
// the vacuous 0.
func TestRun_ParseFailedFileOutsideScope_IsNotExitThree(t *testing.T) {
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
	if code != exitUndetermined || !strings.Contains(stdout, "`other/tenant-b.yaml` `duplicate_key` (generator_unreadable)") {
		t.Errorf("exit = %d, want %d naming the file. stdout=%q stderr=%q", code, exitUndetermined, stdout, stderr)
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
	// Not t.Parallel(): it writes the process-global Version, which every
	// run() reads (newParseTreeDoc, via undeterminedEvidence since #2904).
	// A serial top-level test finishes before any parallel one resumes, so
	// the swap cannot overlap another test's read; save-then-restore under
	// t.Parallel() raced (-race, TestRun_ReceiverReadBesideNonStringKeys).
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
		// #2386: `_meta`, not `meta` — a top-level threshold-shaped key beside
		// a `defaults:` mapping is defaults_toplevel_ignored, not this test's
		// subject.
		"_defaults.yaml": "defaults:\n  cpu: 70\n_routing_defaults:\n" +
			"  receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n" +
			"_meta: {require_critical_escalation: !!null {}}\n",
		"t-pol.yaml": "tenants:\n  t-pol:\n    cpu: 80\n    _routing:\n      group_wait: 30s\n",
	},
}

const taggedNullOutsidePolicyDomain = "domain_policies:\n  finance:\n    tenants: [t-pol]\n    constraints:\n" +
	"      require_critical_escalation: true\n      forbidden_receiver_types: [slack]\n"

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

// routingOnlyTrees are the pre-#2904 fixtures of the #2280 routing checks —
// the ADR-007 worked example and its control without the policy file, an
// unusable policy / profiles structure, a `!!null`-tagged
// require_critical_escalation in and outside the policy file, `{{tenant}}`
// in a receiver URL, `_routing_defaults.routes` — each a tree whose findings
// were all routing-plane.
func routingOnlyTrees(t *testing.T) map[string]map[string]string {
	t.Helper()
	trees := map[string]map[string]string{}
	adr := adr007Tree(t)
	trees["adr007"] = adr
	noPolicy := map[string]string{}
	for k, v := range adr {
		if k != "_domain_policy.yaml" {
			noPolicy[k] = v
		}
	}
	trees["adr007 without the policy file"] = noPolicy
	trees["unusable policy structure"] = map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 70\n",
		"_domain_policy.yaml": "domain_policies:\n  finance:\n    tenants: t-pol\n" +
			"    constraints:\n      forbidden_receiver_types: [slack]\n",
		"_routing_profiles.yaml": "routing_profiles: [not, a, mapping]\n",
		"t-pol.yaml": "tenants:\n  t-pol:\n    cpu: 80\n    _routing:\n" +
			"      receiver: {type: telegram, url: 'https://x.example/h'}\n",
	}
	for _, v := range []string{"!!null x", "!<tag:yaml.org,2002:null> x", `!!null ""`, "!!null {}", "!!null [1]"} {
		trees["tagged null escalation "+v] = map[string]string{
			"_defaults.yaml": "defaults:\n  cpu: 70\n",
			"_domain_policy.yaml": "domain_policies:\n  finance:\n    tenants: [t-pol]\n    constraints:\n" +
				"      require_critical_escalation: " + v + "\n      forbidden_receiver_types: [slack]\n",
			"t-pol.yaml": "tenants:\n  t-pol:\n    cpu: 80\n    _routing:\n" +
				"      receiver: {type: slack, api_url: 'https://hooks.slack.com/services/T/B/x'}\n",
		}
	}
	for name, tree := range taggedNullOutsidePolicyTrees {
		files := map[string]string{"_defaults.yaml": "defaults:\n  cpu: 70\n", "_domain_policy.yaml": taggedNullOutsidePolicyDomain}
		for k, v := range tree {
			files[k] = v
		}
		trees["tagged null outside the policy file, "+name] = files
	}
	trees["tenant placeholder in a receiver URL"] = map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 70\n_routing_defaults:\n  receiver:\n    type: slack\n" +
			"    api_url: 'https://{{tenant}}.hooks.slack.example/services/x'\n",
		"t-a.yaml": "tenants:\n  t-a:\n    cpu: 80\n",
	}
	trees["_routing_defaults.routes"] = map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 70\n_routing_defaults:\n" +
			"  receiver: {type: pagerduty, service_key: k}\n" +
			"  routes: [{match: {severity: critical}, receiver: {type: bogus}}]\n",
		"t-a.yaml": "tenants:\n  t-a:\n    cpu: 80\n",
	}
	return trees
}

// #2904: the default check judges none of them — exit 4, no finding, the
// evidence listed; the route generator judges them
// (tests/ops/test_generate_alertmanager_routes.py and the parity matrix).
func TestRun_RoutingOnlyTreesAreUndetermined(t *testing.T) {
	t.Parallel()
	for name, files := range routingOnlyTrees(t) {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			got := runJudged(t, files, "")
			if got.code != exitUndetermined || len(got.findings) != 0 || len(got.undetermined) == 0 {
				t.Errorf("exit %d, findings %v, undetermined %v; want 4, none, some\nstderr=%s",
					got.code, got.findings, got.undetermined, got.stderr)
			}
		})
	}
}

// --- fixture trees shared by the CLI tests ---------------------------------

// parityTree is one tree of tests/shared/routing_policy_parity_matrix.json,
// read here as a fixture only: its name and its files. The matrix's
// expectations are the route generator's
// (tests/shared/test_routing_policy_parity.py); da-guard's own half of it
// went with its routing checks (#2905).
type parityTree struct {
	Name  string            `json:"name"`
	Files map[string]string `json:"files"`
}

// loadRoutingPolicyMatrix reads the parity matrix's trees.
func loadRoutingPolicyMatrix(t *testing.T) []parityTree {
	t.Helper()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "routing_policy_parity_matrix.json")
	raw, err := os.ReadFile(path) // #nosec G304 -- repo test fixture
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m struct {
		Trees []parityTree `json:"trees"`
	}
	if err := json.Unmarshal(raw, &m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Trees) == 0 {
		t.Fatal("matrix has no trees — a vacuous corpus passes nothing")
	}
	for _, tree := range m.Trees {
		if tree.Name == "" || len(tree.Files) == 0 {
			t.Fatalf("matrix tree %q has no name or no files", tree.Name)
		}
	}
	return m.Trees
}

type jsonFinding struct {
	Severity string `json:"severity"`
	Kind     string `json:"kind"`
	TenantID string `json:"tenant_id"`
	Field    string `json:"field"`
}

// writeParityTree writes files (root-relative slash paths, any depth —
// #2326) under a fresh conf.d and returns it.
func writeParityTree(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "conf.d")
	for rel, content := range files {
		p := filepath.Join(dir, filepath.FromSlash(rel))
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// runTreeJSON writes files under a fresh conf.d and returns the exit code and
// the report's findings.
func runTreeJSON(t *testing.T, files map[string]string) (int, []jsonFinding, []string) {
	t.Helper()
	dir := writeParityTree(t, files)
	code, stdout, stderr := runOnce(t, "--config-dir", dir, "--format", "json")
	var doc struct {
		ParseFailed []string `json:"parse_failed"`
		Report      struct {
			Findings []jsonFinding `json:"findings"`
		} `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("exit %d, report is not JSON: %v\nstdout=%s\nstderr=%s", code, err, stdout, stderr)
	}
	return code, doc.Report.Findings, doc.ParseFailed
}

func equalStrings(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}
