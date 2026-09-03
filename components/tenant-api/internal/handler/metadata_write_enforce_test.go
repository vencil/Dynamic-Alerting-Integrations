package handler

// #1597 — end-to-end (real HTTP) harness for the WRITE-plane metadata scope
// axis. The axis already has in-process regression tests
// (rbac/metadata_write_scope_test.go, handler/metadata_write_proposed_test.go);
// what neither of them exercises is the WIRING: whether a real request, carried
// by the real middleware into the real handler, actually reaches those
// predicates with the tenant's real on-disk `_metadata` attached.
//
// That gap is not theoretical. The write plane's metadata gate lives ONLY in
// the handler: a PermWrite route's middleware authorizes through m.Allowed,
// which stays metadata-blind even in enforce mode — pinned by
// rbac.TestAllowedStaysBlindOnBothAxes, and consistent with middleware.go's
// note that write routes keep their org gate handler-side in RequireOrgWrite.
// So every part of this axis a deployment actually depends on — the resolver
// being handed d.ConfigDir, the handler calling the gate before any commit, the
// post-state gate running on the body — is wiring the unit tests take as given.
//
// Fixture shape (mirrors org_write_enforce_test.go, the org axis's equivalent
// harness):
//   - ONE rule carrying BOTH `environments: [production]` and
//     `domains: [finance]`, tenants ["*"], full perms, loaded through the
//     PRODUCTION path (rbac.NewManager over a real _rbac.yaml). Deliberately no
//     org-scope and a nil TenantOrg, so a failure here cannot be the org axis
//     in disguise.
//   - THREE seeded tenants isolating each half of the axis: one in scope on
//     both, one out on environment only, one out on domain only. Both halves
//     are covered on purpose — dropping `domain` from the two resolvers used to
//     leave this file (and the whole module) green, while `scopeFieldModes`
//     treats an unresolved domain as UNLABELED, which in enforce mode denies
//     every write a `domains:` rule covers. That is a hard false-403, so the
//     half that would cause it gets its own rows.
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
	metaEnfGroup = "meta-enf-writers"

	metaEnfTenantIn        = "tenant-meta-in"     // production / finance — in scope on both halves
	metaEnfTenantEnvOut    = "tenant-meta-envout" // dev / finance        — out on environment only
	metaEnfTenantDomainOut = "tenant-meta-domout" // production / retail  — out on domain only

	metaEnfScopedEnv     = "production"
	metaEnfOutsideEnv    = "dev"
	metaEnfScopedDomain  = "finance"
	metaEnfOutsideDomain = "retail"

	// Seeded on disk vs. sent in the body — see tenantYAMLWithMeta.
	metaEnfSeedMode = "critical"
	metaEnfPutMode  = "warning"

	// The denial string RequireOrgWrite appends and nothing else does. The
	// bare "insufficient permissions for tenant <id>" prefix is NOT usable as
	// an assertion: rbac.writeForbidden — the MIDDLEWARE's denial, a different
	// path that never reaches the handler — emits that exact prefix with the
	// same FORBIDDEN code, so a Contains() on it passes for either path and
	// characterizes neither.
	metaEnfHandlerPreStateFrag = "(permission and organization-scope checks, ADR-027)"
)

// The rule the whole file turns on: write is granted on every tenant, but only
// in `production` AND only in `finance`. Pre-#1597 both lines bound the list
// plane only.
const metaEnfRBACYAML = `groups:
  - name: ` + metaEnfGroup + `
    tenants: ["*"]
    permissions: [read, write, admin]
    environments: ["` + metaEnfScopedEnv + `"]
    domains: ["` + metaEnfScopedDomain + `"]
`

