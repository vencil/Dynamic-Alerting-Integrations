package handler

// #2857: in PR mode the metadata write scope is judged once, authoritatively,
// on the fresh base the branch is cut from. The check on the pod's local tree
// (synced at pod start) can only refuse early, never let a write through that
// the base would refuse. Each case below has a local tree the caller's scope
// allows and a base it does not: the write is refused (403, no PR, no
// branch, origin unmoved) and exactly one scope_denied{axis="metadata_write"}
// is counted — by the re-judgement on the base, the local check having
// allowed.

import (
	"bytes"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

const (
	freshScopeDefaults = "defaults:\n  mysql_connections: 80\n"
	// stayprod stays production on every base; prodprof is the profile the
	// base moves to dev in the post-state case.
	freshScopeProfiles = "profiles:\n" +
		"  prodprof:\n    _metadata:\n      environment: production\n" +
		"  stayprod:\n    _metadata:\n      environment: production\n" +
		"  devprof:\n    _metadata:\n      environment: dev\n"
	freshScopeProduction = "tenants:\n  db-a:\n    _profile: prodprof\n    mysql_connections: '80'\n"
	freshScopeDev        = "tenants:\n  db-a:\n    _profile: devprof\n    mysql_connections: '80'\n"
)

type freshScopeCase struct {
	name          string
	local, origin map[string]string
	put           string // PUT body; empty → batch ops
	ops           string
	preState      bool
}

func freshScopeCases() []freshScopeCase {
	return []freshScopeCase{
		{
			// The tenant itself moved to dev on the base. The write leaves
			// its metadata alone, so only the pre-state can catch it.
			name:     "batch pre-state",
			local:    map[string]string{"_defaults.yaml": freshScopeDefaults, "_profiles.yaml": freshScopeProfiles, "db-a.yaml": freshScopeProduction},
			origin:   map[string]string{"db-a.yaml": freshScopeDev},
			ops:      `[{"tenant_id":"db-a","patch":{"mysql_connections":"90"}}]`,
			preState: true,
		},
		{
			name:     "PUT pre-state",
			local:    map[string]string{"_defaults.yaml": freshScopeDefaults, "_profiles.yaml": freshScopeProfiles, "db-a.yaml": freshScopeProduction},
			origin:   map[string]string{"db-a.yaml": freshScopeDev},
			put:      "tenants:\n  db-a:\n    _metadata:\n      environment: production\n    mysql_connections: '90'\n",
			preState: true,
		},
		{
			// db-a stays production on the base (stayprod), but the body
			// names prodprof, which is production locally and dev on the
			// base: only the post-state on the base is out of scope.
			name: "PUT post-state",
			local: map[string]string{"_defaults.yaml": freshScopeDefaults, "_profiles.yaml": freshScopeProfiles,
				"db-a.yaml": strings.Replace(freshScopeProduction, "prodprof", "stayprod", 1)},
			origin: map[string]string{"_profiles.yaml": strings.Replace(freshScopeProfiles, "environment: production", "environment: dev", 1)},
			put:    "tenants:\n  db-a:\n    _profile: prodprof\n    mysql_connections: '90'\n",
		},
	}
}

func (c freshScopeCase) run(t *testing.T, f *staleFixture) *httptest.ResponseRecorder {
	t.Helper()
	if c.put != "" {
		return putStale(t, f, "db-a", c.put)
	}
	return postTenantBatch(t, f.deps, c.ops)
}

// adr-check: ADR-027/9.6f
func TestPRModeScopeAuthoritativeOnFreshBase(t *testing.T) {
	for _, c := range freshScopeCases() {
		t.Run(c.name, func(t *testing.T) {
			f := newStaleFixture(t, c.local, c.origin)
			mgr := newRBACManager(t, postStateRBAC)
			audit := &countingScopeAuditor{n: map[string]int{}}
			mgr.SetScopeAuditor(audit)
			f.deps.RBAC = mgr
			if env, _ := WriteScopeMeta(f.dir)("db-a"); env != "production" {
				t.Fatalf("precondition: db-a on the local tree = %q, want production", env)
			}
			before := gitRev(t, f.bare, "main")
			w := c.run(t, f)
			tenant, code, msg := freshRefusal(t, w)
			if w.Code != freshRefusalStatus(c.put == "") || code != string(errcode.Forbidden) || tenant != "db-a" ||
				!strings.Contains(msg, "latest base branch") {
				t.Fatalf("status %d, body %s; want %d %s for db-a on the latest base", w.Code, w.Body.String(), freshRefusalStatus(c.put == ""), errcode.Forbidden)
			}
			if pre := strings.Contains(msg, "permission and organization-scope checks"); pre != c.preState {
				t.Errorf("pre-state message = %v, want %v: %s", pre, c.preState, msg)
			}
			if strings.Contains(msg, "operations[") {
				t.Errorf("the refusal carries an op index: %s", w.Body.String())
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
			if got := audit.n["metadata_write"]; got != 1 {
				t.Errorf("scope_denied{axis=metadata_write} = %d, want 1: the write leaves scope only on the latest base", got)
			}
		})
	}
}

// When the base agrees with the local tree, a write the local check refused
// is counted once: the refusal comes before the in-lock re-judgement, which
// therefore never asks again.
func TestPRModeScopeCountsOnceWhenBaseAgrees(t *testing.T) {
	local := map[string]string{"_defaults.yaml": freshScopeDefaults, "_profiles.yaml": freshScopeProfiles, "db-a.yaml": freshScopeDev}
	for name, run := range map[string]func(*staleFixture) *httptest.ResponseRecorder{
		"PUT": func(f *staleFixture) *httptest.ResponseRecorder {
			return putStale(t, f, "db-a", freshScopeDev)
		},
		"batch": func(f *staleFixture) *httptest.ResponseRecorder {
			return postTenantBatch(t, f.deps, `[{"tenant_id":"db-a","patch":{"mysql_connections":"90"}}]`)
		},
	} {
		t.Run(name, func(t *testing.T) {
			f := newStaleFixture(t, local, nil)
			mgr := newRBACManager(t, postStateRBAC)
			audit := &countingScopeAuditor{n: map[string]int{}}
			mgr.SetScopeAuditor(audit)
			f.deps.RBAC = mgr
			if w := run(f); w.Code == http.StatusOK || f.prOpened {
				t.Fatalf("status %d, PR opened %v; want refused: db-a is dev on the local tree: %s", w.Code, f.prOpened, w.Body.String())
			}
			// The pre-state check on the local tree refuses; nothing after
			// it — the post-state check, the in-lock re-judgement — runs.
			if got := audit.n["metadata_write"]; got != 1 {
				t.Errorf("scope_denied{axis=metadata_write} = %d, want 1 (the local pre-state check only)", got)
			}
		})
	}
}

// sideEffectBody runs fn on the request body's first Read.
type sideEffectBody struct {
	r    io.Reader
	fn   func()
	done bool
}

func (b *sideEffectBody) Read(p []byte) (int, error) {
	if !b.done {
		b.done = true
		b.fn()
	}
	return b.r.Read(p)
}

// The in-lock verdict is always taken. While this PUT's body is being read,
// another PR write has the shared working tree on its branch, cut from the
// latest base; anything that read "the local tree" again at that point would
// read the base, see nothing to judge, and let the write through.
// adr-check: ADR-027/9.6f
func TestPRModeScopeJudgedEvenWhenTheTreeMovedMidRequest(t *testing.T) {
	local := map[string]string{"_defaults.yaml": freshScopeDefaults, "_profiles.yaml": freshScopeProfiles, "db-a.yaml": freshScopeProduction}
	f := newStaleFixture(t, local, map[string]string{"db-a.yaml": freshScopeDev})
	f.deps.RBAC = newRBACManager(t, postStateRBAC)
	body := &sideEffectBody{
		r: bytes.NewBufferString("tenants:\n  db-a:\n    _metadata:\n      environment: production\n    mysql_connections: '90'\n"),
		fn: func() {
			runGit(t, f.dir, "fetch", "-q", "origin", "main")
			runGit(t, f.dir, "checkout", "-q", "-b", "tenant-api/other/1", "origin/main")
		}}
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", nil)
	req.Body = io.NopCloser(body)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(f.deps), f.deps.RBAC, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
	if w.Code != http.StatusForbidden || f.prOpened {
		t.Errorf("status %d, PR opened %v; want 403: db-a is dev on the base: %s", w.Code, f.prOpened, w.Body.String())
	}
}

// A tenant's later op in the same batch is compared with the local state
// after its earlier ops, so a scope denial is not counted twice: op0's local
// post-state is refused and counted, op1 (thresholds only) adds nothing —
// the batch is rejected whole (#2900), nothing written.
func TestPRModeScopeSameTenantOpsCountOnce(t *testing.T) {
	local := map[string]string{"_defaults.yaml": freshScopeDefaults, "_profiles.yaml": freshScopeProfiles, "db-a.yaml": freshScopeProduction}
	f := newStaleFixture(t, local, nil)
	mgr := newRBACManager(t, postStateRBAC)
	audit := &countingScopeAuditor{n: map[string]int{}}
	mgr.SetScopeAuditor(audit)
	f.deps.RBAC = mgr
	w := postTenantBatch(t, f.deps, `[{"tenant_id":"db-a","patch":{"_profile":"devprof"}},{"tenant_id":"db-a","patch":{"mysql_connections":"90"}}]`)
	assertRejectedOp(t, w, 0, "db-a", string(errcode.Forbidden), "this op proposes")
	if f.prOpened {
		t.Error("a PR was opened")
	}
	if got := audit.n["metadata_write"]; got != 1 {
		t.Errorf("scope_denied{axis=metadata_write} = %d, want 1 (op0's post-state)", got)
	}
}

// The tenant file is unchanged; the root platform layer moves db-a to dev
// on the base. Both paths read the base's root layer, not a cached one.
func TestPRModeScopeReadsTheBaseRootPlatformLayer(t *testing.T) {
	for _, kind := range []string{"batch", "PUT"} {
		t.Run(kind, func(t *testing.T) {
			local := map[string]string{"_defaults.yaml": freshScopeDefaults,
				"_platform.yaml": "tenants:\n  db-a:\n    _metadata:\n      environment: production\n",
				"db-a.yaml":      "tenants:\n  db-a:\n    mysql_connections: '80'\n"}
			origin := map[string]string{"_platform.yaml": "tenants:\n  db-a:\n    _metadata:\n      environment: dev\n"}
			f := newStaleFixture(t, local, origin)
			f.deps.RBAC = newRBACManager(t, postStateRBAC)
			var w *httptest.ResponseRecorder
			if kind == "batch" {
				w = postTenantBatch(t, f.deps, `[{"tenant_id":"db-a","patch":{"mysql_connections":"90"}}]`)
			} else {
				w = putStale(t, f, "db-a", "tenants:\n  db-a:\n    mysql_connections: '90'\n")
			}
			if w.Code != freshRefusalStatus(kind == "batch") || f.prOpened {
				t.Errorf("status %d, PR opened %v; want %d: %s", w.Code, f.prOpened, freshRefusalStatus(kind == "batch"), w.Body.String())
			}
		})
	}
}

// A tenant file that cannot be read on the base is refused for what it is,
// not as a scope violation, and counts no scope denial of its own.
func TestPRModeScopeBrokenBaseFileIsNotAScopeVerdict(t *testing.T) {
	broken := "tenants:\n  db-a:\n    _profile: prodprof\n    mysql_connections: [unclosed\n"
	for _, kind := range []string{"batch", "PUT"} {
		t.Run(kind, func(t *testing.T) {
			local := map[string]string{"_defaults.yaml": freshScopeDefaults, "_profiles.yaml": freshScopeProfiles, "db-a.yaml": freshScopeProduction}
			f := newStaleFixture(t, local, map[string]string{"db-a.yaml": broken})
			mgr := newRBACManager(t, postStateRBAC)
			audit := &countingScopeAuditor{n: map[string]int{}}
			mgr.SetScopeAuditor(audit)
			f.deps.RBAC = mgr
			var w *httptest.ResponseRecorder
			if kind == "batch" {
				w = postTenantBatch(t, f.deps, `[{"tenant_id":"db-a","patch":{"mysql_connections":"90"}}]`)
			} else {
				w = putStale(t, f, "db-a", "tenants:\n  db-a:\n    _metadata:\n      environment: production\n    mysql_connections: '90'\n")
			}
			if w.Code == http.StatusForbidden || w.Code == http.StatusOK {
				t.Errorf("status %d; want the broken base file's own error: %s", w.Code, w.Body.String())
			}
			if got := audit.n["metadata_write"]; got != 0 {
				t.Errorf("scope_denied{axis=metadata_write} = %d, want 0", got)
			}
		})
	}
}

const freshScopeOrgRBAC = `groups:
  - name: admins
    tenants: ["*"]
    environments: ["production"]
    permissions: [read, write]
    org-scope: org
`

// The in-lock re-judgement records nothing on the org axis when the base's
// organizations agree with the local gate's: a stale base adds no org_write
// denial to what the local gates already counted.
//
// The base moves db-a to dev by moving its profile (prodprof), and the body
// keeps that profile: the write changes db-a's labels on neither tree, which
// an org-scoped caller may not do (ADR-027 §7, label_guard.go). On the stale
// base the pre-state already differs from what the local gate judged and is
// refused there, so the write is counted once; the post-state is never judged.
func TestPRModeScopeRejudgeAddsNoOrgCount(t *testing.T) {
	counts := map[bool]map[string]int{}
	for _, stale := range []bool{false, true} {
		local := map[string]string{"_defaults.yaml": freshScopeDefaults, "_profiles.yaml": freshScopeProfiles, "db-a.yaml": freshScopeProduction,
			"_tenant_orgs.yaml": "tenant_orgs:\n  db-a: [acme]\n"}
		var origin map[string]string
		if stale {
			origin = map[string]string{"_profiles.yaml": strings.Replace(freshScopeProfiles, "environment: production", "environment: dev", 1)}
		}
		f := newStaleFixture(t, local, origin)
		claimHeaders := map[string]string{"org": "X-Org"}
		mgr := newRBACManagerWithClaims(t, freshScopeOrgRBAC, claimHeaders)
		audit := &countingScopeAuditor{n: map[string]int{}}
		mgr.SetScopeAuditor(audit)
		f.deps.RBAC = mgr
		f.deps.ClaimHeaders = claimHeaders
		f.deps.TenantOrg = tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{"db-a": {"acme"}}})
		req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a",
			bytes.NewBufferString("tenants:\n  db-a:\n    _profile: prodprof\n    mysql_connections: '90'\n"))
		req.Header.Set("X-Forwarded-Email", "alice@example.com")
		req.Header.Set("X-Forwarded-Groups", "admins")
		req.Header.Set("X-Org", "acme")
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(PutTenant(f.deps), f.deps.RBAC, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
		if want := map[bool]int{false: http.StatusOK, true: http.StatusForbidden}[stale]; w.Code != want {
			t.Fatalf("stale=%v: status %d, want %d: %s", stale, w.Code, want, w.Body.String())
		}
		counts[stale] = audit.n
	}
	if counts[true]["org_write"] != counts[false]["org_write"] {
		t.Errorf("org_write scope denials: %d with a stale base, %d without; want equal", counts[true]["org_write"], counts[false]["org_write"])
	}
	if counts[true]["metadata_write"] != counts[false]["metadata_write"]+1 {
		t.Errorf("metadata_write scope denials: %d stale vs %d; want exactly one more for the stale base (the pre-state refusal)", counts[true]["metadata_write"], counts[false]["metadata_write"])
	}
}

