package main

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"
)

type compileDocForTest struct {
	Schema string `json:"schema"`
	Groups []struct {
		Name  string           `json:"name"`
		Rules []map[string]any `json:"rules"`
	} `json:"groups"`
	Shapes      int                `json:"shapes"`
	NotCompiled []notCompiledEntry `json:"not_compiled"`
	BlockErrors map[string]string  `json:"block_errors"`
	Lifecycle   []lifecycleUse     `json:"lifecycle"`
	ParseFailed []string           `json:"parse_failed"`
	Skipped     []skippedFile      `json:"skipped"`
	Unreadable  []skippedFile      `json:"unreadable"`
}

func runCompile(t *testing.T, files map[string]string) (int, compileDocForTest, string) {
	t.Helper()
	code, stdout, stderr := runOnce(t, compileCustomAlertsCmd, "--config-dir", writeParityTree(t, files))
	var doc compileDocForTest
	if stdout != "" {
		if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
			t.Fatalf("stdout is not JSON (%v): %q; stderr=%q", err, stdout, stderr)
		}
	}
	return code, doc, stderr
}

const compileDefaults = "defaults:\n  cpu: 70\n"

// The served entry compiles, the switched-off one too; a rejected and a
// duplicate entry are named in not_compiled, a list that does not decode in
// block_errors, a non-active recipe in lifecycle.
func TestCompileCustomAlerts_Document(t *testing.T) {
	t.Parallel()
	code, doc, stderr := runCompile(t, map[string]string{
		"_defaults.yaml": compileDefaults,
		"a.yaml": "tenants:\n  a:\n    _custom_alerts:\n" +
			"      - {recipe: threshold, name: q_high, metric: qd, op: \">\", window: 5m, threshold: \"100\"}\n" +
			"      - {recipe: threshold, name: q_off, metric: qe, op: \">\", window: 5m, threshold: disable}\n" +
			"      - {recipe: threshold, name: q_bad, metric: \"no-dash\", op: \">\", window: 5m, threshold: \"1\"}\n" +
			"      - {recipe: threshold, name: q_dup, metric: qd, op: \">\", window: 5m, threshold: \"200\"}\n",
		"b.yaml": "tenants:\n  b:\n    _custom_alerts: \"not a list\"\n",
	})
	if code != exitOK {
		t.Fatalf("exit = %d, stderr=%q", code, stderr)
	}
	if doc.Schema != compileCustomAlertsSchema || doc.Shapes != 2 {
		t.Fatalf("schema/shapes = %q/%d, want %q/2", doc.Schema, doc.Shapes, compileCustomAlertsSchema)
	}
	statuses := map[string]string{}
	for _, e := range doc.NotCompiled {
		statuses[e.Name] = e.Status
	}
	if statuses["q_bad"] != "rejected" || statuses["q_dup"] != "duplicate" || len(statuses) != 2 {
		t.Errorf("not_compiled = %+v, want q_bad rejected and q_dup duplicate", doc.NotCompiled)
	}
	if doc.BlockErrors["b"] == "" || len(doc.BlockErrors) != 1 {
		t.Errorf("block_errors = %v, want b's", doc.BlockErrors)
	}
	var names []string
	for _, g := range doc.Groups {
		names = append(names, g.Name)
	}
	if strings.Join(names, ",") != "custom-alerts-normalization,custom-alerts-info,custom-alerts" {
		t.Errorf("groups = %v", names)
	}
	// threshold__qd and threshold__qe, each with a warning alert, plus the
	// sentinel ahead of them.
	alerts := doc.Groups[2].Rules
	if len(alerts) != 3 || alerts[0]["alert"] != "CustomRecipeSilent" {
		t.Errorf("alert group = %v", alerts)
	}
}

// A tree whose /metrics cannot be gathered serves no row, so a pack compiled
// from it would describe rules nothing feeds: refused (exit 2), as the guard
// and served-values refuse it. Before #3229 step 2 the compiler read the tree
// through served-values and inherited this; a compile that only listed the
// entries would have passed it.
func TestCompileCustomAlerts_RefusesATreeMetricsCannotGather(t *testing.T) {
	t.Parallel()
	code, _, stderr := runCompile(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"a.yaml": "tenants:\n  a:\n    mysql_connections: \"70:critical\"\n    mysql_connections_critical: 95\n" +
			"    _custom_alerts:\n" +
			"      - {recipe: threshold, name: q_high, metric: qd, op: \">\", window: 5m, threshold: \"100\"}\n",
	})
	if code != exitCallerErr || !strings.Contains(stderr, "cannot be gathered") {
		t.Fatalf("exit = %d stderr = %q, want %d naming the gather", code, stderr, exitCallerErr)
	}
}

