package handler

// ADR-027 §7 / §9 item 6: a write by a caller who is not a platform
// administrator (rbac.Manager.PlatformAdminNonOrgScoped) that would change a
// tenant's environment or domain label is refused, 403 FORBIDDEN — on every
// write path that rewrites a tenant file (PUT, the tenant batch; the group
// batch runs the same loops), and in PR mode on the fresh base the branch is
// cut from, not only on the pod's local tree.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

const (
	lgTenant    = "tenant-alpha" // labeled in its own `_metadata`
	lgProfiled  = "tenant-beta"  // labeled through its profile
	lgNewTenant = "tenant-gamma" // no file

	lgWriters   = "lg-writers"   // write on "*", not an admin
	lgPlatform  = "lg-platform"  // admin on "*", no org-scope: a platform administrator
	lgOrgAdmins = "lg-org-admin" // admin on "*" but org-scoped: not one
	lgOrg       = "ORG-LG"
	lgOrgHeader = "X-LG-Org"

	lgDeniedFrag = "only a platform administrator may change the environment or domain labels"
)

const lgRBAC = `groups:
  - name: ` + lgWriters + `
    tenants: ["*"]
    permissions: [read, write]
  - name: ` + lgPlatform + `
    tenants: ["*"]
    permissions: [read, write, admin]
  - name: ` + lgOrgAdmins + `
    tenants: ["*"]
    permissions: [read, write, admin]
    org-scope: org
`

const (
	lgDefaults = "defaults:\n  mysql_connections: 80\n"
	lgProfiles = "profiles:\n" +
		"  prodfin:\n    _metadata:\n      environment: production\n      domain: finance\n" +
		"  prodfin2:\n    _metadata:\n      environment: production\n      domain: finance\n" +
		"  devfin:\n    _metadata:\n      environment: dev\n      domain: finance\n"
	lgBeta = "tenants:\n  " + lgProfiled + ":\n    _profile: prodfin\n    mysql_connections: '80'\n"
)

// lgTenantYAML is lgTenant's document with meta as its `_metadata` block
// ("" for none) and mysql_connections set to conn.
func lgTenantYAML(meta, conn string) string {
	s := "tenants:\n  " + lgTenant + ":\n"
	if meta != "" {
		s += "    _metadata:\n" + meta
	}
	return s + "    mysql_connections: '" + conn + "'\n"
}

const lgProdFinance = "      environment: production\n      domain: finance\n"

func lgTree() map[string]string {
	return map[string]string{
		"_defaults.yaml":     lgDefaults,
		"_profiles.yaml":     lgProfiles,
		lgTenant + ".yaml":   lgTenantYAML(lgProdFinance, "80"),
		lgProfiled + ".yaml": lgBeta,
	}
}

// lgRBACManager is lgRBAC with the org claim declared and org-scope
// enforced, and the tenant/org mapping that puts every fixture tenant in the
// org-scoped caller's organization — so an org-scoped refusal below is the
// label gate's, never the org axis's.
func lgRBACManager(t *testing.T) (*rbac.Manager, *tenantorg.Manager, map[string]string) {
	t.Helper()
	claims := map[string]string{"org": lgOrgHeader}
	mgr := newRBACManagerWithClaims(t, lgRBAC, claims)
	orgs := tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
		lgTenant: {lgOrg}, lgProfiled: {lgOrg}, lgNewTenant: {lgOrg},
	}})
	return mgr, orgs, claims
}

// lgDirectDeps: a git-backed direct-mode Deps over lgTree.
func lgDirectDeps(t *testing.T) *Deps {
	t.Helper()
	dir := seedGitTree(t, lgTree())
	mgr, orgs, claims := lgRBACManager(t)
	return &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: mgr, TenantOrg: orgs, ClaimHeaders: claims,
		Groups: groups.NewDirManager(dir), WriteMode: WriteModeDirect}
}

// lgPRFixture: a PR-mode stale fixture (the local tree is lgTree, origin has
// moved on by originFiles) with lgRBAC.
func lgPRFixture(t *testing.T, originFiles map[string]string) *staleFixture {
	t.Helper()
	f := newStaleFixture(t, lgTree(), originFiles)
	mgr, orgs, claims := lgRBACManager(t)
	f.deps.RBAC, f.deps.TenantOrg, f.deps.ClaimHeaders = mgr, orgs, claims
	return f
}

