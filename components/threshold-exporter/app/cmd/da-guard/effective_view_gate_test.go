package main

// effective_view_gate_test.go — #2115 round 2 (E1): /effective's effective
// config is now laid per threshold (pkg/config effectiveView: one spelling
// per threshold). da-guard's main gate — schema / required fields — keeps
// judging the leaf-by-leaf merge (EffectiveConfig.MergedConfig): #2115
// changes no finding of the main report. (Cardinality reads neither since
// #3380: it counts the rows the exporter produces.)
//
// Measured on 26229806 (the gate fed the view): the tree below with
// `--required-fields mysql_threads_running` gave missing_required, rc 1 —
// the view carries the threshold only as the subtree's retired spelling, and
// resolvePath does not read aliases — and the predicted metric count fell
// from 3 to 2. On main fc5438c4: rc 0, no findings, count 3.

import (
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/testutil"
)

// effectiveViewGateTree: root canonical, subtree retired spelling, the
// tenant writing only the retired spelling's `_critical` key. The merge has
// 3 keys, the per-threshold view 2; /metrics serves tx 4 series.
var effectiveViewGateTree = map[string]string{
	"conf.d/_defaults.yaml":     "defaults:\n  mysql_threads_running: 30\n",
	"conf.d/sub/_defaults.yaml": "defaults:\n  mysql_cpu: 40\n",
	"conf.d/sub/tx.yaml":        "tenants:\n  tx:\n    mysql_cpu_critical: \"90\"\n",
}

func TestMainGate_RequiredFieldsJudgeTheMergeNotTheView(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, effectiveViewGateTree)
	dir := filepath.Join(tmp, "conf.d")

	code, stdout, stderr := runOnce(t, "--config-dir", dir, "--required-fields", "mysql_threads_running", "--format", "md")
	if code != exitOK {
		t.Errorf("exit = %d, want %d (the merge carries mysql_threads_running). stdout=%q stderr=%q", code, exitOK, stdout, stderr)
	}
	if !strings.Contains(stdout, "No findings") {
		t.Errorf("want no findings: %q", stdout)
	}

	// Must-trigger control: a key neither the merge nor the view carries is
	// still reported missing, so the green above is not a gate that stopped
	// looking.
	code, stdout, _ = runOnce(t, "--config-dir", dir, "--required-fields", "pg_connections", "--format", "md")
	if code != exitFindings || !strings.Contains(stdout, "missing_required") {
		t.Errorf("control: exit = %d, want %d with missing_required: %q", code, exitFindings, stdout)
	}
}

// Since #3380 the cardinality check counts neither the merge's keys nor the
// view's: it counts the user_threshold rows the exporter produces — here the
// threshold and its `_critical`, each with its #1231 legacy twin.
func TestMainGate_CardinalityCountsWhatMetricsServes(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, effectiveViewGateTree)
	dir := filepath.Join(tmp, "conf.d")
	if served := metricsRows(t, loadTree(t, dir), fixedNow, "tx", true); served != 4 {
		t.Fatalf("control: /metrics serves %d series of tx, want 4", served)
	}

	code, stdout, stderr := runOnce(t, "--config-dir", dir, "--cardinality-limit", "3", "--format", "md")
	if code != exitFindings {
		t.Errorf("exit = %d, want %d. stdout=%q stderr=%q", code, exitFindings, stdout, stderr)
	}
	if !strings.Contains(stdout, "would produce 4 user_threshold rows") {
		t.Errorf("want /metrics' count (4) over the limit: %q", stdout)
	}
}
