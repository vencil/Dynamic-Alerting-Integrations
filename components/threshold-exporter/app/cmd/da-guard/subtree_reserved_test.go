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
	"fmt"
	"path/filepath"
	"reflect"
	"slices"
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
	if m := msg["_silent_mode"]; !strings.Contains(m, "set `_silent_mode` in each tenant's own entry under `tenants:`") ||
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
					!strings.Contains(f.Message, "move `_custom_alerts` out of `defaults:` to the top level") {
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

// allFindings runs the guard with --format json and returns every finding as
// "severity kind tenant field".
func allFindings(t *testing.T, args ...string) []string {
	t.Helper()
	code, stdout, stderr := runOnce(t, append([]string{"--format", "json"}, args...)...)
	var doc struct {
		Report *guard.GuardReport `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("stdout is not JSON (exit %d): %v\nstderr=%s", code, err, stderr)
	}
	var out []string
	for _, f := range doc.Report.Findings {
		out = append(out, string(f.Severity)+" "+string(f.Kind)+" "+f.TenantID+" "+f.Field)
	}
	return out
}

// #2388 r2 (F1): the routing exclusion is the routing checks' own predicate
// (config.IsRoutingKey: `_routing`, `_routing_<…>`), not the `_routing`
// reserved prefix. `_routingProfile` is a reserved key no routing check names:
// reported here. `_routing_xyz` inside `defaults:` is routing_in_unread_location
// (an error) and not repeated here. A top-level `_routing_defaults` in an
// unwrapped subtree file is the generator's spelling (#2326): neither reports.
func TestGuard_SubtreeReservedRoutingKeys(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, subtree string
		want          []string
	}{
		{name: "routingProfile", subtree: "defaults:\n  _routingProfile: p1\n",
			want: []string{"warn subtree_default_reserved_key t1 _routingProfile"}},
		{name: "routing-key-in-defaults", subtree: "defaults:\n  _routing_xyz: p1\n",
			want: []string{"error routing_in_unread_location  finance/_defaults.yaml:defaults._routing_xyz"}},
		{name: "top-level-routing-defaults", subtree: "_routing_defaults:\n  receiver:\n    type: webhook\n" +
			"    url: https://hooks.example.com/a\n",
			want: nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			files := reservedCase{defaultState: "disable", tenant: "{}"}.files()
			files["finance/_defaults.yaml"] = tc.subtree
			got := allFindings(t, "--config-dir", writeReservedConfD(t, files))
			if !reflect.DeepEqual(got, tc.want) {
				t.Errorf("findings = %q, want %q", got, tc.want)
			}
		})
	}
}

// #2388 r2 (F2, F3): the fix depends on the key. The blind review's f8 shape:
// a subtree writing `_state_nope` (no such filter declared), `_silent_x` (not a
// recognised key), and the recognised `_silent_mode` / `_severity_dedup`.
func TestGuard_SubtreeReservedMessageShapes(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": reservedRoot("disable"),
		"a/_defaults.yaml": "defaults:\n  _silent_mode: warning\n  _state_nope: enable\n  _silent_x: 3\n" +
			"  _severity_dedup: disable\n  _state_maintenance: enable\n  _routingProfile: p1\n",
		"a/b/t.yaml": "tenants:\n  t1: {}\n",
	}
	_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, files))
	msg := map[string]string{}
	for _, f := range got {
		msg[f.Field] = f.Message
	}
	const ownEntry = "in each tenant's own entry"
	for _, tc := range []struct {
		key        string
		want, deny []string
	}{
		{key: "_state_nope",
			want: []string{"does not declare a filter `nope`", "Declare `nope` under `state_filters:`",
				"every tenant in the tree", "or delete it from this file"},
			deny: []string{ownEntry + " under", "Set `_state_nope`"}},
		{key: "_silent_x",
			want: []string{"Key `_silent_x`", "not a recognised key", "Delete it from this file."},
			deny: []string{"Reserved key", "Set `_silent_x`"}},
		// #2388 r3: reserved by prefix alone; nothing reads it by name.
		{key: "_routingProfile",
			want: []string{"Key `_routingProfile`", "not a recognised key", "Delete it from this file."},
			deny: []string{"Reserved key", "Set `_routingProfile`"}},
		{key: "_state_maintenance",
			// `enable` and `warning` are ignored by the overlay today (#2388 A).
			want: []string{"Today the exporter ignores this value; delete it from this file",
				"To have it take effect, set `_state_maintenance` " + ownEntry,
				"`state_filters.maintenance.default_state`", "affects every tenant in the tree"},
			deny: []string{"Move it"}},
		{key: "_silent_mode", want: []string{"Reserved key", "Today the exporter ignores this value",
			"To have it take effect, set `_silent_mode` " + ownEntry}, deny: []string{"Move it"}},
	} {
		m, ok := msg[tc.key]
		if !ok {
			t.Errorf("no finding for %s; got %v", tc.key, got)
			continue
		}
		for _, w := range tc.want {
			if !strings.Contains(m, w) {
				t.Errorf("%s: message lacks %q: %s", tc.key, w, m)
			}
		}
		for _, d := range tc.deny {
			if strings.Contains(m, d) {
				t.Errorf("%s: message has %q: %s", tc.key, d, m)
			}
		}
	}
	if len(got) != 6 {
		t.Errorf("findings = %d, want 6: %v", len(got), got)
	}
}

// #2388 r2 (F2): following each message's advice on the f8 shape — declare the
// filter at the root, delete `_silent_x` and `_routingProfile` (r3), move the
// recognised keys into the tenant's entry — leaves no finding AND serves the
// values. The declared
// filter applies to every tenant in the tree (t2 in c/ too), which is why the
// message says so (F3).
func TestGuard_SubtreeReservedAdviceIsAFix(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n" +
			"state_filters:\n  nope:\n    reasons: []\n    severity: warning\n    default_state: enable\n",
		"a/_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"a/b/t.yaml":       "tenants:\n  t1:\n    _silent_mode: warning\n    _severity_dedup: disable\n",
		"c/t.yaml":         "tenants:\n  t2: {}\n",
	}
	code, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, files), "--warn-as-error")
	if code != exitOK || len(got) != 0 {
		t.Errorf("exit = %d, findings = %+v; want 0 and none", code, got)
	}
	code, doc, _, stderr := served(t, files, "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	t1 := doc.Tenants["t1"].Values
	if t1["_state_nope"] != true || !reflect.DeepEqual(t1["_silent_mode"], []any{"warning"}) ||
		t1["_severity_dedup"] != "disable" {
		t.Errorf("t1 values = %v; want _state_nope true, _silent_mode [warning], _severity_dedup disable", t1)
	}
	if v := doc.Tenants["t2"].Values["_state_nope"]; v != true {
		t.Errorf("t2 _state_nope = %v, want true (the root filter covers the whole tree)", v)
	}
}

// #2388 r3: `_routingProfile` gets "delete it", and deleting it is the fix —
// the finding goes; the routing checks name it nowhere either way.
func TestGuard_SubtreeReservedRoutingProfileDeleted(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, subtree string
		want          []string
	}{
		{name: "written", subtree: "defaults:\n  mysql_connections: 70\n  _routingProfile: p1\n",
			want: []string{"warn subtree_default_reserved_key t1 _routingProfile"}},
		{name: "deleted", subtree: "defaults:\n  mysql_connections: 70\n", want: nil},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			files := reservedCase{defaultState: "disable", tenant: "{}"}.files()
			files["finance/_defaults.yaml"] = tc.subtree
			got := allFindings(t, "--config-dir", writeReservedConfD(t, files), "--warn-as-error")
			if !reflect.DeepEqual(got, tc.want) {
				t.Errorf("findings = %q, want %q", got, tc.want)
			}
		})
	}
}

// #2388 r4: a top-level `_silent_mode` beside a `defaults:` mapping in a
// subtree file is defaults_toplevel_ignored. Its fix used to be "move it under
// `defaults:`", which only turned it into subtree_default_reserved_key. Now it
// is that finding's fix — set it in the tenant's own entry — and doing so
// clears both findings and serves the value.
func TestGuard_SubtreeTopLevelReservedKeyFix(t *testing.T) {
	t.Parallel()
	before := map[string]string{
		"_defaults.yaml":   "defaults:\n  mysql_connections: 80\n",
		"a/_defaults.yaml": "defaults:\n  mysql_connections: 70\n_silent_mode: warning\n",
		"a/t.yaml":         "tenants:\n  t1: {}\n",
	}
	got := allFindings(t, "--config-dir", writeReservedConfD(t, before))
	if want := []string{"error defaults_toplevel_ignored  a/_defaults.yaml"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("before: findings = %q, want %q", got, want)
	}
	_, stdout, _ := runOnce(t, "--format", "json", "--config-dir", writeReservedConfD(t, before))
	if !strings.Contains(stdout, "set `_silent_mode` in each tenant's own entry under `tenants:`") ||
		strings.Contains(stdout, "Move them under") {
		t.Errorf("before: the fix is not the tenant-entry one: %s", stdout)
	}

	after := map[string]string{
		"_defaults.yaml":   before["_defaults.yaml"],
		"a/_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"a/t.yaml":         "tenants:\n  t1:\n    _silent_mode: warning\n",
	}
	if got := allFindings(t, "--config-dir", writeReservedConfD(t, after), "--warn-as-error"); len(got) != 0 {
		t.Errorf("after: findings = %q, want none", got)
	}
	code, doc, _, stderr := served(t, after, "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	if v := doc.Tenants["t1"].Values["_silent_mode"]; !reflect.DeepEqual(v, []any{"warning"}) {
		t.Errorf("after: served _silent_mode = %#v, want [warning]", v)
	}
}

// #2388 r5: a bare `_state_` is the filter named "", judged like any other:
// undeclared → (a) declare or delete; declared (a root `state_filters:` entry
// named "") → (c), the recognised-key fix.
func TestGuard_SubtreeReservedBareStatePrefix(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, root string
		want, deny []string
	}{
		{name: "undeclared", root: reservedRoot("disable"),
			want: []string{"Reserved key `_state_`", "does not declare a filter `\"\"`",
				"Declare `\"\"` under `state_filters:`", "or delete it from this file"},
			deny: []string{"not a recognised key", "Move it", "filter ``"}},
		{name: "declared", root: "defaults:\n  mysql_connections: 80\n" +
			"state_filters:\n  \"\":\n    reasons: [x]\n    severity: warning\n    default_state: enable\n",
			want: []string{"Reserved key `_state_`", "Move it: set `_state_: disable`", "`state_filters.\"\".default_state`"},
			deny: []string{"does not declare", "not a recognised key"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			files := reservedCase{defaultState: "disable", tenant: "{}"}.files()
			files["_defaults.yaml"] = tc.root
			files["finance/_defaults.yaml"] = "defaults:\n  _state_: disable\n"
			_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, files))
			if len(got) != 1 || got[0].Field != "_state_" {
				t.Fatalf("findings = %+v, want one for _state_", got)
			}
			for _, w := range tc.want {
				if !strings.Contains(got[0].Message, w) {
					t.Errorf("message lacks %q: %s", w, got[0].Message)
				}
			}
			for _, d := range tc.deny {
				if strings.Contains(got[0].Message, d) {
					t.Errorf("message has %q: %s", d, got[0].Message)
				}
			}
		})
	}
}