func lgIdentity(req *http.Request, group string) {
	req.Header.Set("X-Forwarded-Email", "lg@example.com")
	req.Header.Set("X-Forwarded-Groups", group)
	req.Header.Set(lgOrgHeader, lgOrg)
}

func lgPut(t *testing.T, d *Deps, group, tenantID, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+tenantID, "id", tenantID, bytes.NewBufferString(body))
	lgIdentity(req, group)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(d), d.RBAC, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
	return w
}

func lgBatch(t *testing.T, d *Deps, group, ops string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(`{"operations":`+ops+`}`))
	req.Header.Set("Content-Type", "application/json")
	lgIdentity(req, group)
	w := httptest.NewRecorder()
	d.RBAC.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)
	return w
}

// lgAssertLabelDenied: w is the PUT label refusal for tenantID.
func lgAssertLabelDenied(t *testing.T, w *httptest.ResponseRecorder, tenantID string) {
	t.Helper()
	var env map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &env)
	msg, _ := env["error"].(string)
	if w.Code != http.StatusForbidden || env["code"] != string(errcode.Forbidden) ||
		!strings.Contains(msg, lgDeniedFrag) || !strings.Contains(msg, tenantID) {
		t.Fatalf("status %d, body %s; want 403 %s naming the label rule for %s", w.Code, w.Body.String(), errcode.Forbidden, tenantID)
	}
}

func lgReadFile(t *testing.T, dir, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(dir, name))
	if err != nil {
		if os.IsNotExist(err) {
			return ""
		}
		t.Fatal(err)
	}
	return string(b)
}

// ── PUT, direct mode ───────────────────────────────────────────────────────

func TestLabelGuardPutDirect(t *testing.T) {
	t.Parallel()
	for _, c := range []struct {
		name, group, body string
		allowed           bool
	}{
		{"writer changes environment", lgWriters,
			lgTenantYAML("      environment: staging\n      domain: finance\n", "90"), false},
		{"writer changes domain", lgWriters,
			lgTenantYAML("      environment: production\n      domain: retail\n", "90"), false},
		{"writer removes both labels", lgWriters, lgTenantYAML("", "90"), false},
		{"writer removes one label", lgWriters, lgTenantYAML("      environment: production\n", "90"), false},
		{"writer relabels through a profile", lgWriters,
			"tenants:\n  " + lgTenant + ":\n    _profile: devfin\n    mysql_connections: '90'\n", false},
		{"writer keeps the labels", lgWriters, lgTenantYAML(lgProdFinance, "90"), true},
		{"writer keeps the labels through a profile that says the same", lgWriters,
			"tenants:\n  " + lgTenant + ":\n    _profile: prodfin\n    mysql_connections: '90'\n", true},
		{"org-scoped * admin changes environment", lgOrgAdmins,
			lgTenantYAML("      environment: staging\n      domain: finance\n", "90"), false},
		{"org-scoped * admin keeps the labels", lgOrgAdmins, lgTenantYAML(lgProdFinance, "90"), true},
		{"platform admin changes environment", lgPlatform,
			lgTenantYAML("      environment: staging\n      domain: finance\n", "90"), true},
		{"platform admin removes the labels", lgPlatform, lgTenantYAML("", "90"), true},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			d := lgDirectDeps(t)
			before := lgReadFile(t, d.ConfigDir, lgTenant+".yaml")
			w := lgPut(t, d, c.group, lgTenant, c.body)
			after := lgReadFile(t, d.ConfigDir, lgTenant+".yaml")
			if !c.allowed {
				lgAssertLabelDenied(t, w, lgTenant)
				if after != before {
					t.Errorf("the tenant file changed on a refused write:\n%s", after)
				}
				return
			}
			if w.Code != http.StatusOK {
				t.Fatalf("status %d, body %s; want 200", w.Code, w.Body.String())
			}
			if after != c.body {
				t.Errorf("tenant file = %q, want the body %q", after, c.body)
			}
		})
	}
}