// tenantYAMLWithMeta builds a whole tenant file — the shape PUT replaces, with
// `_metadata` inside it (which is why the post-state gate has to exist).
//
// `mode` exists so a PUT body can differ from what the fixture seeded on disk.
// It is not cosmetic: gitops.Writer commits nothing when the rendered file is
// byte-identical to the current one, so a body echoing the seed makes an
// ALLOWED write indistinguishable from a denied one at the onWrite spy — the
// allow-rows below would pass while proving nothing. Seed with seedMode, PUT
// with putMode.
func tenantYAMLWithMeta(tenantID, env, domain, mode string) string {
	return "tenants:\n  " + tenantID + ":\n    _metadata:\n      environment: " + env +
		"\n      domain: " + domain +
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
		metaEnfTenantIn + ".yaml":        tenantYAMLWithMeta(metaEnfTenantIn, metaEnfScopedEnv, metaEnfScopedDomain, metaEnfSeedMode),
		metaEnfTenantEnvOut + ".yaml":    tenantYAMLWithMeta(metaEnfTenantEnvOut, metaEnfOutsideEnv, metaEnfScopedDomain, metaEnfSeedMode),
		metaEnfTenantDomainOut + ".yaml": tenantYAMLWithMeta(metaEnfTenantDomainOut, metaEnfScopedEnv, metaEnfOutsideDomain, metaEnfSeedMode),
	})
	initGitRepo(t, configDir)
	// initGitRepo leaves the repo on the init default branch; gitops.Writer's
	// base branch is "main" — normalize (same as the org-axis harness).
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
// headers may add request headers (used by the ordering row); may be nil.
func (f *metaEnfFixture) putTenant(t *testing.T, tenantID, bodyYAML string, headers map[string]string) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+tenantID, "id", tenantID,
		bytes.NewBufferString(bodyYAML))
	req = metaEnfIdentity(req)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(f.deps()), f.rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
	return w
}