// #2388 r5: the blind review's m4 shape — the tenant already sets `_silent_mode`
// and the subtree file still carries it at the top level beside `defaults:` —
// must be told to delete it from the file (it has no effect there today); and
// a mixed tree (`defaults: {x: 1}` + top-level `mysql_connections` and
// `_silent_mode`), which IS defaults_toplevel_ignored, gets the Move sentence
// for the threshold without "leave `defaults:` with no value" (#2388 r6: the
// r5 version of this half used an unwrapped file, which raises no such
// finding, so it could not fail).
func TestGuard_SubtreeRefusedKeyDeleteFirst(t *testing.T) {
	t.Parallel()
	m4 := map[string]string{
		"_defaults.yaml":   "defaults:\n  mysql_connections: 80\n",
		"a/_defaults.yaml": "defaults:\n  mysql_connections: 70\n_silent_mode: warning\n",
		"a/t.yaml":         "tenants:\n  t1:\n    _silent_mode: warning\n",
	}
	_, stdout, _ := runOnce(t, "--format", "json", "--config-dir", writeReservedConfD(t, m4))
	if !strings.Contains(stdout, "Today it has no effect here") || !strings.Contains(stdout, "delete it from this file to keep things as they are.") {
		t.Errorf("m4: no delete instruction: %s", stdout)
	}
	mixed := map[string]string{
		"_defaults.yaml":   "defaults:\n  mysql_connections: 80\n",
		"a/_defaults.yaml": "defaults: {x: 1}\nmysql_connections: 70\n_silent_mode: warning\n",
		"a/t.yaml":         "tenants:\n  t1:\n    _silent_mode: warning\n",
	}
	got := allFindings(t, "--config-dir", writeReservedConfD(t, mixed))
	if !slices.Contains(got, "error defaults_toplevel_ignored  a/_defaults.yaml") {
		t.Fatalf("mixed: findings = %q, want defaults_toplevel_ignored", got)
	}
	_, stdout, _ = runOnce(t, "--format", "json", "--config-dir", writeReservedConfD(t, mixed))
	if !strings.Contains(stdout, "Move `mysql_connections` under `defaults:`.") {
		t.Errorf("mixed: no Move sentence for the threshold: %s", stdout)
	}
	if strings.Contains(stdout, "leave `defaults:` with no value") {
		t.Errorf("mixed: advises leaving `defaults:` with no value: %s", stdout)
	}
}