// Creating a tenant: from no file to labels is a change.
func TestLabelGuardPutNewTenant(t *testing.T) {
	t.Parallel()
	labeled := "tenants:\n  " + lgNewTenant + ":\n    _metadata:\n      environment: production\n    mysql_connections: '80'\n"
	unlabeled := "tenants:\n  " + lgNewTenant + ":\n    mysql_connections: '80'\n"
	for _, c := range []struct {
		name, group, body string
		allowed           bool
	}{
		{"writer creates a labeled tenant", lgWriters, labeled, false},
		{"writer creates an unlabeled tenant", lgWriters, unlabeled, true},
		{"platform admin creates a labeled tenant", lgPlatform, labeled, true},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			d := lgDirectDeps(t)
			w := lgPut(t, d, c.group, lgNewTenant, c.body)
			got := lgReadFile(t, d.ConfigDir, lgNewTenant+".yaml")
			if !c.allowed {
				lgAssertLabelDenied(t, w, lgNewTenant)
				if got != "" {
					t.Errorf("a refused create left a file:\n%s", got)
				}
				return
			}
			if w.Code != http.StatusOK || got != c.body {
				t.Fatalf("status %d, file %q, body %s; want 200 and the body written", w.Code, got, w.Body.String())
			}
		})
	}
}

// ── Tenant batch, direct mode ──────────────────────────────────────────────

func TestLabelGuardBatchDirect(t *testing.T) {
	t.Parallel()
	// op 0 leaves every label alone; op 1 is the case.
	const op0 = `{"tenant_id":"` + lgTenant + `","patch":{"mysql_connections":"95"}}`
	for _, c := range []struct {
		name, group, profile string
		allowed              bool
	}{
		{"writer moves the profile to another environment", lgWriters, "devfin", false},
		{"writer moves the profile to one with the same labels", lgWriters, "prodfin2", true},
		{"org-scoped * admin moves the profile", lgOrgAdmins, "devfin", false},
		{"platform admin moves the profile", lgPlatform, "devfin", true},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			d := lgDirectDeps(t)
			beforeAlpha := lgReadFile(t, d.ConfigDir, lgTenant+".yaml")
			ops := `[` + op0 + `,{"tenant_id":"` + lgProfiled + `","patch":{"_profile":"` + c.profile + `"}}]`
			w := lgBatch(t, d, c.group, ops)
			beta := lgReadFile(t, d.ConfigDir, lgProfiled+".yaml")
			if !c.allowed {
				// #2900: one refused op rejects the whole batch (422), the
				// refusal on that op, the others not applied.
				assertRejectedOp(t, w, 1, lgProfiled, string(errcode.Forbidden), lgDeniedFrag)
				var resp BatchResponse
				_ = json.Unmarshal(w.Body.Bytes(), &resp)
				if resp.Results[0].Status != resultNotApplied {
					t.Errorf("op 0 = %+v; want %s", resp.Results[0], resultNotApplied)
				}
				if beta != lgBeta || lgReadFile(t, d.ConfigDir, lgTenant+".yaml") != beforeAlpha {
					t.Errorf("a rejected batch wrote: %s / %s", beta, lgReadFile(t, d.ConfigDir, lgTenant+".yaml"))
				}
				return
			}
			if w.Code != http.StatusOK || !strings.Contains(beta, `_profile: "`+c.profile+`"`) {
				t.Fatalf("status %d, %s = %q, body %s; want 200 and the profile written", w.Code, lgProfiled, beta, w.Body.String())
			}
		})
	}
}

// A tenant's later op is judged against its earlier ops: moving away and
// back in one request changes the labels on the first op.
func TestLabelGuardBatchStackedOps(t *testing.T) {
	t.Parallel()
	d := lgDirectDeps(t)
	ops := `[{"tenant_id":"` + lgProfiled + `","patch":{"_profile":"devfin"}},` +
		`{"tenant_id":"` + lgProfiled + `","patch":{"_profile":"prodfin"}}]`
	w := lgBatch(t, d, lgWriters, ops)
	assertRejectedOp(t, w, 0, lgProfiled, string(errcode.Forbidden), lgDeniedFrag)
}

// ── PR mode: the verdict on the fresh base ─────────────────────────────────

