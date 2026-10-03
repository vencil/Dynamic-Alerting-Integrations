package main

// subtree_undeliverable_test.go — #1976: a key only a subtree `_defaults.yaml`
// names is one the exporter serves no series for (it logs an ERROR and counts
// the tenant on da_config_subtree_undeliverable_tenants). da-guard reports it
// as a subtree_default_undeliverable warning (exit 1 under --warn-as-error)
// and `served-values` lists it in `unserved`. The control is the same tree
// with the key declared at the root: it is served, and nothing is reported.
//
// Seams: none — t.TempDir() trees through run().

import (
	"encoding/json"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/testutil"
)

// undeliverableFiles: tenant-a (finance/) inherits redis_evicted_keys from
// finance/_defaults.yaml only; tenant-b (ops/) inherits nothing below the root.
// rootDeclares adds the key to the root defaults (the control).
func undeliverableFiles(rootDeclares bool) map[string]string {
	root := "defaults:\n  mysql_connections: 80\n"
	if rootDeclares {
		root += "  redis_evicted_keys: 500\n"
	}
	return map[string]string{
		"_defaults.yaml":         root,
		"finance/_defaults.yaml": "defaults:\n  mysql_connections: 60\n  redis_evicted_keys: 100\n",
		"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
		"ops/tenant-b.yaml":      "tenants:\n  tenant-b: {}\n",
	}
}

func writeUndeliverableConfD(t *testing.T, rootDeclares bool) string {
	t.Helper()
	tmp := t.TempDir()
	tree := map[string]string{}
	for k, v := range undeliverableFiles(rootDeclares) {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	return filepath.Join(tmp, "conf.d")
}

// undeliverableFindings runs the guard with --format json and returns the exit code and
// the subtree_default_undeliverable findings.
func undeliverableFindings(t *testing.T, args ...string) (int, []guard.Finding) {
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
		if f.Kind == guard.FindingSubtreeDefaultUndeliverable {
			out = append(out, f)
		}
	}
	return code, out
}

func TestGuard_SubtreeOnlyKeyIsUndeliverableWarning(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableConfD(t, false)

	code, got := undeliverableFindings(t, "--config-dir", dir)
	if code != exitOK {
		t.Errorf("exit = %d, want %d (a warning does not fail the run)", code, exitOK)
	}
	if len(got) != 1 {
		t.Fatalf("subtree_default_undeliverable findings = %+v, want exactly one", got)
	}
	f := got[0]
	if f.Severity != guard.SeverityWarn || f.TenantID != "tenant-a" || f.Field != "redis_evicted_keys" {
		t.Errorf("finding = %+v, want warn / tenant-a / redis_evicted_keys", f)
	}

	if code, _ := undeliverableFindings(t, "--config-dir", dir, "--warn-as-error"); code != exitFindings {
		t.Errorf("--warn-as-error: exit = %d, want %d", code, exitFindings)
	}
}

func TestGuard_RootDeclaredKeyIsNotUndeliverable(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableConfD(t, true)
	code, got := undeliverableFindings(t, "--config-dir", dir, "--warn-as-error")
	if code != exitOK || len(got) != 0 {
		t.Errorf("exit = %d, findings = %+v; want exit 0 and none (the root declares the key)", code, got)
	}
}