// #2388 r6: inside a subtree `defaults:`, a key the overlay applies today (a
// plain `disable`) must not be told to be deleted on its own — that changes
// what is served. The t2 shape: the message says "Move it" and warns that
// deleting alone changes the served value; doing the Move (tenant entry +
// delete from the file) keeps every served value and clears the findings.
func TestGuard_SubtreeReservedMoveKeepsServedValues(t *testing.T) {
	t.Parallel()
	root := "defaults:\n  mysql_connections: 80\n" +
		"state_filters:\n  maintenance:\n    reasons: [x]\n    severity: warning\n    default_state: enable\n"
	before := map[string]string{
		"_defaults.yaml": root,
		"a/_defaults.yaml": "defaults:\n  mysql_connections: 70\n  _state_maintenance: disable\n" +
			"  _severity_dedup: disable\n",
		"a/t.yaml": "tenants:\n  t1:\n    mysql_connections: 60\n",
	}
	_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, before))
	if len(got) != 2 {
		t.Fatalf("before: findings = %+v, want 2", got)
	}
	for _, f := range got {
		if !strings.Contains(f.Message, "Move it") || !strings.Contains(f.Message, "Deleting it alone can change") ||
			strings.Contains(f.Message, "Delete it from this file.") {
			t.Errorf("%s: message = %s", f.Field, f.Message)
		}
	}
	after := map[string]string{
		"_defaults.yaml":   root,
		"a/_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"a/t.yaml": "tenants:\n  t1:\n    mysql_connections: 60\n    _state_maintenance: disable\n" +
			"    _severity_dedup: disable\n",
	}
	if got := allFindings(t, "--config-dir", writeReservedConfD(t, after), "--warn-as-error"); len(got) != 0 {
		t.Errorf("after: findings = %q, want none", got)
	}
	_, b, _, _ := served(t, before, "2026-10-01T00:00:00Z")
	code, a, _, stderr := served(t, after, "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	if !reflect.DeepEqual(b.Tenants["t1"].Values, a.Tenants["t1"].Values) {
		t.Errorf("served values changed:\n before %v\n after  %v", b.Tenants["t1"].Values, a.Tenants["t1"].Values)
	}
	if a.Tenants["t1"].Values["_state_maintenance"] != false || a.Tenants["t1"].Values["_severity_dedup"] != "disable" {
		t.Errorf("after: values = %v; want _state_maintenance false, _severity_dedup disable", a.Tenants["t1"].Values)
	}
}

