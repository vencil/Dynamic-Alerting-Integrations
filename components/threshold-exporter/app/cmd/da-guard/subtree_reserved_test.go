package main

// subtree_reserved_test.go — #2388: a subtree `_defaults.yaml` carrying a
// reserved key (`_state_*`, `_silent_mode`, …) in its defaults is reported as
// a subtree_default_reserved_key warning (exit 1 under --warn-as-error),
// whatever the value. The exporter's behaviour is NOT changed by #2388: a
// subtree `disable` still switches the state filter off, an `enable` and a
// `_silent_mode` severity are still dropped. The controls write the same key
// in the tenant's own file, or leave the subtree file out: nothing reported.
//
// Seams: none — t.TempDir() trees through run().

import (
	"encoding/json"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/testutil"
)

// reservedRoot is a root `_defaults.yaml` declaring the maintenance filter
// with the given default_state.
func reservedRoot(defaultState string) string {
	return "defaults:\n  mysql_connections: 80\n" +
		"state_filters:\n  maintenance:\n    reasons: []\n    severity: warning\n    default_state: " + defaultState + "\n"
}

// reservedCase is one tree: t1 lives in finance/, t2 in ops/. subtree is the
// `finance/_defaults.yaml` defaults body ("" = no such file); tenant is t1's
// own entry body.
type reservedCase struct {
	name, defaultState, subtree, tenant string
	wantKey                             string // "" = no finding
	wantMaint                           bool   // served _state_maintenance for t1
	wantSilent                          []any  // served _silent_mode for t1
}

var reservedCases = []reservedCase{
	{name: "EN", defaultState: "disable", subtree: "_state_maintenance: enable", tenant: "{}",
		wantKey: "_state_maintenance", wantMaint: false, wantSilent: []any{}},
	{name: "SM", defaultState: "disable", subtree: "_silent_mode: warning", tenant: "{}",
		wantKey: "_silent_mode", wantMaint: false, wantSilent: []any{}},
	{name: "DIS", defaultState: "enable", subtree: "_state_maintenance: disable", tenant: "{}",
		wantKey: "_state_maintenance", wantMaint: false, wantSilent: []any{}},
	{name: "CTL", defaultState: "disable", tenant: "\n    _state_maintenance: enable",
		wantMaint: true, wantSilent: []any{}},
	{name: "SMCTL", defaultState: "disable", tenant: "\n    _silent_mode: warning",
		wantMaint: false, wantSilent: []any{"warning"}},
	{name: "DISCTL", defaultState: "enable", tenant: "{}",
		wantMaint: true, wantSilent: []any{}},
}

func (c reservedCase) files() map[string]string {
	files := map[string]string{
		"_defaults.yaml":     reservedRoot(c.defaultState),
		"finance/t1.yaml":    "tenants:\n  t1: " + c.tenant + "\n",
		"ops/t2.yaml":        "tenants:\n  t2: {}\n",
		"ops/_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
	}
	if c.subtree != "" {
		files["finance/_defaults.yaml"] = "defaults:\n  " + c.subtree + "\n"
	}
	return files
}

func writeReservedConfD(t *testing.T, files map[string]string) string {
	t.Helper()
	tmp := t.TempDir()
	tree := map[string]string{}
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	return filepath.Join(tmp, "conf.d")
}

