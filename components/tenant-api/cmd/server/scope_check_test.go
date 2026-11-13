package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// ADR-027 §9 item 7: `tenant-api config check --kind scope` lists, for each
// scope axis, the rules that restrict it × the tenants they apply to that
// carry no label, and exits non-zero when there is any. The fixture tree is
// testdata/scope-check/tree with testdata/scope-check/rbac.yaml; each file's
// comment says what it covers. The
// broken tenant file is written by the test (scopeFixture), so no unparsable
// YAML is checked in.

// scopeFixtureClaims is the oauth2-proxy config that injects the org claim.
var scopeFixtureClaims = filepath.Join("testdata", "oauth2-proxy.yaml")

// scopeFixture copies testdata/scope-check/tree, plus testdata/scope-check/rbac.yaml
// as _rbac.yaml, into a temporary directory, minus the files named in drop,
// and writes extra files into it.
func scopeFixture(t *testing.T, drop []string, extra map[string]string) string {
	t.Helper()
	src := filepath.Join("testdata", "scope-check", "tree")
	entries, err := os.ReadDir(src)
	if err != nil {
		t.Fatal(err)
	}
	dir := t.TempDir()
	skip := map[string]bool{}
	for _, d := range drop {
		skip[d] = true
	}
	for _, e := range entries {
		if skip[e.Name()] {
			continue
		}
		b, err := os.ReadFile(filepath.Join(src, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, e.Name()), b, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if !skip["_rbac.yaml"] {
		b, err := os.ReadFile(filepath.Join("testdata", "scope-check", "rbac.yaml"))
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(filepath.Join(dir, "_rbac.yaml"), b, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	for name, body := range extra {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func runScope(t *testing.T, args ...string) (code int, stdout, stderr string) {
	t.Helper()
	var out, errb bytes.Buffer
	code = runConfigCmd(append([]string{"check", "--kind", "scope"}, args...), strings.NewReader(""), &out, &errb)
	return code, out.String(), errb.String()
}

// The whole document, byte for byte: every finding kind (an unlabeled tenant,
// and a tenant whose metadata cannot be read under each rule restricting a
// metadata axis), the order, the tenant problems and the summary.
// adr-check: ADR-027/9.7a
func TestConfigCheckScope_Fixture(t *testing.T) {
	t.Parallel()
	dir := scopeFixture(t, nil, map[string]string{
		// Not YAML: a tenant problem; its org axis is still judged.
		"tenant-echo.yaml": "tenants:\n  tenant-echo: [unclosed\n",
		// Declares another id: git-managed (id_mismatch), metadata unknown.
		"tenant-foxtrot.yaml": "tenants:\n  other-id:\n    _metadata:\n      environment: production\n",
	})
	code, out, errOut := runScope(t, "--config-dir", dir, "--oauth2-proxy-config", scopeFixtureClaims)
	if code != 1 {
		t.Fatalf("exit %d, want 1; stderr:\n%s\nstdout:\n%s", code, errOut, out)
	}
	want := `{
  "schema": "tenant-api.scope-check/v1",
  "findings": [
    {
      "axis": "environment",
      "rule": "env-operators",
      "rule_index": 0,
      "tenant": "tenant-bravo",
      "reason": "unlabeled"
    },
    {
      "axis": "environment",
      "rule": "env-operators",
      "rule_index": 0,
      "tenant": "tenant-echo",
      "reason": "metadata_unknown"
    },
    {
      "axis": "environment",
      "rule": "env-operators",
      "rule_index": 0,
      "tenant": "tenant-foxtrot",
      "reason": "metadata_unknown"
    },
    {
      "axis": "domain",
      "rule": "finance-readers",
      "rule_index": 1,
      "tenant": "tenant-charlie",
      "reason": "unlabeled"
    },
    {
      "axis": "domain",
      "rule": "finance-readers",
      "rule_index": 1,
      "tenant": "tenant-echo",
      "reason": "metadata_unknown"
    },
    {
      "axis": "domain",
      "rule": "finance-readers",
      "rule_index": 1,
      "tenant": "tenant-foxtrot",
      "reason": "metadata_unknown"
    },
    {
      "axis": "org",
      "rule": "org-writers",
      "rule_index": 2,
      "tenant": "tenant-delta",
      "reason": "unlabeled"
    },
    {
      "axis": "org",
      "rule": "org-writers",
      "rule_index": 2,
      "tenant": "tenant-echo",
      "reason": "unlabeled"
    }
  ],
  "tenant_problems": [
    {
      "tenant": "tenant-echo",
      "reason": "malformed_yaml",
      "git_managed_reason": "not_tenant_document"
    },
    {
      "tenant": "tenant-foxtrot",
      "reason": "metadata_incomplete",
      "git_managed_reason": "id_mismatch"
    }
  ],
  "config_problems": [],
  "summary": {
    "tenants": 7,
    "rules": 4,
    "findings": 8,
    "findings_by_axis": {
      "domain": 3,
      "environment": 3,
      "org": 2
    },
    "tenant_problems": 2,
    "config_problems": 0
  }
}
`
	if out != want {
		t.Errorf("output:\n%s\nwant:\n%s", out, want)
	}
}

// Every tenant a scoped rule applies to is labeled: exit 0.
func TestConfigCheckScope_CleanExitsZero(t *testing.T) {
	t.Parallel()
	dir := scopeFixture(t, []string{"tenant-bravo.yaml", "tenant-charlie.yaml", "tenant-delta.yaml"}, nil)
	code, out, errOut := runScope(t, "--config-dir", dir, "--oauth2-proxy-config", scopeFixtureClaims)
	if code != 0 {
		t.Fatalf("exit %d, want 0; stderr:\n%s\nstdout:\n%s", code, errOut, out)
	}
	if !strings.Contains(out, `"findings": [],`) || !strings.Contains(out, `"tenant_problems": [],`) {
		t.Errorf("want empty findings and tenant_problems:\n%s", out)
	}
}

// A tenant whose metadata cannot be read, under rules that restrict no
// metadata axis: the server grants it as before, so it is listed in
// tenant_problems for information and the exit code is 0.
func TestConfigCheckScope_TenantProblemAloneExitsZero(t *testing.T) {
	t.Parallel()
	dir := scopeFixture(t, []string{"tenant-bravo.yaml", "tenant-charlie.yaml", "tenant-delta.yaml"}, map[string]string{
		"_rbac.yaml":       "groups:\n  - name: ops\n    tenants: [\"*\"]\n    permissions: [read, write]\n",
		"tenant-echo.yaml": "tenants:\n  tenant-echo: [unclosed\n",
	})
	code, out, errOut := runScope(t, "--config-dir", dir, "--oauth2-proxy-config", scopeFixtureClaims)
	if code != 0 {
		t.Fatalf("exit %d, want 0; stderr:\n%s\nstdout:\n%s", code, errOut, out)
	}
	if !strings.Contains(out, `"findings": [],`) || !strings.Contains(out, `"tenant": "tenant-echo"`) {
		t.Errorf("want no finding and tenant-echo in tenant_problems:\n%s", out)
	}
}

// No _tenant_orgs.yaml in DIR is the empty mapping, as in the server: every
// tenant an org-scoped rule applies to is unlabeled. An explicit
// --tenant-orgs that does not exist cannot be opened.
func TestConfigCheckScope_TenantOrgsFile(t *testing.T) {
	t.Parallel()
	dir := scopeFixture(t, []string{"_tenant_orgs.yaml", "tenant-bravo.yaml", "tenant-charlie.yaml", "tenant-delta.yaml"}, nil)
	code, out, errOut := runScope(t, "--config-dir", dir, "--oauth2-proxy-config", scopeFixtureClaims)
	if code != 1 || !strings.Contains(out, `"tenant": "tenant-alpha"`) || !strings.Contains(out, `"axis": "org"`) {
		t.Fatalf("exit %d, want 1 with an org finding for tenant-alpha; stderr:\n%s\nstdout:\n%s", code, errOut, out)
	}

	code, _, errOut = runScope(t, "--config-dir", dir, "--oauth2-proxy-config", scopeFixtureClaims,
		"--tenant-orgs", filepath.Join(dir, "missing.yaml"))
	if code != 2 || !strings.Contains(errOut, "tenant-orgs") {
		t.Fatalf("explicit missing --tenant-orgs: exit %d, want 2; stderr:\n%s", code, errOut)
	}

	// --rbac and --tenant-orgs elsewhere than DIR.
	other := t.TempDir()
	orgs := filepath.Join(other, "orgs.yaml")
	if err := os.WriteFile(orgs, []byte("tenant_orgs:\n  tenant-alpha: [ORG-1]\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	rbacFile := filepath.Join(other, "rbac.yaml")
	b, err := os.ReadFile(filepath.Join(dir, "_rbac.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(rbacFile, b, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(dir, "_rbac.yaml")); err != nil {
		t.Fatal(err)
	}
	code, out, errOut = runScope(t, "--config-dir", dir, "--oauth2-proxy-config", scopeFixtureClaims,
		"--rbac", rbacFile, "--tenant-orgs", orgs)
	if code != 0 {
		t.Fatalf("--rbac/--tenant-orgs outside DIR: exit %d, want 0; stderr:\n%s\nstdout:\n%s", code, errOut, out)
	}
}

// An input the server would refuse is a config problem (exit 1) and no
// finding is judged.
func TestConfigCheckScope_ConfigProblems(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name   string
		claims string
		extra  map[string]string
		source string
	}{
		// org-scope on a claim that is not declared: the server refuses the
		// RBAC config.
		{"undeclared org-scope claim", "", nil, `"source": "rbac"`},
		{"tenant-orgs typo", scopeFixtureClaims,
			map[string]string{"_tenant_orgs.yaml": "tenant_org:\n  tenant-alpha: [ORG-1]\n"}, `"source": "tenant_orgs"`},
		// Two files for one tenant: the listing the server refuses.
		{"duplicate tenant file", scopeFixtureClaims,
			map[string]string{"tenant-alpha.yml": "tenants:\n  tenant-alpha: {}\n"}, `"source": "tenants"`},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			dir := scopeFixture(t, nil, c.extra)
			code, out, errOut := runScope(t, "--config-dir", dir, "--oauth2-proxy-config", c.claims)
			if code != 1 {
				t.Fatalf("exit %d, want 1; stderr:\n%s\nstdout:\n%s", code, errOut, out)
			}
			if !strings.Contains(out, c.source) || !strings.Contains(out, `"findings": [],`) {
				t.Errorf("want a %s config problem and no findings:\n%s", c.source, out)
			}
		})
	}
}

func TestConfigCheckScope_UsageAndUnopenable(t *testing.T) {
	t.Parallel()
	dir := scopeFixture(t, nil, nil)
	noRBAC := scopeFixture(t, []string{"_rbac.yaml"}, nil)
	cases := []struct {
		name string
		args []string
	}{
		{"no --config-dir", []string{"check", "--kind", "scope"}},
		{"positional FILE", []string{"check", "--kind", "scope", "--config-dir", dir, "x.yaml"}},
		{"missing dir", []string{"check", "--kind", "scope", "--config-dir", filepath.Join(dir, "nope")}},
		{"no _rbac.yaml", []string{"check", "--kind", "scope", "--config-dir", noRBAC}},
		{"stdin rbac", []string{"check", "--kind", "scope", "--config-dir", dir, "--rbac", "-"}},
		{"scope flag on --kind rbac", []string{"check", "--kind", "rbac", "--config-dir", dir, filepath.Join(dir, "_rbac.yaml")}},
		{"scope flag on --kind tenant-orgs", []string{"check", "--kind", "tenant-orgs", "--tenant-orgs", "x", filepath.Join(dir, "_tenant_orgs.yaml")}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			var out, errb bytes.Buffer
			if code := runConfigCmd(c.args, strings.NewReader(""), &out, &errb); code != 2 {
				t.Fatalf("exit %d, want 2; stderr:\n%s\nstdout:\n%s", code, errb.String(), out.String())
			}
			if out.Len() != 0 {
				t.Errorf("stdout on a usage/open error:\n%s", out.String())
			}
		})
	}
}