// #2388 r5: redundant_override does not tell a tenant to drop a key it
// "inherits" from a subtree `_defaults.yaml` that subtree defaults refuse —
// the exporter does not apply that inherited value, so the tenant's own key is
// the one in effect (m2: served ["warning"] only because the tenant sets it).
// The control inherits the same kind of key from the ROOT: still reported.
func TestGuard_RedundantOverrideSkipsRefusedSubtreeKey(t *testing.T) {
	t.Parallel()
	m2 := map[string]string{
		"_defaults.yaml":   "defaults:\n  mysql_connections: 80\n",
		"a/_defaults.yaml": "defaults:\nx: 1\nmysql_connections: 70\n_silent_mode: warning\n",
		"a/t.yaml":         "tenants:\n  t1:\n    _silent_mode: warning\n",
	}
	for _, f := range allFindings(t, "--config-dir", writeReservedConfD(t, m2)) {
		if strings.Contains(f, "redundant_override") {
			t.Errorf("m2: %s", f)
		}
	}
	code, doc, _, stderr := served(t, m2, "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	if v := doc.Tenants["t1"].Values["_silent_mode"]; !reflect.DeepEqual(v, []any{"warning"}) {
		t.Errorf("m2: served _silent_mode = %#v, want [warning]", v)
	}

	// Root control: `_severity_dedup` (a refused key) inherited from the ROOT
	// defaults as a number, overridden by the tenant with the same number.
	root := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n  _severity_dedup: 1\n",
		"a/t.yaml":       "tenants:\n  t1:\n    _severity_dedup: 1\n",
	}
	want := "warn redundant_override t1 _severity_dedup"
	if got := allFindings(t, "--config-dir", writeReservedConfD(t, root)); !slices.Contains(got, want) {
		t.Errorf("root control: findings = %q, want %q among them", got, want)
	}
}