// A file the load cannot decode: the rest of the tree still compiles, the
// file is named in parse_failed, and the exit is 3 so a caller that ignores
// the field still fails.
func TestCompileCustomAlerts_ParseFailedIsExitThreeWithTheRestCompiled(t *testing.T) {
	t.Parallel()
	code, doc, stderr := runCompile(t, map[string]string{
		"_defaults.yaml": compileDefaults,
		"a.yaml": "tenants:\n  a:\n    _custom_alerts:\n" +
			"      - {recipe: threshold, name: q_high, metric: qd, op: \">\", window: 5m, threshold: \"100\"}\n",
		"broken.yaml": "tenants: [\n",
	})
	if code != exitParseFailed {
		t.Fatalf("exit = %d, want %d; stderr=%q", code, exitParseFailed, stderr)
	}
	if len(doc.ParseFailed) != 1 || doc.Shapes != 1 {
		t.Fatalf("parse_failed = %v shapes = %d, want broken.yaml and 1 shape", doc.ParseFailed, doc.Shapes)
	}
}

// No custom alert anywhere: an empty pack, not an error.
func TestCompileCustomAlerts_NoCustomAlertsIsAnEmptyPack(t *testing.T) {
	t.Parallel()
	code, doc, stderr := runCompile(t, map[string]string{
		"_defaults.yaml": compileDefaults,
		"a.yaml":         "tenants:\n  a:\n    cpu: 80\n",
	})
	if code != exitOK || doc.Shapes != 0 || len(doc.Groups) != 0 || doc.NotCompiled == nil || doc.Lifecycle == nil {
		t.Fatalf("exit = %d doc = %+v stderr=%q", code, doc, stderr)
	}
}

func TestCompileCustomAlerts_RequiresConfigDir(t *testing.T) {
	t.Parallel()
	code, _, stderr := runOnce(t, compileCustomAlertsCmd)
	if code != exitCallerErr || !strings.Contains(stderr, "--config-dir is required") {
		t.Fatalf("exit = %d stderr = %q", code, stderr)
	}
	if code, _, _ := runOnce(t, compileCustomAlertsCmd, "-h"); code != exitOK {
		t.Fatalf("-h exit = %d", code)
	}
}

// A recipe the contract marks deprecated or eol keeps compiling, and is named
// in lifecycle with the tenants a compiled entry declares it for — a rejected
// entry's tenant is not one of them. Not parallel: it swaps recipeStatus.
func TestCompileCustomAlerts_LifecycleNamesNonActiveRecipesInUse(t *testing.T) {
	orig := recipeStatus
	t.Cleanup(func() { recipeStatus = orig })
	recipeStatus = func(recipe string) string {
		switch recipe {
		case "rate":
			return "deprecated"
		case "absence":
			return "eol"
		}
		return orig(recipe)
	}
	code, doc, stderr := runCompile(t, map[string]string{
		"_defaults.yaml": compileDefaults,
		"a.yaml": "tenants:\n  b:\n    _custom_alerts:\n" +
			"      - {recipe: rate, name: r, metric: rr, op: \">\", window: 5m, threshold: \"1\"}\n" +
			"      - {recipe: absence, name: gone, metric: up_x, window: 10m, threshold: \"1\"}\n" +
			"  a:\n    _custom_alerts:\n" +
			"      - {recipe: rate, name: r, metric: rr, op: \">\", window: 5m, threshold: \"1\"}\n" +
			"  c:\n    _custom_alerts:\n" +
			"      - {recipe: rate, name: bad, metric: \"no-dash\", op: \">\", window: 5m, threshold: \"1\"}\n",
	})
	if code != exitOK {
		t.Fatalf("exit = %d, stderr=%q", code, stderr)
	}
	want := []lifecycleUse{
		{Recipe: "rate", Status: "deprecated", Tenants: []string{"a", "b"}},
		{Recipe: "absence", Status: "eol", Tenants: []string{"b"}},
	}
	if !reflect.DeepEqual(doc.Lifecycle, want) {
		t.Fatalf("lifecycle = %+v, want %+v", doc.Lifecycle, want)
	}
	if doc.Shapes != 2 {
		t.Fatalf("shapes = %d, want 2 (deprecated and eol recipes still compile)", doc.Shapes)
	}
}
