package parser

// Freshness CI gate for vm_only_functions.yaml.
//
// The allowlist's correctness depends on staying in sync with the
// metricsql parser release in go.mod. Two failure modes we explicitly
// guard against:
//
//   1. **Pinned-version drift**: someone bumps `metricsql` in go.mod
//      without re-auditing the YAML. New VM-only functions silently
//      classify as `prom`, breaking the anti-vendor-lock-in promise.
//      → TestVMOnlyFunctions_VersionPinMatchesGoMod fails.
//
//   2. **Allowlist regression**: someone removes an entry from the
//      YAML thinking it's "promQL-compatible now" without checking
//      what target Prometheus version supports it. The
//      coverage-floor test below catches gross regressions (allowlist
//      dropping below the canonical seed).
//
// These run with the rest of the parser unit tests (`go test
// ./internal/parser`) so a stale allowlist is caught in PR CI before
// merge — the customer never sees an "unexpected portable" rule
// because of an out-of-sync allowlist.

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// TestVMOnlyFunctions_VersionPinMatchesGoMod compares the
// `metricsql_version` field embedded in vm_only_functions.yaml
// against the metricsql require line in go.mod. A mismatch means the
// dep was bumped without a corresponding allowlist re-audit; CI must
// fail the PR until someone hand-checks the metricsql changelog and
// updates either the YAML or the go.mod pin.
//
// Recovery: read the metricsql release notes for new VM-only
// functions, add them to vm_only_functions.yaml, then bump
// `metricsql_version:` to match go.mod.
func TestVMOnlyFunctions_VersionPinMatchesGoMod(t *testing.T) {
	yamlVer := VMOnlyMetricsqlVersion()
	if yamlVer == "" {
		t.Fatal("vm_only_functions.yaml has no metricsql_version field")
	}

	goMod, err := readGoModFromTest(t)
	if err != nil {
		t.Fatalf("read go.mod: %v", err)
	}
	depVer := extractMetricsqlVersionLine(goMod)
	if depVer == "" {
		t.Fatal("could not find `github.com/VictoriaMetrics/metricsql v...` in go.mod")
	}

	// Both should be of the form "v0.87.0". An exact match means the
	// audit is current. A mismatch is a real regression; do not
	// "auto-tolerate" patch-level diffs because patch releases CAN
	// add new functions.
	if yamlVer != depVer {
		t.Errorf(
			"metricsql version drift: vm_only_functions.yaml says %q but go.mod has metricsql %q.\n"+
				"Action required:\n"+
				"  1. Read https://github.com/VictoriaMetrics/metricsql/releases between %s and %s\n"+
				"  2. Audit any new function names; add VM-only ones to vm_only_functions.yaml\n"+
				"  3. Bump `metricsql_version:` in vm_only_functions.yaml to %s\n",
			yamlVer, depVer, yamlVer, depVer, depVer)
	}
}

// TestVMOnlyFunctions_AllowlistCoverageFloor pins a lower bound for
// the allowlist size — if the YAML drops below this it almost
// certainly means a careless edit. The floor is intentionally well
// below the actual count so that purposeful curation (removing one
// or two entries that PromQL has caught up with) doesn't trip it.
// The number gets bumped manually when the allowlist grows, with a
// commit message reviewer can sanity-check.
func TestVMOnlyFunctions_AllowlistCoverageFloor(t *testing.T) {
	const floor = 80 // PR-2 seed: 95 entries; floor leaves margin for purposeful trim
	got := len(VMOnlyFunctionNames())
	if got < floor {
		t.Errorf("vm_only_functions.yaml has %d entries; floor is %d. Was an entry removed in error?", got, floor)
	}
}

// TestVMOnlyFunctions_CoreEntriesAlwaysPresent pins a minimal core
// of "definitely VM-only" functions that we never expect Prometheus
// to add. If any of these go missing the allowlist is corrupt.
func TestVMOnlyFunctions_CoreEntriesAlwaysPresent(t *testing.T) {
	mustHave := []string{
		"rollup_rate",
		"quantiles_over_time",
		"interpolate",
		"label_set",
		"keep_last_value",
	}
	for _, name := range mustHave {
		if !IsVMOnlyFunction(name) {
			t.Errorf("core VM-only function %q missing from allowlist", name)
		}
	}
}

// readGoModFromTest finds the module's go.mod file by walking up
// from the test file's directory. We can't hard-code a relative
// path because `go test` runs the binary from a tmp dir.
func readGoModFromTest(t *testing.T) ([]byte, error) {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		return nil, errCaller
	}
	dir := filepath.Dir(file)
	for i := 0; i < 10; i++ { // bounded climb — we're at most a few levels deep
		candidate := filepath.Join(dir, "go.mod")
		if data, err := os.ReadFile(candidate); err == nil {
			return data, nil
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			break
		}
		dir = parent
	}
	return nil, errGoModNotFound
}

// extractMetricsqlVersionLine pulls the first
// `github.com/VictoriaMetrics/metricsql v...` token from go.mod.
// Trims trailing comments / `// indirect` markers.
func extractMetricsqlVersionLine(goMod []byte) string {
	for _, ln := range strings.Split(string(goMod), "\n") {
		ln = strings.TrimSpace(ln)
		if !strings.HasPrefix(ln, "github.com/VictoriaMetrics/metricsql ") {
			continue
		}
		// e.g. `github.com/VictoriaMetrics/metricsql v0.87.0` or
		// `github.com/VictoriaMetrics/metricsql v0.87.0 // indirect`
		fields := strings.Fields(ln)
		if len(fields) >= 2 {
			return fields[1]
		}
	}
	return ""
}

// Distinguished error sentinels keep the freshness gate's diagnostic
// output focused on actionable signals.
var (
	errCaller        = stringErr("runtime.Caller failed")
	errGoModNotFound = stringErr("go.mod not found in any ancestor up to 10 levels")
)

type stringErr string

func (e stringErr) Error() string { return string(e) }