// #2388 A: the fix for a recognised key follows the exporter overlay's own
// verdict. The blind review's r5f1 shape: both subtree values are IGNORED
// today (`_silent_mode: warning` is not threshold-shaped; `_state_offd:
// enable` neither), so the message says so and says "delete it". Deleting
// keeps every served value and clears the findings; following "To have it
// take effect" instead does change them — that is what it says it does.
func TestGuard_SubtreeReservedIgnoredValueDelete(t *testing.T) {
	t.Parallel()
	root := "defaults:\n  mysql_connections: 80\n" +
		"state_filters:\n  offd:\n    reasons: []\n    severity: warning\n    default_state: disable\n"
	before := map[string]string{
		"_defaults.yaml":   root,
		"a/_defaults.yaml": "defaults:\n  mysql_connections: 70\n  _silent_mode: warning\n  _state_offd: enable\n",
		"a/t.yaml":         "tenants:\n  t1: {}\n",
	}
	_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, before))
	if len(got) != 2 {
		t.Fatalf("before: findings = %+v, want 2", got)
	}
	for _, f := range got {
		if !strings.Contains(f.Message, "Today the exporter ignores this value; delete it from this file: what the exporter serves stays the same") ||
			strings.Contains(f.Message, "Move it") {
			t.Errorf("%s: message = %s", f.Field, f.Message)
		}
	}
	deleted := map[string]string{
		"_defaults.yaml":   root,
		"a/_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"a/t.yaml":         "tenants:\n  t1: {}\n",
	}
	if got := allFindings(t, "--config-dir", writeReservedConfD(t, deleted), "--warn-as-error"); len(got) != 0 {
		t.Errorf("deleted: findings = %q, want none", got)
	}
	_, b, _, _ := served(t, before, "2026-10-01T00:00:00Z")
	code, d, _, stderr := served(t, deleted, "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	if !reflect.DeepEqual(b.Tenants["t1"].Values, d.Tenants["t1"].Values) {
		t.Errorf("deleting changed served values:\n before %v\n after  %v", b.Tenants["t1"].Values, d.Tenants["t1"].Values)
	}
	// "To have it take effect": the values start to apply — expected, and why
	// it is not the keep-things-as-they-are action.
	effect := map[string]string{
		"_defaults.yaml":   root,
		"a/_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"a/t.yaml":         "tenants:\n  t1:\n    _silent_mode: warning\n    _state_offd: enable\n",
	}
	code, e, _, stderr := served(t, effect, "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	if v := e.Tenants["t1"].Values; !reflect.DeepEqual(v["_silent_mode"], []any{"warning"}) || v["_state_offd"] != true {
		t.Errorf("take effect: values = %v; want _silent_mode [warning], _state_offd true", v)
	}
	if v := b.Tenants["t1"].Values; !reflect.DeepEqual(v["_silent_mode"], []any{}) || v["_state_offd"] != false {
		t.Errorf("before: values = %v; want _silent_mode [], _state_offd false", v)
	}
}

// #2388 A: one subtree file with an applied value (`_state_maintenance:
// disable`, root default_state enable) and an ignored one (`_silent_mode:
// warning`): each key gets its own sentence.
func TestGuard_SubtreeReservedMixedAppliedAndIgnored(t *testing.T) {
	t.Parallel()
	files := reservedCase{defaultState: "enable", tenant: "{}"}.files()
	files["finance/_defaults.yaml"] = "defaults:\n  _state_maintenance: disable\n  _silent_mode: warning\n"
	_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, files))
	msg := map[string]string{}
	for _, f := range got {
		msg[f.Field] = f.Message
	}
	if m := msg["_state_maintenance"]; !strings.Contains(m, "Move it: set `_state_maintenance: disable`") ||
		!strings.Contains(m, "Deleting it alone can change what is served") || strings.Contains(m, "ignores this value") {
		t.Errorf("_state_maintenance (applied): %s", m)
	}
	if m := msg["_silent_mode"]; !strings.Contains(m, "Today the exporter ignores this value") ||
		strings.Contains(m, "Move it") {
		t.Errorf("_silent_mode (ignored): %s", m)
	}
}

