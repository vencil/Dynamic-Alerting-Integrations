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

func TestServedValues_RootDeclaredKeyIsServed(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, undeliverableFiles(true), "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "redis_evicted_keys", 100)
	if u := doc.Tenants["tenant-a"].Unserved; len(u) != 0 {
		t.Errorf("tenant-a unserved = %v, want none", u)
	}
}