func TestGuard_UndeliverableFollowsScope(t *testing.T) {
	t.Parallel()
	dir := writeUndeliverableConfD(t, false)
	for _, tc := range []struct {
		scope string
		want  int
	}{
		{scope: "finance", want: 1},
		{scope: "ops", want: 0}, // tenant-a is outside the scope
	} {
		t.Run(tc.scope, func(t *testing.T) {
			t.Parallel()
			code, got := undeliverableFindings(t, "--config-dir", dir, "--scope", tc.scope, "--warn-as-error")
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

func TestServedValues_UndeliverableKeyIsUnserved(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, undeliverableFiles(false), "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	tv := doc.Tenants["tenant-a"]
	if _, has := tv.Values["redis_evicted_keys"]; has {
		t.Errorf("values carries redis_evicted_keys: %v", tv.Values)
	}
	if want := map[string]any{"redis_evicted_keys": "100"}; !reflect.DeepEqual(tv.Unserved, want) {
		t.Errorf("tenant-a unserved = %v, want %v", tv.Unserved, want)
	}
	wantValue(t, doc, "tenant-a", "mysql_connections", 60)
	if u := doc.Tenants["tenant-b"].Unserved; len(u) != 0 {
		t.Errorf("tenant-b unserved = %v, want none", u)
	}
}

// #1976 r2/r3: which refused keys the finding and `unserved` report. Reserved
// keys and keys the exporter never serves as a row (#2388's) and keys the
// subtree switches off are not reported — the exporter's build still refuses
// them (its ERROR and gauge are unchanged). An unrecognised `_` key is a
// threshold to the exporter and is reported; declared at the root (the
// finding's fix), the tenant serves the subtree's value.
func TestUndeliverable_SubtreeKeysReportedOrNot(t *testing.T) {
	t.Parallel()
	const root = "defaults:\n  mysql_connections: 80\n"
	const stateFilters = "state_filters:\n  maintenance:\n    reasons: [\"x\"]\n    severity: warning\n"
	const scheduled = "  _state_maintenance:\n    default: enable\n    overrides:\n" +
		"      - window: \"00:00-23:59\"\n        value: disable\n"
	subtree := func(rootBody, sub string) map[string]string {
		return map[string]string{
			"_defaults.yaml":         rootBody,
			"finance/_defaults.yaml": sub,
			"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
		}
	}
	for _, tc := range []struct {
		name         string
		files        map[string]string
		wantFindings []string           // Fields of tenant-a's findings
		wantUnserved map[string]any     // tenant-a's unserved; nil = none
		wantValues   map[string]float64 // tenant-a values that must be served
	}{
		{
			name:  "silent-bogus",
			files: subtree(root, "defaults:\n  _silent_bogus: 5\n"),
		},
		{
			// The review's t3: a subtree switching a key off.
			name:  "switched-off",
			files: subtree(root, "defaults:\n  redis_evicted_keys: disable\n"),
		},
		{
			// The review's t6.
			name:         "unrecognised-underscore-key",
			files:        subtree(root+"  _myth: 5\n", "defaults:\n  _myth2: 7\n"),
			wantFindings: []string{"_myth2"},
			wantUnserved: map[string]any{"_myth2": "7"},
		},
		{
			// The review's t7 without tenant-b: `_myth2` declared at the root
			// is delivered (the subtree's 7). `_silent_bogus` declared at the
			// root lands in the tenant's map but serves no row, so it is in
			// `unserved` through the merged-map path that predates #1976 (no
			// finding: it is not refused).
			name:         "unrecognised-underscore-key-declared",
			files:        subtree(root+"  _myth2: 1\n  _silent_bogus: 1\n", "defaults:\n  _myth2: 7\n  _silent_bogus: 7\n"),
			wantUnserved: map[string]any{"_silent_bogus": "7"},
			wantValues:   map[string]float64{"_myth2": 7},
		},
		{
			// `_metadata`, a scheduled `_state_maintenance` and `_profile`,
			// none declared in optional_overrides: all three are refused.
			name: "reserved-keys",
			files: map[string]string{
				"_defaults.yaml":         "defaults:\n  mysql_connections: 80\n" + stateFilters,
				"finance/_defaults.yaml": "defaults:\n  _metadata: 5\n" + scheduled + "  _profile: disable\n",
				"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
			},
		},
		{
			// The same keys with `_state_maintenance` and `_metadata` in
			// optional_overrides (the review's exB).
			name: "reserved-keys-declared",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  mysql_connections: 80\n" + stateFilters +
					"optional_overrides:\n  - _state_maintenance\n  - _metadata\n",
				"finance/_defaults.yaml": "defaults:\n  _metadata: 5\n" + scheduled + "  _profile: disable\n",
				"finance/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
			},
		},
		{
			// A schedule at finance/ (refused), a scalar below (delivered):
			// the shallow refusal stays in the exporter's set (the review's
			// exC); it is not reported here.
			name: "schedule-then-scalar-state",
			files: map[string]string{
				"_defaults.yaml":            "defaults:\n  mysql_connections: 80\n" + stateFilters,
				"finance/_defaults.yaml":    "defaults:\n" + scheduled,
				"finance/us/_defaults.yaml": "defaults:\n  _state_maintenance: disable\n",
				"finance/us/tenant-a.yaml":  "tenants:\n  tenant-a: {}\n",
			},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			tmp := t.TempDir()
			tree := map[string]string{}
			for k, v := range tc.files {
				tree["conf.d/"+k] = v
			}
			testutil.WriteTree(t, tmp, tree)
			dir := filepath.Join(tmp, "conf.d")

			code, got := undeliverableFindings(t, "--config-dir", dir, "--warn-as-error")
			var fields []string
			for _, f := range got {
				if f.TenantID != "tenant-a" {
					t.Errorf("finding for another tenant: %+v", f)
				}
				fields = append(fields, f.Field)
			}
			if !reflect.DeepEqual(fields, tc.wantFindings) {
				t.Errorf("finding fields = %v, want %v", fields, tc.wantFindings)
			}
			wantCode := exitOK
			if len(tc.wantFindings) > 0 {
				wantCode = exitFindings
			}
			if code != wantCode {
				t.Errorf("--warn-as-error: exit = %d, want %d", code, wantCode)
			}

			code, doc, _, stderr := served(t, tc.files, "2026-10-01T00:00:00Z")
			mustOK(t, code, stderr)
			u := doc.Tenants["tenant-a"].Unserved
			if len(u) == 0 {
				u = nil
			}
			if !reflect.DeepEqual(u, tc.wantUnserved) {
				t.Errorf("tenant-a unserved = %v, want %v", u, tc.wantUnserved)
			}
			for k, v := range tc.wantValues {
				wantValue(t, doc, "tenant-a", k, v)
			}
		})
	}
}

func TestServedValues_RootDeclaredKeyIsServed(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, undeliverableFiles(true), "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "redis_evicted_keys", 100)
	if u := doc.Tenants["tenant-a"].Unserved; len(u) != 0 {
		t.Errorf("tenant-a unserved = %v, want none", u)
	}
}