// #2388 A r2 (F1): a subtree file is shared. c3: a/_defaults.yaml
// `_state_maintenance: disable` is applied for t1 and ignored for t2 (which
// sets the key itself). t2's finding must not say "delete it": that turns t1's
// filter on. It names t1 instead — also under `--scope a/x`, where t1 is not
// in scope (c4). Following t1's Move first, then deleting, keeps every served
// value.
func TestGuard_SubtreeReservedSharedFile(t *testing.T) {
	t.Parallel()
	root := "defaults:\n  mysql_connections: 80\n" +
		"state_filters:\n  maintenance:\n    reasons: []\n    severity: warning\n    default_state: enable\n"
	for _, tc := range []struct {
		name, t2File string
		scope        []string
	}{
		{name: "same-dir", t2File: "a/t2.yaml"},
		{name: "scoped-to-t2", t2File: "a/x/t2.yaml", scope: []string{"--scope", "a/x"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			files := map[string]string{
				"_defaults.yaml":   root,
				"a/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
				"a/t1.yaml":        "tenants:\n  t1: {}\n",
				tc.t2File:          "tenants:\n  t2:\n    _state_maintenance: enable\n",
			}
			_, got := reservedFindings(t, append([]string{"--config-dir", writeReservedConfD(t, files)}, tc.scope...)...)
			var t2 string
			for _, f := range got {
				if f.TenantID == "t2" {
					t2 = f.Message
				}
			}
			for _, w := range []string{"This tenant's `tenants:` entry in `" + tc.t2File + "` sets `_state_maintenance`",
				"other tenants get their value from one of these files: `t1`", "Do not just delete it from the file"} {
				if !strings.Contains(t2, w) {
					t.Errorf("t2 message lacks %q: %s", w, t2)
				}
			}
			if strings.Contains(t2, "stays the same") {
				t.Errorf("t2 message promises nothing changes: %s", t2)
			}
		})
	}
	// Following the messages: t1 moves the value into its own entry, then the
	// key is deleted from the file — served values unchanged, no finding.
	before := map[string]string{
		"_defaults.yaml":   root,
		"a/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
		"a/t1.yaml":        "tenants:\n  t1: {}\n",
		"a/t2.yaml":        "tenants:\n  t2:\n    _state_maintenance: enable\n",
	}
	after := map[string]string{
		"_defaults.yaml":   root,
		"a/_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"a/t1.yaml":        "tenants:\n  t1:\n    _state_maintenance: disable\n",
		"a/t2.yaml":        "tenants:\n  t2:\n    _state_maintenance: enable\n",
	}
	if got := allFindings(t, "--config-dir", writeReservedConfD(t, after), "--warn-as-error"); len(got) != 0 {
		t.Errorf("after: findings = %q, want none", got)
	}
	_, b, _, _ := served(t, before, "2026-10-01T00:00:00Z")
	code, a, _, stderr := served(t, after, "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	for _, id := range []string{"t1", "t2"} {
		if !reflect.DeepEqual(reservedValues(b.Tenants[id].Values), reservedValues(a.Tenants[id].Values)) {
			t.Errorf("%s served values changed:\n before %v\n after  %v", id, b.Tenants[id].Values, a.Tenants[id].Values)
		}
	}
}

