package handler

// #1597 — end-to-end (real HTTP) harness for the WRITE-plane metadata scope
// axis. The axis already has in-process regression tests
// (rbac/metadata_write_scope_test.go, handler/metadata_write_proposed_test.go);
// what neither of them exercises is the WIRING: whether a real request, carried
// by the real middleware into the real handler, actually reaches those
// predicates with the tenant's real on-disk `_metadata` attached.
//
// That gap is not theoretical. The write plane's metadata gate lives ONLY in
// the handler: the route middleware resolves a PermWrite route through
// m.Allowed, which is metadata-blind by design (rbac.TestAllowedStaysBlindOnBothAxes,
// middleware.go's "WRITE routes stay org-blind here too; their gate lives
// handler-side in RequireOrgWrite"). So every part of this axis that a
// deployment actually depends on — the resolver being handed d.ConfigDir, the
// handler calling the gate before any commit, the post-state gate running on
// the body — is wiring the unit tests take as given.
//
// Fixture shape (mirrors org_write_enforce_test.go, the org axis's equivalent
// harness):
//   - ONE rule carrying `environments: [production]`, tenants ["*"], full
//     perms, loaded through the PRODUCTION path (rbac.NewManager over a real
//     _rbac.yaml). Deliberately NO org-scope and a nil TenantOrg, so a failure
//     here cannot be the org axis in disguise.
//   - Two seeded tenants: one labeled production (in the caller's scope), one
//     labeled dev (outside it).
//   - A git-backed writer with an onWrite spy: a denied request must not merely
//     answer 403 — the spy pins that NO commit happened.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/views"
)

const (
	metaEnfGroup      = "meta-enf-writers"
	metaEnfTenantIn   = "tenant-meta-in"  // _metadata.environment = production
	metaEnfTenantOut  = "tenant-meta-out" // _metadata.environment = dev
	metaEnfScopedEnv  = "production"
	metaEnfOutsideEnv = "dev"

	// Seeded on disk vs. sent in the body — see tenantYAMLWithEnv.
	metaEnfSeedMode = "critical"
	metaEnfPutMode  = "warning"
)

// The rule the whole file turns on: write is granted on every tenant, but only
// in `production`. Pre-#1597 the environments[] line bound the list plane only.
const metaEnfRBACYAML = `groups:
  - name: ` + metaEnfGroup + `
    tenants: ["*"]
    permissions: [read, write, admin]
    environments: ["` + metaEnfScopedEnv + `"]
`

// tenantYAMLWithEnv builds a whole tenant file — the shape PUT replaces, with
// `_metadata` inside it (which is why the post-state gate has to exist).
//
// `mode` exists so a PUT body can differ from what the fixture seeded on disk.
// It is not cosmetic: gitops.Writer commits nothing when the rendered file is
// byte-identical to the current one, so a body echoing the seed makes an
// ALLOWED write indistinguishable from a denied one at the onWrite spy — the
// allow-rows below would pass while proving nothing. Seed with seedMode, PUT
// with putMode.
func tenantYAMLWithEnv(tenantID, env, mode string) string {
	return "tenants:\n  " + tenantID + ":\n    _metadata:\n      environment: " + env +
		"\n    _silent_mode: \"" + mode + "\"\n"
}

type metaEnfFixture struct {
	configDir string
	writer    *gitops.Writer
	rbacMgr   *rbac.Manager
	writes    atomic.Int32
}

// newMetaEnfFixture builds the harness. enforce selects the mode under test:
// true = the axis is fail-closed, false = shadow (the default a deployment
// upgrades into).
func newMetaEnfFixture(t *testing.T, enforce bool) *metaEnfFixture {
	t.Helper()
	configDir := setupConfigDir(t, map[string]string{
		metaEnfTenantIn + ".yaml":  tenantYAMLWithEnv(metaEnfTenantIn, metaEnfScopedEnv, metaEnfSeedMode),
		metaEnfTenantOut + ".yaml": tenantYAMLWithEnv(metaEnfTenantOut, metaEnfOutsideEnv, metaEnfSeedMode),
	})
	initGitRepo(t, configDir)
	cmd := exec.Command("git", "-C", configDir, "branch", "-M", "main")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git branch -M main: %v\n%s", err, out)
	}

	f := &metaEnfFixture{configDir: configDir}
	f.writer = newTestWriter(configDir)
	f.writer.SetOnWrite(func(string) { f.writes.Add(1) })
	f.rbacMgr = newRBACManagerWithClaims(t, metaEnfRBACYAML, nil)
	if enforce {
		f.rbacMgr.EnableMetadataWriteScopeEnforce()
	}
	return f
}

