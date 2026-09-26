package main

// cardinality_limit_source_test.go — #2043.
//
// Without an explicit --cardinality-limit, da-guard must predict against the
// cap the exporter will actually enforce for the tree: the ROOT
// `_defaults.yaml`'s max_metrics_per_tenant (unset/0 → 500, negative → no
// truncation). Before #2043 the CI workflow passed a hard-coded 500, so a
// tree that raised the cap got false errors and one that lowered it got
// silent passes.

import (
	"fmt"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// defaultsWithMetrics renders a root `_defaults.yaml` body carrying n metric
// defaults plus the given top-level extra lines (e.g. the cap).
func defaultsWithMetrics(n int, extra string) string {
	var b strings.Builder
	b.WriteString(extra)
	b.WriteString("defaults:\n")
	for i := 1; i <= n; i++ {
		fmt.Fprintf(&b, "  m%d: %d\n", i, i)
	}
	return b.String()
}

const oneTenant = "tenants:\n  tenant-a:\n    m1: 10\n"

func TestCardinalityLimit_RootCapIsUsedWhenFlagOmitted(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": defaultsWithMetrics(6, "max_metrics_per_tenant: 3\n"),
		"conf.d/tenant-a.yaml":  oneTenant,
	})
	code, stdout, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"))
	if code != exitFindings {
		t.Fatalf("exit = %d, want %d. stdout=%q stderr=%q", code, exitFindings, stdout, stderr)
	}
	if !strings.Contains(stdout, "cardinality_exceeded") {
		t.Errorf("root cap 3 < 6 metrics must be exceeded: %q", stdout)
	}
	if !strings.Contains(stderr, "cardinality limit 3 from") {
		t.Errorf("stderr should say where the limit came from: %q", stderr)
	}
}

func TestCardinalityLimit_RaisedRootCapIsNotAFalseAlarm(t *testing.T) {
	t.Parallel()
	// 501 metrics would exceed the built-in 500 — the old hard-coded
	// workflow value — but the tree raised its cap to 1000.
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": defaultsWithMetrics(501, "max_metrics_per_tenant: 1000\n"),
		"conf.d/tenant-a.yaml":  oneTenant,
	})
	code, stdout, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"))
	if code != exitOK {
		t.Fatalf("exit = %d, want %d. stdout=%q stderr=%q", code, exitOK, stdout, stderr)
	}
	if strings.Contains(stdout, "cardinality_") {
		t.Errorf("501 metrics under a 1000 cap must not be flagged: %q", stdout)
	}
}

func TestCardinalityLimit_UnsetRootCapIsTheBuiltIn500(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": defaultsWithMetrics(501, ""),
		"conf.d/tenant-a.yaml":  oneTenant,
	})
	code, stdout, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"))
	if code != exitFindings {
		t.Fatalf("exit = %d, want %d. stdout=%q stderr=%q", code, exitFindings, stdout, stderr)
	}
	if !strings.Contains(stdout, "cardinality_exceeded") {
		t.Errorf("501 metrics must exceed the built-in 500: %q", stdout)
	}
	if !strings.Contains(stderr, "cardinality limit 500 from") {
		t.Errorf("stderr should report the built-in 500: %q", stderr)
	}
}

func TestCardinalityLimit_NegativeRootCapDisablesTheCheck(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": defaultsWithMetrics(501, "max_metrics_per_tenant: -1\n"),
		"conf.d/tenant-a.yaml":  oneTenant,
	})
	code, stdout, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"))
	if code != exitOK {
		t.Fatalf("exit = %d, want %d. stdout=%q stderr=%q", code, exitOK, stdout, stderr)
	}
	if strings.Contains(stdout, "cardinality_") {
		t.Errorf("a negative cap means no truncation, so no finding: %q", stdout)
	}
	if !strings.Contains(stderr, "cardinality limit 0 from") {
		t.Errorf("stderr should report the check as off (0): %q", stderr)
	}
}

func TestCardinalityLimit_ExplicitFlagOverridesRootCap(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": defaultsWithMetrics(6, "max_metrics_per_tenant: 3\n"),
		"conf.d/tenant-a.yaml":  oneTenant,
	})
	dir := filepath.Join(tmp, "conf.d")

	// --cardinality-limit 0 still means "no check", even though the root cap is 3.
	code, stdout, stderr := runOnce(t, "--config-dir", dir, "--cardinality-limit", "0")
	if code != exitOK || strings.Contains(stdout, "cardinality_") {
		t.Errorf("explicit 0 must disable the check: exit=%d stdout=%q", code, stdout)
	}
	if strings.Contains(stderr, "cardinality limit") {
		t.Errorf("an explicit flag must not be reported as read from the tree: %q", stderr)
	}

	// An explicit larger value wins over the root cap.
	code, stdout, _ = runOnce(t, "--config-dir", dir, "--cardinality-limit", "100")
	if code != exitOK || strings.Contains(stdout, "cardinality_") {
		t.Errorf("explicit 100 must override root cap 3: exit=%d stdout=%q", code, stdout)
	}
}

func TestCardinalityLimit_ScopedRunStillUsesTheRootCap(t *testing.T) {
	t.Parallel()
	// The cap is one global value (#2028): a subtree `_defaults.yaml` cannot
	// set it, and a --scope run must use the ROOT's, not the scope's.
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml":      defaultsWithMetrics(6, "max_metrics_per_tenant: 3\n"),
		"conf.d/team/_defaults.yaml": "max_metrics_per_tenant: 1000\n",
		"conf.d/team/tenant-a.yaml":  oneTenant,
	})
	code, stdout, stderr := runOnce(t,
		"--config-dir", filepath.Join(tmp, "conf.d"),
		"--scope", filepath.Join(tmp, "conf.d", "team"),
	)
	if code != exitFindings {
		t.Fatalf("exit = %d, want %d. stdout=%q stderr=%q", code, exitFindings, stdout, stderr)
	}
	if !strings.Contains(stdout, "cardinality_exceeded") {
		t.Errorf("the root cap 3 applies inside the scope; the subtree 1000 is ignored: %q", stdout)
	}
}

func TestCardinalityLimit_UndecodableRootIsACallerError(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": defaultsWithMetrics(2, "max_metrics_per_tenant: lots\n"),
		"conf.d/tenant-a.yaml":  oneTenant,
	})
	code, stdout, stderr := runOnce(t, "--config-dir", filepath.Join(tmp, "conf.d"))
	if code != exitCallerErr {
		t.Fatalf("exit = %d, want %d. stdout=%q stderr=%q", code, exitCallerErr, stdout, stderr)
	}
	if !strings.Contains(stderr, "parse root defaults") {
		t.Errorf("stderr should name the undecodable root defaults: %q", stderr)
	}
}