// denialAction returns the JSON error envelope's code + error text.
func decodeEnvelope(t *testing.T, w *httptest.ResponseRecorder) (code, errText string) {
	t.Helper()
	var env struct {
		Code  string `json:"code"`
		Error string `json:"error"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("denial body is not the JSON error envelope: %v; raw=%s", err, w.Body.String())
	}
	return env.Code, env.Error
}

// ── Pre-state gate: the tenant's metadata AS IT IS ON DISK ─────────────────

// The core end-to-end claim, asserted once per half of the axis: a write to a
// tenant labeled outside the caller's environments[]/domains[] is refused, and
// refused BEFORE anything is committed.
//
// ⚠️ Each row's body deliberately proposes IN-scope metadata. An out-of-scope
// body would make the row pass for the wrong reason: the post-state gate would
// refuse it too, so deleting the pre-state gate entirely would leave the 403
// (and the zero-commit) intact and the test green. Measured, not assumed — that
// is exactly what happened to the first version of this test. With an in-scope
// body only the ON-DISK label can deny.
//
// The message assertion is what separates this from a middleware denial: see
// metaEnfHandlerPreStateFrag.
func TestMetadataWriteEnforce_EndToEnd_PreStateDenied(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name   string
		tenant string
	}{
		{"environment_half", metaEnfTenantEnvOut},
		{"domain_half", metaEnfTenantDomainOut},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			f := newMetaEnfFixture(t, true)

			w := f.putTenant(t, tc.tenant,
				tenantYAMLWithMeta(tc.tenant, metaEnfScopedEnv, metaEnfScopedDomain, metaEnfPutMode), nil)

			if w.Code != http.StatusForbidden {
				t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
			}
			code, errText := decodeEnvelope(t, w)
			if code != string(CodeForbidden) {
				t.Errorf("code = %q, want %q", code, CodeForbidden)
			}
			if !strings.Contains(errText, metaEnfHandlerPreStateFrag) {
				t.Errorf("error = %q, want the handler's own denial (containing %q) — a bare "+
					"\"insufficient permissions\" prefix is also what the middleware emits",
					errText, metaEnfHandlerPreStateFrag)
			}
			if n := f.writes.Load(); n != 0 {
				t.Errorf("denied request committed %d time(s), want 0", n)
			}
		})
	}
}

// Enforce must NARROW, not deny outright. A tenant inside the caller's scope on
// both halves stays writable over real HTTP.
func TestMetadataWriteEnforce_EndToEnd_InScopeTenantAllowed(t *testing.T) {
	t.Parallel()
	f := newMetaEnfFixture(t, true)

	w := f.putTenant(t, metaEnfTenantIn,
		tenantYAMLWithMeta(metaEnfTenantIn, metaEnfScopedEnv, metaEnfScopedDomain, metaEnfPutMode), nil)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if n := f.writes.Load(); n != 1 {
		t.Errorf("allowed request commits = %d, want 1", n)
	}
}

// The gate must run BEFORE the request body and its headers are parsed, which
// tenant_put.go states as an ordering property: a denied caller triggers no
// side effect and learns nothing from a body it was never allowed to submit.
// Pinned by pairing an out-of-scope tenant with a malformed X-DA-Base-Hash — if
// the gate ever moves below readBaseHashHeader, this answers 400 instead.
func TestMetadataWriteEnforce_EndToEnd_GateRunsBeforeHeaderParsing(t *testing.T) {
	t.Parallel()
	f := newMetaEnfFixture(t, true)

	w := f.putTenant(t, metaEnfTenantEnvOut,
		tenantYAMLWithMeta(metaEnfTenantEnvOut, metaEnfScopedEnv, metaEnfScopedDomain, metaEnfPutMode),
		map[string]string{BaseHashHeader: "not-a-source-hash"})

	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403 — authorization must precede %s parsing; body=%s",
			w.Code, BaseHashHeader, w.Body.String())
	}
	if n := f.writes.Load(); n != 0 {
		t.Errorf("denied request committed %d time(s), want 0", n)
	}
}

// Migration safety, measured on the wire rather than on a predicate: the exact
// request that enforce refuses must still succeed — and actually commit — in
// the shadow default a deployment upgrades into. A shadow regression here is
// the shape that silently tightens deployments that never opted in.
func TestMetadataWriteEnforce_EndToEnd_ShadowStillWritesOutOfScopeTenant(t *testing.T) {
	t.Parallel()
	f := newMetaEnfFixture(t, false)

	// Byte-identical request to the environment_half row above, so the two
	// differ in the flag and nothing else.
	w := f.putTenant(t, metaEnfTenantEnvOut,
		tenantYAMLWithMeta(metaEnfTenantEnvOut, metaEnfScopedEnv, metaEnfScopedDomain, metaEnfPutMode), nil)

	if w.Code != http.StatusOK {
		t.Fatalf("shadow: status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if n := f.writes.Load(); n != 1 {
		t.Errorf("shadow: commits = %d, want 1 — shadow must not merely avoid 403, it must still write", n)
	}
}

// ── Post-state gate: the metadata the BODY proposes ───────────────────────

// The relabel shape, end-to-end, once per half of the axis. The pre-state gate
// passes (the tenant is in scope today, on both halves), so this 403 can only
// come from RequireOrgWriteProposed — which makes these the only assertions
// that prove the post-state gate is wired into the routed PUT rather than
// merely unit-tested.
func TestMetadataWriteEnforce_EndToEnd_RelabelOutOfScopeDenied(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name        string
		env, domain string
	}{
		{"environment_half", metaEnfOutsideEnv, metaEnfScopedDomain},
		{"domain_half", metaEnfScopedEnv, metaEnfOutsideDomain},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			f := newMetaEnfFixture(t, true)

			// Precondition, over the same wire: this tenant IS writable.
			if w := f.putTenant(t, metaEnfTenantIn,
				tenantYAMLWithMeta(metaEnfTenantIn, metaEnfScopedEnv, metaEnfScopedDomain, metaEnfPutMode), nil); w.Code != http.StatusOK {
				t.Fatalf("precondition: in-scope write status = %d, want 200; body=%s", w.Code, w.Body.String())
			}
			before := f.writes.Load()

			// Same tenant, same caller — a body that moves it out of scope.
			w := f.putTenant(t, metaEnfTenantIn,
				tenantYAMLWithMeta(metaEnfTenantIn, tc.env, tc.domain, metaEnfPutMode), nil)

			if w.Code != http.StatusForbidden {
				t.Fatalf("relabel: status = %d, want 403; body=%s", w.Code, w.Body.String())
			}
			code, errText := decodeEnvelope(t, w)
			if code != string(CodeForbidden) {
				t.Errorf("code = %q, want %q", code, CodeForbidden)
			}
			if want := "the environment/domain in the body places tenant " + metaEnfTenantIn +
				" outside your scope"; !strings.Contains(errText, want) {
				t.Errorf("error = %q, want it to contain %q", errText, want)
			}
			if n := f.writes.Load(); n != before {
				t.Errorf("relabel: commits went %d → %d, want no additional commit", before, n)
			}
		})
	}
}

// The post-state gate inherits the axis's flag, so in shadow the same relabel
// goes through. Pinned end-to-end for the same reason as the pre-state shadow
// row: the migration promise is about real requests, not predicates.
func TestMetadataWriteEnforce_EndToEnd_ShadowAllowsRelabel(t *testing.T) {
	t.Parallel()
	f := newMetaEnfFixture(t, false)

	w := f.putTenant(t, metaEnfTenantIn,
		tenantYAMLWithMeta(metaEnfTenantIn, metaEnfOutsideEnv, metaEnfOutsideDomain, metaEnfPutMode), nil)

	if w.Code != http.StatusOK {
		t.Fatalf("shadow relabel: status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if n := f.writes.Load(); n != 1 {
		t.Errorf("shadow relabel: commits = %d, want 1", n)
	}
}