// The organization axis is judged on the base too: the base's
// `_tenant_orgs.yaml` moved db-a out of the caller's organization, or left it
// unlabeled, while the manager's copy (the local tree) still has it in: both
// are refused, and the refusal is counted once on org_write.
// adr-check: ADR-027/9.6f
func TestPRModeOrgScopeJudgedOnFreshBase(t *testing.T) {
	orgsFile := func(org string) string { return "tenant_orgs:\n  db-a: [" + org + "]\n" }
	run := func(t *testing.T, kind string, originOrgs string) (*staleFixture, *httptest.ResponseRecorder, map[string]int) {
		local := map[string]string{"_defaults.yaml": freshScopeDefaults, "_profiles.yaml": freshScopeProfiles,
			"db-a.yaml": freshScopeProduction, "_tenant_orgs.yaml": orgsFile("acme")}
		var origin map[string]string
		if originOrgs != "" {
			origin = map[string]string{"_tenant_orgs.yaml": originOrgs}
		}
		f := newStaleFixture(t, local, origin)
		claimHeaders := map[string]string{"org": "X-Org"}
		mgr := newRBACManagerWithClaims(t, freshScopeOrgRBAC, claimHeaders)
		audit := &countingScopeAuditor{n: map[string]int{}}
		mgr.SetScopeAuditor(audit)
		f.deps.RBAC = mgr
		f.deps.ClaimHeaders = claimHeaders
		f.deps.TenantOrg = tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{"db-a": {"acme"}}})
		var req *http.Request
		if kind == "PUT" {
			req = newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a",
				bytes.NewBufferString(strings.Replace(freshScopeProduction, "'80'", "'90'", 1)))
		} else {
			req = httptest.NewRequest("POST", "/api/v1/tenants/batch",
				bytes.NewBufferString(`{"operations":[{"tenant_id":"db-a","patch":{"mysql_connections":"90"}}]}`))
			req.Header.Set("Content-Type", "application/json")
		}
		req.Header.Set("X-Forwarded-Email", "alice@example.com")
		req.Header.Set("X-Forwarded-Groups", "admins")
		req.Header.Set("X-Org", "acme")
		w := httptest.NewRecorder()
		if kind == "PUT" {
			wrapWithRBACMiddleware(PutTenant(f.deps), mgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)
		} else {
			rbac.ResolveIdentity(mgr.HeaderResolver())(mgr.Middleware(rbac.PermRead, nil)(BatchTenants(f.deps))).ServeHTTP(w, req)
		}
		return f, w, audit.n
	}
	for _, kind := range []string{"PUT", "batch"} {
		_, w0, base := run(t, kind, "")
		if w0.Code != http.StatusOK {
			t.Fatalf("%s with the base agreeing: status %d, want 200: %s", kind, w0.Code, w0.Body.String())
		}
		for name, origin := range map[string]string{"other org": orgsFile("other"), "unlabeled": "tenant_orgs: {}\n"} {
			t.Run(kind+"/"+name, func(t *testing.T) {
				f, w, stale := run(t, kind, origin)
				if w.Code != freshRefusalStatus(kind == "batch") || f.prOpened {
					t.Errorf("status %d, PR opened %v; want %d: %s", w.Code, f.prOpened, freshRefusalStatus(kind == "batch"), w.Body.String())
				}
				if stale["org_write"] != base["org_write"]+1 {
					t.Errorf("org_write scope denials: %d with the stale base, %d without; want exactly one more", stale["org_write"], base["org_write"])
				}
			})
		}
	}
	t.Run("broken org file on the base", func(t *testing.T) {
		local := map[string]string{"_defaults.yaml": freshScopeDefaults, "_profiles.yaml": freshScopeProfiles,
			"db-a.yaml": freshScopeProduction, "_tenant_orgs.yaml": orgsFile("acme")}
		f := newStaleFixture(t, local, map[string]string{"_tenant_orgs.yaml": "tenant_orgs: [unclosed\n"})
		w := putStale(t, f, "db-a", freshScopeProduction)
		if w.Code != http.StatusInternalServerError || !strings.Contains(w.Body.String(), string(errcode.ConfigDecode)) || f.prOpened {
			t.Errorf("status %d, PR opened %v; want 500 %s: %s", w.Code, f.prOpened, errcode.ConfigDecode, w.Body.String())
		}
	})
	// A batch answers the same refusal per op: 422, the op's code
	// CONFIG_DECODE_ERROR, no PR/MR.
	t.Run("broken org file on the base, batch", func(t *testing.T) {
		f, w, _ := run(t, "batch", "tenant_orgs: [unclosed\n")
		if w.Code != http.StatusUnprocessableEntity || f.prOpened {
			t.Fatalf("status %d, PR opened %v; want 422: %s", w.Code, f.prOpened, w.Body.String())
		}
		if tenant, code, _ := freshRefusal(t, w); tenant != "db-a" || code != string(errcode.ConfigDecode) {
			t.Errorf("refused op = (%q, %q), want (db-a, %s): %s", tenant, code, errcode.ConfigDecode, w.Body.String())
		}
	})
}