// deps deliberately leaves TenantOrg nil — the metadata axis must stand on its
// own, and OrgAllowed is nil-receiver-safe (reports the tenant as unlabeled).
func (f *metaEnfFixture) deps() *Deps {
	return &Deps{
		ConfigDir: f.configDir,
		Writer:    f.writer,
		RBAC:      f.rbacMgr,
		Groups:    groups.NewManager(f.configDir),
		Views:     views.NewManager(f.configDir),
		WriteMode: WriteModeDirect,
	}
}

func metaEnfIdentity(req *http.Request) *http.Request {
	req.Header.Set("X-Forwarded-Email", "meta-caller@example.com")
	req.Header.Set("X-Forwarded-Groups", metaEnfGroup)
	return req
}

// putTenant drives one real request end-to-end: middleware → handler.
// bodyYAML is what the caller proposes to write.
func (f *metaEnfFixture) putTenant(t *testing.T, tenantID, bodyYAML string) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+tenantID, "id", tenantID,
		bytes.NewBufferString(bodyYAML))
	req = metaEnfIdentity(req)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(f.deps()), f.rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
	return w
}

// ── Pre-state gate: the tenant's metadata AS IT IS ON DISK ─────────────────

// The core end-to-end claim: a write to a tenant labeled outside the caller's
// environments[] is refused, and refused BEFORE anything is committed.
//
// ⚠️ The body deliberately proposes an IN-scope environment. An out-of-scope
// body would make this row pass for the wrong reason: the post-state gate would
// refuse it too, so deleting the pre-state gate entirely would leave the 403 (and
// the zero-commit) intact and this test green. Measured, not assumed — that is
// exactly what the M1 mutation did to the first version of this test. With an
// in-scope body only the ON-DISK label can deny, so this row now fails if and
// only if the pre-state gate is gone.
func TestMetadataWriteEnforce_EndToEnd_OutOfScopeTenantDenied(t *testing.T) {
	t.Parallel()
	f := newMetaEnfFixture(t, true)

	w := f.putTenant(t, metaEnfTenantOut, tenantYAMLWithEnv(metaEnfTenantOut, metaEnfScopedEnv, metaEnfPutMode))

	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
	if n := f.writes.Load(); n != 0 {
		t.Errorf("denied request committed %d time(s), want 0", n)
	}
}

// The other row of the same table: enforce must NARROW, not deny outright. A
// tenant inside the caller's environment stays writable over real HTTP.
func TestMetadataWriteEnforce_EndToEnd_InScopeTenantAllowed(t *testing.T) {
	t.Parallel()
	f := newMetaEnfFixture(t, true)

	w := f.putTenant(t, metaEnfTenantIn, tenantYAMLWithEnv(metaEnfTenantIn, metaEnfScopedEnv, metaEnfPutMode))

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if n := f.writes.Load(); n != 1 {
		t.Errorf("allowed request commits = %d, want 1", n)
	}
}

