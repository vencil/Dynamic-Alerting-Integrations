package main

// root_null_undeclared_test.go — #2518: a threshold the conf.d root
// `_defaults.yaml` writes as null is no write, so the root does not declare
// it and a tenant's own value for it is not served (before #2518 the null
// was a 0 that declared the key, and the tenant's value WAS served).
// da-guard names each such (tenant, key) as root_default_null_undeclared;
// `served-values` lists the key in `unserved`. Following either half of the
// finding's fix serves the tenant's value and clears the finding.
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

// rootNullFiles is the measured shape: root `mysql_connections: null`, tenant
// tx under sub/ setting 50; ty sets nothing. root replaces the root file.
func rootNullFiles(root string) map[string]string {
	return map[string]string{
		"_defaults.yaml": root,
		"sub/tx.yaml":    "tenants:\n  tx:\n    mysql_connections: 50\n",
		"sub/ty.yaml":    "tenants:\n  ty: {}\n",
	}
}

const rootNullDefaults = "defaults:\n  mysql_connections: null\n  mysql_slow_queries: 5\n"

// guardFindingsOf runs the guard (--format json) over files and returns the
// exit code and every finding.
func guardFindingsOf(t *testing.T, files map[string]string, args ...string) (int, []guard.Finding) {
	t.Helper()
	tmp := t.TempDir()
	tree := map[string]string{}
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, tmp, tree)
	argv := append([]string{"--format", "json", "--config-dir", filepath.Join(tmp, "conf.d")}, args...)
	code, stdout, stderr := runOnce(t, argv...)
	var doc struct {
		Report *guard.GuardReport `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("stdout is not JSON (exit %d): %v\nstderr=%s", code, err, stderr)
	}
	return code, doc.Report.Findings
}

func TestGuard_RootNullTenantValueIsNamed(t *testing.T) {
	t.Parallel()
	code, got := guardFindingsOf(t, rootNullFiles(rootNullDefaults))
	if code != exitOK {
		t.Errorf("exit = %d, want %d (a warning does not fail the run)", code, exitOK)
	}
	if len(got) != 1 {
		t.Fatalf("findings = %+v, want exactly one", got)
	}
	f := got[0]
	if f.Kind != guard.FindingRootDefaultNullUndeclared || f.Severity != guard.SeverityWarn ||
		f.TenantID != "tx" || f.Field != "mysql_connections" {
		t.Errorf("finding = %+v, want warn root_default_null_undeclared / tx / mysql_connections", f)
	}
	for _, want := range []string{"writes it as null", "optional_overrides:"} {
		if !strings.Contains(f.Message, want) {
			t.Errorf("message lacks %q: %s", want, f.Message)
		}
	}
	if code, _ := guardFindingsOf(t, rootNullFiles(rootNullDefaults), "--warn-as-error"); code != exitFindings {
		t.Errorf("--warn-as-error: exit = %d, want %d", code, exitFindings)
	}

	// served-values: not served, listed in unserved (the same build).
	code, doc, _, stderr := served(t, rootNullFiles(rootNullDefaults), "2026-10-01T00:00:00Z")
	mustOK(t, code, stderr)
	tv := doc.Tenants["tx"]
	if _, has := tv.Values["mysql_connections"]; has {
		t.Errorf("values carries mysql_connections: %v", tv.Values)
	}
	if want := map[string]any{"mysql_connections": "50"}; !reflect.DeepEqual(tv.Unserved, want) {
		t.Errorf("tx unserved = %v, want %v", tv.Unserved, want)
	}
}

// The finding's fix, each half: a number at the root, or the key listed
// under optional_overrides (with the null line kept or removed). Each serves
// the tenant's 50 and leaves no finding.
func TestGuard_RootNullFixServesTheTenantValue(t *testing.T) {
	t.Parallel()
	for name, root := range map[string]string{
		"root number":                 "defaults:\n  mysql_connections: 30\n  mysql_slow_queries: 5\n",
		"optional_overrides, null":    rootNullDefaults + "optional_overrides: [mysql_connections]\n",
		"optional_overrides, no null": "defaults:\n  mysql_slow_queries: 5\noptional_overrides: [mysql_connections]\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			if code, got := guardFindingsOf(t, rootNullFiles(root), "--warn-as-error"); code != exitOK || len(got) != 0 {
				t.Errorf("exit = %d, findings = %+v; want 0 and none", code, got)
			}
			code, doc, _, stderr := served(t, rootNullFiles(root), "2026-10-01T00:00:00Z")
			mustOK(t, code, stderr)
			wantValue(t, doc, "tx", "mysql_connections", 50)
		})
	}
}

// One cause, one finding: a value only a subtree `_defaults.yaml` hands down
// under a root null is subtree_default_undeliverable's, never this one's;
// a tenant writing its own value over that subtree is this one's alone; a
// tenant switching the key off is not reported; the other spelling of the
// root null is matched.
func TestGuard_RootNullOneFindingPerCause(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name   string
		files  map[string]string
		kind   guard.FindingKind // "" = no finding
		field  string
		tenant string
	}{
		{"subtree value only", map[string]string{
			"_defaults.yaml":     rootNullDefaults,
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
			"sub/tx.yaml":        "tenants:\n  tx: {}\n",
		}, guard.FindingSubtreeDefaultUndeliverable, "mysql_connections", "tx"},
		{"tenant value over a subtree value", map[string]string{
			"_defaults.yaml":     rootNullDefaults,
			"sub/_defaults.yaml": "defaults:\n  mysql_connections: 40\n",
			"sub/tx.yaml":        "tenants:\n  tx:\n    mysql_connections: 50\n",
		}, guard.FindingRootDefaultNullUndeclared, "mysql_connections", "tx"},
		{"platform entry value", map[string]string{
			"_defaults.yaml": rootNullDefaults,
			"_platform.yaml": "tenants:\n  tx:\n    mysql_connections: 70\n",
			"tx.yaml":        "tenants:\n  tx: {}\n",
		}, guard.FindingRootDefaultNullUndeclared, "mysql_connections", "tx"},
		{"tenant switches it off", map[string]string{
			"_defaults.yaml": rootNullDefaults,
			"tx.yaml":        "tenants:\n  tx:\n    mysql_connections: disable\n",
		}, "", "", ""},
		{"root legacy null, tenant canonical", map[string]string{
			"_defaults.yaml": "defaults:\n  " + aliasLegacy + ": null\n  mysql_slow_queries: 5\n",
			"tx.yaml":        "tenants:\n  tx:\n    " + aliasCanon + ": 50\n",
		}, guard.FindingRootDefaultNullUndeclared, aliasCanon, "tx"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			_, got := guardFindingsOf(t, tc.files)
			if tc.kind == "" {
				if len(got) != 0 {
					t.Errorf("findings = %+v, want none", got)
				}
				return
			}
			if len(got) != 1 || got[0].Kind != tc.kind || got[0].Field != tc.field || got[0].TenantID != tc.tenant {
				t.Errorf("findings = %+v, want exactly one %s / %s / %s", got, tc.kind, tc.tenant, tc.field)
			}
		})
	}
}