// A group batch refused for one member (#2900: per op, 422) names that
// member only when the caller may read it, and never its index among the
// stored members: the per-op message carries no index, and the results the
// caller sees are filtered by read permission.
func TestGroupMemberRefusalHidesUnreadableMembers(t *testing.T) {
	f := newOrgEnfFixture(t, nil)
	d := f.deps()
	var results []BatchResult
	for _, member := range []string{orgEnfTenantIn, orgEnfTenantOut} {
		for _, err := range []error{
			&freshBaseScopeError{TenantID: member, Op: 13},
			&freshBaseScopeError{TenantID: member, Op: 13, PreState: true},
		} {
			r := freshBaseOpResult(member, err, false)
			if strings.Contains(r.Message, "13") || strings.Contains(r.Message, "operations[") {
				t.Errorf("%s: per-op message carries the op index: %q", member, r.Message)
			}
			results = append(results, r)
		}
	}
	h := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, http.StatusOK, visibleGroupBatchResults(d, rbac.RequestPrincipal(r), results))
	})
	req := orgEnfIdentity(newRequestWithChiParam("POST", "/api/v1/groups/g/batch", "id", "g", nil), orgEnfMemberOrg)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, f.rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)
	if body := w.Body.String(); strings.Contains(body, orgEnfTenantOut) || !strings.Contains(body, orgEnfTenantIn) {
		t.Errorf("visible results = %s; want %s only", body, orgEnfTenantIn)
	}
}

// freshRefusalStatus: a PUT refused on the fresh base answers 403; a batch
// answers 422 rejected with the refusal per op (#2900 F4).
func freshRefusalStatus(batch bool) int {
	if batch {
		return http.StatusUnprocessableEntity
	}
	return http.StatusForbidden
}

// freshRefusal reads the refused tenant, code and message from either form:
// a PUT's error envelope, or a rejected batch's first "error" result.
func freshRefusal(t *testing.T, w *httptest.ResponseRecorder) (tenant, code, msg string) {
	t.Helper()
	if w.Code == http.StatusUnprocessableEntity {
		var resp BatchResponse
		_ = json.Unmarshal(w.Body.Bytes(), &resp)
		for _, r := range resp.Results {
			if r.Status == "error" {
				return r.TenantID, string(r.Code), r.Message
			}
		}
		return "", "", ""
	}
	var env map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &env)
	tenant, _ = env["tenant_id"].(string)
	code, _ = env["code"].(string)
	msg, _ = env["error"].(string)
	return tenant, code, msg
}