// Migration safety, measured on the wire rather than on a predicate: the exact
// request that enforce refuses must still succeed — and actually commit — in
// the shadow default a deployment upgrades into. A shadow regression here is
// the shape that silently tightens deployments that never opted in.
func TestMetadataWriteEnforce_EndToEnd_ShadowStillWritesOutOfScopeTenant(t *testing.T) {
	t.Parallel()
	f := newMetaEnfFixture(t, false)

	// Byte-identical request to the enforce row above, so the two differ in the
	// flag and nothing else.
	w := f.putTenant(t, metaEnfTenantOut, tenantYAMLWithEnv(metaEnfTenantOut, metaEnfScopedEnv, metaEnfPutMode))

	if w.Code != http.StatusOK {
		t.Fatalf("shadow: status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if n := f.writes.Load(); n != 1 {
		t.Errorf("shadow: commits = %d, want 1 — shadow must not merely avoid 403, it must still write", n)
	}
}

// ── Post-state gate: the metadata the BODY proposes ───────────────────────

// The relabel shape, end-to-end. The pre-state gate passes (the tenant is
// production today, the caller is production-scoped), so this 403 can only come
// from RequireOrgWriteProposed — which makes it the only assertion that proves
// the post-state gate is actually wired into the routed PUT rather than merely
// unit-tested.
func TestMetadataWriteEnforce_EndToEnd_RelabelOutOfScopeDenied(t *testing.T) {
	t.Parallel()
	f := newMetaEnfFixture(t, true)

	// Precondition, over the same wire: this tenant IS writable by this caller.
	if w := f.putTenant(t, metaEnfTenantIn, tenantYAMLWithEnv(metaEnfTenantIn, metaEnfScopedEnv, metaEnfPutMode)); w.Code != http.StatusOK {
		t.Fatalf("precondition: in-scope write status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	before := f.writes.Load()

	// Same tenant, same caller — a body that moves it to dev.
	w := f.putTenant(t, metaEnfTenantIn, tenantYAMLWithEnv(metaEnfTenantIn, metaEnfOutsideEnv, metaEnfPutMode))

	if w.Code != http.StatusForbidden {
		t.Fatalf("relabel: status = %d, want 403; body=%s", w.Code, w.Body.String())
	}
	if n := f.writes.Load(); n != before {
		t.Errorf("relabel: commits went %d → %d, want no additional commit", before, n)
	}
}

// The post-state gate inherits the axis's flag, so in shadow the same relabel
// goes through. Pinned end-to-end for the same reason as the pre-state shadow
// row: the migration promise is about real requests, not predicates.
func TestMetadataWriteEnforce_EndToEnd_ShadowAllowsRelabel(t *testing.T) {
	t.Parallel()
	f := newMetaEnfFixture(t, false)

	w := f.putTenant(t, metaEnfTenantIn, tenantYAMLWithEnv(metaEnfTenantIn, metaEnfOutsideEnv, metaEnfPutMode))

	if w.Code != http.StatusOK {
		t.Fatalf("shadow relabel: status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if n := f.writes.Load(); n != 1 {
		t.Errorf("shadow relabel: commits = %d, want 1", n)
	}
}

// ── The 403 an operator actually receives ─────────────────────────────────

// What the denied caller is told. Pinned as a characterization test: the
// envelope shape and code are the contract, and the message is recorded here so
// that a future edit to either denial string is a visible diff rather than a
// silent change to what operators are instructed to do.
func TestMetadataWriteEnforce_EndToEnd_DenialEnvelope(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name     string
		tenant   string
		body     string
		wantFrag string
	}{
		{
			// In-scope body, out-of-scope tenant on disk — only the pre-state
			// gate can produce this denial (see OutOfScopeTenantDenied).
			name:     "pre_state_denial",
			tenant:   metaEnfTenantOut,
			body:     tenantYAMLWithEnv(metaEnfTenantOut, metaEnfScopedEnv, metaEnfPutMode),
			wantFrag: "insufficient permissions for tenant " + metaEnfTenantOut,
		},
		{
			name:     "post_state_denial",
			tenant:   metaEnfTenantIn,
			body:     tenantYAMLWithEnv(metaEnfTenantIn, metaEnfOutsideEnv, metaEnfPutMode),
			wantFrag: "the environment/domain in the body places tenant " + metaEnfTenantIn + " outside your scope",
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			f := newMetaEnfFixture(t, true)
			w := f.putTenant(t, tc.tenant, tc.body)

			if w.Code != http.StatusForbidden {
				t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
			}
			var env struct {
				Code  string `json:"code"`
				Error string `json:"error"`
			}
			if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
				t.Fatalf("denial body is not the JSON error envelope: %v; raw=%s", err, w.Body.String())
			}
			if env.Code != string(CodeForbidden) {
				t.Errorf("code = %q, want %q", env.Code, CodeForbidden)
			}
			if !strings.Contains(env.Error, tc.wantFrag) {
				t.Errorf("error = %q, want it to contain %q", env.Error, tc.wantFrag)
			}
		})
	}
}