// The base moved tenant-alpha to staging (a platform admin, after the pod
// synced). A writer's body still says production — no change on the local
// tree, a change on the tree the PR would rewrite: refused there, nothing
// left behind.
func TestLabelGuardPutPRModeBaseDrift(t *testing.T) {
	staging := lgTenantYAML("      environment: staging\n      domain: finance\n", "80")
	t.Run("writer reverting the base's label", func(t *testing.T) {
		f := lgPRFixture(t, map[string]string{lgTenant + ".yaml": staging})
		before := gitRev(t, f.bare, "main")
		w := lgPut(t, f.deps, lgWriters, lgTenant, lgTenantYAML(lgProdFinance, "90"))
		lgAssertLabelDenied(t, w, lgTenant)
		if !strings.Contains(w.Body.String(), "latest base branch") {
			t.Errorf("the refusal does not say it was judged on the base: %s", w.Body.String())
		}
		if f.prOpened {
			t.Error("a PR was opened")
		}
		if b := f.tenantBranches(t); b != "" {
			t.Errorf("branch left behind: %s", b)
		}
		if got := gitRev(t, f.bare, "main"); got != before {
			t.Errorf("origin main moved: %s → %s", before, got)
		}
	})
	t.Run("writer keeping the base's label", func(t *testing.T) {
		// The body agrees with the base, so it is a change on the local tree
		// (production → staging): refused early, as the local check can only
		// refuse early.
		f := lgPRFixture(t, map[string]string{lgTenant + ".yaml": staging})
		w := lgPut(t, f.deps, lgWriters, lgTenant, lgTenantYAML("      environment: staging\n      domain: finance\n", "90"))
		lgAssertLabelDenied(t, w, lgTenant)
		if f.prOpened {
			t.Error("a PR was opened")
		}
	})
	t.Run("writer with no label change on either tree", func(t *testing.T) {
		// The base moved something else: the write goes through.
		f := lgPRFixture(t, map[string]string{lgProfiled + ".yaml": strings.Replace(lgBeta, "'80'", "'85'", 1)})
		w := lgPut(t, f.deps, lgWriters, lgTenant, lgTenantYAML(lgProdFinance, "90"))
		if w.Code != http.StatusOK || !f.prOpened {
			t.Fatalf("status %d, PR opened %v, body %s; want the PR opened", w.Code, f.prOpened, w.Body.String())
		}
	})
	t.Run("platform admin reverting the base's label", func(t *testing.T) {
		f := lgPRFixture(t, map[string]string{lgTenant + ".yaml": staging})
		w := lgPut(t, f.deps, lgPlatform, lgTenant, lgTenantYAML(lgProdFinance, "90"))
		if w.Code != http.StatusOK || !f.prOpened {
			t.Fatalf("status %d, PR opened %v, body %s; want the PR opened", w.Code, f.prOpened, w.Body.String())
		}
	})
}

// The batch twin: the base moved tenant-beta to devfin; an op setting it back
// to prodfin is a no-op on the local tree and a change on the base.
func TestLabelGuardBatchPRModeBaseDrift(t *testing.T) {
	devBeta := strings.Replace(lgBeta, "prodfin", "devfin", 1)
	ops := `[{"tenant_id":"` + lgProfiled + `","patch":{"_profile":"prodfin"}}]`
	t.Run("writer", func(t *testing.T) {
		f := lgPRFixture(t, map[string]string{lgProfiled + ".yaml": devBeta})
		before := gitRev(t, f.bare, "main")
		w := lgBatch(t, f.deps, lgWriters, ops)
		assertRejectedOp(t, w, 0, lgProfiled, string(errcode.Forbidden), lgDeniedFrag)
		if !strings.Contains(w.Body.String(), "latest base branch") {
			t.Errorf("the refusal does not say it was judged on the base: %s", w.Body.String())
		}
		if f.prOpened {
			t.Error("a PR was opened")
		}
		if b := f.batchBranches(t); b != "" {
			t.Errorf("branch left behind: %s", b)
		}
		if got := gitRev(t, f.bare, "main"); got != before {
			t.Errorf("origin main moved: %s → %s", before, got)
		}
	})
	t.Run("platform admin", func(t *testing.T) {
		f := lgPRFixture(t, map[string]string{lgProfiled + ".yaml": devBeta})
		w := lgBatch(t, f.deps, lgPlatform, ops)
		if w.Code != http.StatusOK || !f.prOpened {
			t.Fatalf("status %d, PR opened %v, body %s; want the PR opened", w.Code, f.prOpened, w.Body.String())
		}
	})
}