// reservedFindings runs the guard with --format json and returns the exit code
// and the subtree_default_reserved_key findings.
func reservedFindings(t *testing.T, args ...string) (int, []guard.Finding) {
	t.Helper()
	code, stdout, stderr := runOnce(t, append([]string{"--format", "json"}, args...)...)
	var doc struct {
		Report *guard.GuardReport `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("stdout is not JSON (exit %d): %v\nstderr=%s", code, err, stderr)
	}
	var out []guard.Finding
	for _, f := range doc.Report.Findings {
		if f.Kind == guard.FindingSubtreeDefaultReservedKey {
			out = append(out, f)
		}
	}
	return code, out
}

func TestGuard_SubtreeReservedKey(t *testing.T) {
	t.Parallel()
	for _, c := range reservedCases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := writeReservedConfD(t, c.files())

			code, got := reservedFindings(t, "--config-dir", dir)
			if code != exitOK {
				t.Errorf("exit = %d, want %d (a warning does not fail the run)", code, exitOK)
			}
			if c.wantKey == "" {
				if len(got) != 0 {
					t.Errorf("findings = %+v, want none", got)
				}
			} else {
				if len(got) != 1 {
					t.Fatalf("subtree_default_reserved_key findings = %+v, want exactly one", got)
				}
				f := got[0]
				if f.Severity != guard.SeverityWarn || f.TenantID != "t1" || f.Field != c.wantKey {
					t.Errorf("finding = %+v, want warn / t1 / %s", f, c.wantKey)
				}
				for _, want := range []string{"`finance/_defaults.yaml`", "next minor release", "becomes an error", "#2388"} {
					if !strings.Contains(f.Message, want) {
						t.Errorf("message lacks %q: %s", want, f.Message)
					}
				}
			}

			wantCode := exitOK
			if c.wantKey != "" {
				wantCode = exitFindings
			}
			if code, _ := reservedFindings(t, "--config-dir", dir, "--warn-as-error"); code != wantCode {
				t.Errorf("--warn-as-error: exit = %d, want %d", code, wantCode)
			}

			// The exporter's behaviour is unchanged by #2388.
			code, doc, _, stderr := served(t, c.files(), "2026-10-01T00:00:00Z")
			mustOK(t, code, stderr)
			tv := doc.Tenants["t1"]
			if got := tv.Values["_state_maintenance"]; got != c.wantMaint {
				t.Errorf("served _state_maintenance = %v, want %v", got, c.wantMaint)
			}
			if got := tv.Values["_silent_mode"]; !reflect.DeepEqual(got, c.wantSilent) {
				t.Errorf("served _silent_mode = %#v, want %#v", got, c.wantSilent)
			}
		})
	}
}

// The fix sentence depends on the key kind.
func TestGuard_SubtreeReservedKeyFix(t *testing.T) {
	t.Parallel()
	dir := writeReservedConfD(t, reservedCase{defaultState: "disable", tenant: "{}",
		subtree: "_state_maintenance: enable\n  _silent_mode: warning"}.files())
	_, got := reservedFindings(t, "--config-dir", dir)
	msg := map[string]string{}
	for _, f := range got {
		msg[f.Field] = f.Message
	}
	if m := msg["_state_maintenance"]; !strings.Contains(m, "`state_filters.maintenance.default_state`") {
		t.Errorf("_state_maintenance fix = %q; want it to name state_filters.maintenance.default_state", m)
	}
	if m := msg["_silent_mode"]; !strings.Contains(m, "Set `_silent_mode` in each tenant's own entry under `tenants:`") ||
		strings.Contains(m, "state_filters") {
		t.Errorf("_silent_mode fix = %q; want the tenant-entry fix only", m)
	}
}

// Root defaults are not a subtree level: a reserved key there is not reported.
func TestGuard_RootReservedKeyIsNotSubtreeReserved(t *testing.T) {
	t.Parallel()
	files := reservedCase{defaultState: "disable", tenant: "{}"}.files()
	files["_defaults.yaml"] = strings.Replace(files["_defaults.yaml"],
		"mysql_connections: 80\n", "mysql_connections: 80\n  _state_maintenance: 0\n", 1)
	dir := writeReservedConfD(t, files)
	code, got := reservedFindings(t, "--config-dir", dir, "--warn-as-error")
	if code != exitOK || len(got) != 0 {
		t.Errorf("exit = %d, findings = %+v; want exit 0 and none (root defaults are out of scope)", code, got)
	}
}

func TestGuard_SubtreeReservedKeyFollowsScope(t *testing.T) {
	t.Parallel()
	dir := writeReservedConfD(t, reservedCases[0].files()) // EN
	for _, tc := range []struct {
		scope string
		want  int
	}{
		{scope: "finance", want: 1},
		{scope: "ops", want: 0}, // t1 is outside the scope
	} {
		t.Run(tc.scope, func(t *testing.T) {
			t.Parallel()
			code, got := reservedFindings(t, "--config-dir", dir, "--scope", tc.scope, "--warn-as-error")
			if len(got) != tc.want {
				t.Errorf("--scope %s: findings = %+v, want %d", tc.scope, got, tc.want)
			}
			wantCode := exitOK
			if tc.want > 0 {
				wantCode = exitFindings
			}
			if code != wantCode {
				t.Errorf("--scope %s --warn-as-error: exit = %d, want %d", tc.scope, code, wantCode)
			}
		})
	}
}

// #2388 r1b: `_custom_alerts` (guard.TopLevelReadElsewhere) at the TOP level
// of an unwrapped subtree `_defaults.yaml` is the custom-alert compiler's
// documented spelling (it reads every level's top-level list; the repo's own
// rule-packs/recipes/examples/conf.d/finance/_defaults.yaml is written so) and
// is not reported; the same list under a `defaults:` mapping is read by
// nothing and is reported. Measured with compile_custom_alerts.py: 12 shapes
// unwrapped, 11 wrapped (the subtree's recipe dropped).
func TestGuard_SubtreeCustomAlertsTopLevelIsNotReserved(t *testing.T) {
	t.Parallel()
	const recipe = "_custom_alerts:\n  - recipe: ratio\n    name: payment_failure_ratio\n" +
		"    metric: payment_failed_total\n    denominator_metric: payment_attempts_total\n" +
		"    op: \">\"\n    window: 5m\n    threshold: \"0.01:critical\"\n"
	indented := "defaults:\n" + "  " + strings.ReplaceAll(strings.TrimSuffix(recipe, "\n"), "\n", "\n  ") + "\n"
	for _, tc := range []struct {
		name, subtree string
		want          int
	}{
		{name: "top-level-unwrapped", subtree: recipe, want: 0},
		{name: "inside-defaults", subtree: indented, want: 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			files := reservedCase{defaultState: "disable", tenant: "{}"}.files()
			files["finance/_defaults.yaml"] = tc.subtree
			code, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, files))
			if len(got) != tc.want {
				t.Fatalf("findings = %+v, want %d", got, tc.want)
			}
			if code != exitOK {
				t.Errorf("exit = %d, want %d", code, exitOK)
			}
			if tc.want == 1 {
				f := got[0]
				if f.TenantID != "t1" || f.Field != "_custom_alerts" ||
					!strings.Contains(f.Message, "Move `_custom_alerts` out of `defaults:` to the top level") {
					t.Errorf("finding = %+v", f)
				}
			}
		})
	}
}

// #2388 r1b: the golden fixture tree mixed-mode/conf.d, whose
// reserved-null/child/_defaults.yaml nulls `_severity_dedup`: the null level is
// not named (the parent's non-null one still is).
func TestGuard_SubtreeReservedGoldenMixedMode(t *testing.T) {
	t.Parallel()
	dir := filepath.Join(goldenDir(t), "fixtures", "mixed-mode", "conf.d")
	_, got := reservedFindings(t, "--config-dir", dir)
	var rows []string
	for _, f := range got {
		rows = append(rows, f.TenantID+" "+f.Field)
		if strings.Contains(f.Message, "child/_defaults.yaml") {
			t.Errorf("a null level is named: %s", f.Message)
		}
	}
	want := []string{
		"tenant-date _silent_mode",
		"tenant-reserved _severity_dedup",
		"tenant-reserved _silent_mode",
	}
	if !reflect.DeepEqual(rows, want) {
		t.Errorf("findings = %v, want %v", rows, want)
	}
}