// #2388 A r2 (F2): with two subtree levels writing the key, the Move names the
// value the tenant gets today and its file (c1: a/ `disable` applied, a/us/
// `warning` dropped → `[]` served), and says to delete the key from every
// listed file. Doing exactly that keeps served values unchanged.
func TestGuard_SubtreeReservedMoveNamesTheValue(t *testing.T) {
	t.Parallel()
	root := reservedRoot("enable")
	before := map[string]string{
		"_defaults.yaml":      root,
		"a/_defaults.yaml":    "defaults:\n  _silent_mode: disable\n",
		"a/us/_defaults.yaml": "defaults:\n  _silent_mode: warning\n",
		"a/us/t.yaml":         "tenants:\n  t1: {}\n",
	}
	_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, before))
	if len(got) != 1 {
		t.Fatalf("findings = %+v, want 1", got)
	}
	for _, w := range []string{"Move it: set `_silent_mode: disable` (the value this tenant gets today, from `a/_defaults.yaml`)",
		"delete `_silent_mode` from every subtree file listed above"} {
		if !strings.Contains(got[0].Message, w) {
			t.Errorf("message lacks %q: %s", w, got[0].Message)
		}
	}
	after := map[string]string{
		"_defaults.yaml":      root,
		"a/_defaults.yaml":    "defaults:\n  mysql_connections: 70\n",
		"a/us/_defaults.yaml": "defaults:\n  mysql_connections: 70\n",
		"a/us/t.yaml":         "tenants:\n  t1:\n    _silent_mode: disable\n",
	}
	if got := allFindings(t, "--config-dir", writeReservedConfD(t, after), "--warn-as-error"); len(got) != 0 {
		t.Errorf("after: findings = %q, want none", got)
	}
	_, b, _, _ := served(t, before, "2026-10-01T00:00:00Z")
	code, a, _, stderr := served(t, after, "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	if !reflect.DeepEqual(reservedValues(b.Tenants["t1"].Values), reservedValues(a.Tenants["t1"].Values)) {
		t.Errorf("served values changed:\n before %v\n after  %v", b.Tenants["t1"].Values, a.Tenants["t1"].Values)
	}
}

// reservedValues is the `_` keys of a served-values `values` map — the
// reserved settings, without the threshold rows a test tree may add to keep a
// subtree file non-empty.
func reservedValues(v map[string]any) map[string]any {
	out := map[string]any{}
	for k, x := range v {
		if strings.HasPrefix(k, "_") {
			out[k] = x
		}
	}
	return out
}

// #2388 A r2 (F5): ignored because the tenant sets the key itself, and no other
// tenant gets the file's value: its own entry is what is served — no "to have
// it take effect, set it in each tenant's own entry".
func TestGuard_SubtreeReservedTenantSetsItself(t *testing.T) {
	t.Parallel()
	files := reservedCase{defaultState: "enable", tenant: "\n    _silent_mode: critical"}.files()
	files["finance/_defaults.yaml"] = "defaults:\n  _silent_mode: warning\n"
	_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, files))
	if len(got) != 1 {
		t.Fatalf("findings = %+v, want 1", got)
	}
	m := got[0].Message
	if !strings.Contains(m, "This tenant's `tenants:` entry in `finance/t1.yaml` sets `_silent_mode`, and that is what the exporter serves") ||
		strings.Contains(m, "To have it take effect") {
		t.Errorf("message = %s", m)
	}
}

// #2388 A r3 (R2-2): a key the tenant "sets itself" through its `_profile`
// (profiles are applied before the overlay) is named as the profile's, not as
// the tenant's own entry; a key in the entry still is.
func TestGuard_SubtreeReservedSetByProfile(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, tenant, want, deny string
	}{
		{name: "profile", tenant: "\n    _profile: quiet",
			want: "This tenant's profile `quiet` (its `_profile`) sets `_silent_mode`", deny: "own entry"},
		{name: "entry", tenant: "\n    _profile: quiet\n    _silent_mode: critical",
			want: "This tenant's `tenants:` entry in `finance/t1.yaml` sets `_silent_mode`", deny: "profile `quiet`"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			files := reservedCase{defaultState: "enable", tenant: tc.tenant}.files()
			files["_profiles.yaml"] = "profiles:\n  quiet:\n    _silent_mode: warning\n"
			files["finance/_defaults.yaml"] = "defaults:\n  _silent_mode: warning\n"
			_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, files))
			if len(got) != 1 {
				t.Fatalf("findings = %+v, want 1", got)
			}
			if m := got[0].Message; !strings.Contains(m, tc.want) || strings.Contains(m, tc.deny) {
				t.Errorf("message = %s; want %q, not %q", m, tc.want, tc.deny)
			}
		})
	}
}

// #2388 A r4 (R3-2): a key set by a root platform file's `tenants:` block is
// named with that file, not as "own entry" (a reader would look in the tenant
// file and not find it).
func TestGuard_SubtreeReservedSetByPlatformFile(t *testing.T) {
	t.Parallel()
	files := reservedCase{defaultState: "enable", tenant: "\n    _profile: quiet"}.files()
	files["_profiles.yaml"] = "profiles:\n  quiet:\n    _silent_mode: warning\n"
	files["_platform.yaml"] = "tenants:\n  t1:\n    _silent_mode: critical\n"
	files["finance/_defaults.yaml"] = "defaults:\n  _silent_mode: warning\n"
	_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, files))
	if len(got) != 1 {
		t.Fatalf("findings = %+v, want 1", got)
	}
	if m := got[0].Message; !strings.Contains(m, "This tenant's `tenants:` entry in `_platform.yaml` sets `_silent_mode`") ||
		strings.Contains(m, "own entry sets") || strings.Contains(m, "profile `quiet`") {
		t.Errorf("message = %s", m)
	}
}

// #2388 A r4: the other-tenants list is capped at five names plus a count,
// from one index built per check (not a walk of every tenant per finding).
func TestGuard_SubtreeReservedSharedListCapped(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n" +
			"state_filters:\n  maintenance:\n    reasons: []\n    severity: warning\n    default_state: enable\n",
		"a/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
		"a/x.yaml":         "tenants:\n  x:\n    _state_maintenance: enable\n",
	}
	for i := 0; i < 8; i++ {
		files[fmt.Sprintf("a/t%d.yaml", i)] = fmt.Sprintf("tenants:\n  t%d: {}\n", i)
	}
	_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, files))
	var m string
	for _, f := range got {
		if f.TenantID == "x" {
			m = f.Message
		}
	}
	if !strings.Contains(m, "`t0`, `t1`, `t2`, `t3`, `t4` and 3 more") {
		t.Errorf("x message = %s", m)
	}
}

// #2388 A r5 (R4-1): the other-tenants count over two listed files — each
// tenant has one source file, so per-file counts add up — and this tenant is
// excluded from its own file's list by binary search.
func TestGuard_SubtreeReservedSharedCountTwoFiles(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n" +
			"state_filters:\n  maintenance:\n    reasons: []\n    severity: warning\n    default_state: enable\n",
		"a/_defaults.yaml":   "defaults:\n  _state_maintenance: disable\n",
		"a/b/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
		// x sets the key itself; it inherits both files.
		"a/b/x.yaml": "tenants:\n  x:\n    _state_maintenance: enable\n",
	}
	for i := 0; i < 4; i++ {
		files[fmt.Sprintf("a/p%d.yaml", i)] = fmt.Sprintf("tenants:\n  p%d: {}\n", i)   // source a/_defaults.yaml
		files[fmt.Sprintf("a/b/q%d.yaml", i)] = fmt.Sprintf("tenants:\n  q%d: {}\n", i) // source a/b/_defaults.yaml
	}
	_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, files))
	msg := map[string]string{}
	for _, f := range got {
		msg[f.TenantID] = f.Message
	}
	if m := msg["x"]; !strings.Contains(m, "`p0`, `p1`, `p2`, `p3`, `q0` and 3 more") {
		t.Errorf("x message = %s", m)
	}
	// q0 gets its value from a/b/ and its chain lists both files: the others
	// are p0–p3 (from a/) and q1–q3 (from a/b/) — 7, never q0 itself.
	if m := msg["q0"]; !strings.Contains(m, "`p0`, `p1`, `p2`, `p3`, `q1` and 2 more") || strings.Contains(m, "`q0`") {
		t.Errorf("q0 message = %s", m)
	}
}

// #2388 A r5 (R4-2): when several files' `tenants:` entries set the key, the
// message names them all and says the exporter serves the merged result — not
// "that one" value.
func TestGuard_SubtreeReservedSetByTwoEntries(t *testing.T) {
	t.Parallel()
	files := reservedCase{defaultState: "enable", tenant: "\n    _silent_mode: warning"}.files()
	files["_platform.yaml"] = "tenants:\n  t1:\n    _silent_mode: critical\n"
	files["finance/_defaults.yaml"] = "defaults:\n  _silent_mode: warning\n"
	_, got := reservedFindings(t, "--config-dir", writeReservedConfD(t, files))
	if len(got) != 1 {
		t.Fatalf("findings = %+v, want 1", got)
	}
	want := "This tenant's `tenants:` entries in `_platform.yaml`, `finance/t1.yaml` (the exporter serves the merged result) set `_silent_mode`"
	if !strings.Contains(got[0].Message, want) {
		t.Errorf("message = %s", got[0].Message)
	}
}
