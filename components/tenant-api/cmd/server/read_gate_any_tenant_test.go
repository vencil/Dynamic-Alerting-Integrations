package main

// #2520, end to end through the production router (buildRouter): the routes
// whose URL names no tenant used to demand read on the platform wildcard "*",
// which only a rule listing "*" passes, so a caller granted ["alpha-one"] or
// ["alpha-*"] got 403 on /me, /tenants, /groups … before the handlers' own
// per-tenant filters ran. They now take the any-tenant gate
// (rbac.MiddlewareAnyTenant → GrantsSomeTenant) and the handler filters.
//
// Every assertion that a scoped caller gets 200 fails if a route is put back
// on Middleware(PermRead, nil): that gate answers those callers 403.

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/tenantorg"
	"github.com/vencil/tenant-api/internal/views"
	"github.com/vencil/tenant-api/internal/ws"
)

// The rules the fixture serves. Callers pick one by IdP group:
//
//	exact   — one tenant by exact id, read + write
//	prefix  — a prefix pattern, read only
//	admins  — the platform wildcard, every permission
//	ghost   — read on an id no tenant carries
//	orgs    — "*" behind org-scope on the `org` claim
//	(none)  — a group no rule names
const anyTenantRBACYAML = `groups:
  - name: exact
    tenants: ["alpha-one"]
    permissions: [read, write]
  - name: prefix
    tenants: ["alpha-*"]
    permissions: [read]
  - name: admins
    tenants: ["*"]
    permissions: [read, write, admin]
  - name: ghost
    tenants: ["no-such-tenant"]
    permissions: [read]
  - name: orgs
    tenants: ["*"]
    permissions: [read]
    org-scope: org
`

const orgClaimHeader = "X-Auth-Request-Org"

// gatePRTracker serves a fixed pending-PR list for GET /prs.
type gatePRTracker struct{ prs []platform.PRInfo }

func (gatePRTracker) WatchLoop(<-chan struct{})       {}
func (g gatePRTracker) PendingPRs() []platform.PRInfo { return g.prs }
func (g gatePRTracker) PendingPRForTenant(id string) (platform.PRInfo, bool) {
	for _, pr := range g.prs {
		if pr.TenantID == id {
			return pr, true
		}
	}
	return platform.PRInfo{}, false
}
func (gatePRTracker) HasPendingPR(string) bool   { return false }
func (gatePRTracker) ClaimTenant(string) bool    { return false }
func (gatePRTracker) ReleaseClaim(string)        {}
func (gatePRTracker) RegisterPR(platform.PRInfo) {}
func (gatePRTracker) LastSyncTime() time.Time    { return time.Time{} }
func (gatePRTracker) RefreshNow(context.Context) {}

type gateFixture struct {
	router *chi.Mux
	hub    *ws.Hub
	tasks  *async.Manager
}

func writeFixtureFile(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
}

// newGateFixture builds the production router over three tenants (alpha-one
// in ORG-A, alpha-two in ORG-B, beta-one unlabeled), one group per tenant
// plus a mixed one, a saved view, and one pending PR per tenant.
func newGateFixture(t *testing.T, rbacYAML string, orgEnforce bool) *gateFixture {
	t.Helper()
	return newGateFixtureWith(t, rbacYAML, orgEnforce, routerDeps{})
}

// newGateFixtureWith is newGateFixture with the router knobs a test sets
// itself: RequestTimeout and RateLimit (nil = pass-through) are taken from
// over; Deps, RBAC and Events are always the fixture's.
func newGateFixtureWith(t *testing.T, rbacYAML string, orgEnforce bool, over routerDeps) *gateFixture {
	t.Helper()
	confd := t.TempDir()
	for _, id := range []string{"alpha-one", "alpha-two", "beta-one"} {
		writeFixtureFile(t, confd, id+".yaml", "tenants:\n  "+id+":\n    cpu: \"70\"\n")
	}
	stateDir := t.TempDir()
	writeFixtureFile(t, stateDir, statestore.KeyGroups, `groups:
  g-alpha-one: {label: one, members: [alpha-one]}
  g-alpha-two: {label: two, members: [alpha-two]}
  g-beta-one: {label: beta, members: [beta-one]}
  g-mixed: {label: mixed, members: [alpha-one, beta-one]}
`)
	writeFixtureFile(t, stateDir, statestore.KeyViews, `views:
  v-all: {label: everything, filters: {environment: production}}
`)
	store, err := statestore.NewDir(stateDir, nil)
	if err != nil {
		t.Fatal(err)
	}

	// rbacYAML == "" is open mode: no --rbac path at all (not an empty file,
	// which is MED-8's configured-but-empty).
	rbacPath := ""
	if rbacYAML != "" {
		rbacDir := t.TempDir()
		writeFixtureFile(t, rbacDir, "_rbac.yaml", rbacYAML)
		rbacPath = filepath.Join(rbacDir, "_rbac.yaml")
	}
	mgr, err := rbac.NewManager(rbacPath, map[string]string{"org": orgClaimHeader})
	if err != nil {
		t.Fatal(err)
	}
	torg := tenantorg.NewForTest(&tenantorg.Config{TenantOrgs: map[string][]string{
		"alpha-one": {"ORG-A"},
		"alpha-two": {"ORG-B"},
	}})
	mgr.SetOrgResolver(func(id string) []string { o, _ := torg.OrgsForTenant(id); return o })
	if orgEnforce {
		mgr.EnableOrgScopeEnforce()
	}

	tasks := async.NewManager(1)
	t.Cleanup(func() { _ = tasks.Close() })
	hub := ws.NewHubWithConfig(ws.Config{})
	deps := &handler.Deps{
		ConfigDir:   confd,
		RBAC:        mgr,
		TenantOrg:   torg,
		State:       store,
		Groups:      groups.NewManager(store),
		Views:       views.NewManager(store),
		Tasks:       tasks,
		Federation:  &token.Manager{},
		SearchCache: handler.NewTenantSnapshotCache(),
		PRTracker: gatePRTracker{prs: []platform.PRInfo{
			{Number: 1, TenantID: "alpha-one", HeadRef: "tenant-api/alpha-one"},
			{Number: 2, TenantID: "alpha-two", HeadRef: "tenant-api/alpha-two"},
			{Number: 3, TenantID: "beta-one", HeadRef: "tenant-api/beta-one"},
		}},
	}
	rateLimit := over.RateLimit
	if rateLimit == nil {
		rateLimit = func(next http.Handler) http.Handler { return next }
	}
	router := buildRouter(routerDeps{
		Deps:           deps,
		RBAC:           mgr,
		Events:         handler.EventStream(deps, hub),
		RateLimit:      rateLimit,
		RequestTimeout: over.RequestTimeout,
	})
	return &gateFixture{router: router, hub: hub, tasks: tasks}
}

// caller is one identity: its IdP group and, optionally, its org claim.
type caller struct{ group, org string }

func (c caller) email() string { return c.group + "@example.com" }

func (f *gateFixture) do(t *testing.T, c caller, method, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest(method, path, strings.NewReader(body))
	req.Header.Set("X-Forwarded-Email", c.email())
	req.Header.Set("X-Forwarded-Groups", c.group)
	if c.org != "" {
		req.Header.Set(orgClaimHeader, c.org)
	}
	w := httptest.NewRecorder()
	f.router.ServeHTTP(w, req)
	return w
}

// getOK GETs path as c, requires 200 and decodes the body into out.
func (f *gateFixture) getOK(t *testing.T, c caller, path string, out any) {
	t.Helper()
	w := f.do(t, c, http.MethodGet, path, "")
	if w.Code != http.StatusOK {
		t.Fatalf("%s as %q: status %d, want 200 (#2520: the any-tenant gate admits a caller who may read some tenant); body=%s",
			path, c.group, w.Code, w.Body.String())
	}
	if err := json.Unmarshal(w.Body.Bytes(), out); err != nil {
		t.Fatalf("%s as %q: decode: %v; body=%s", path, c.group, err, w.Body.String())
	}
}

// envelopeWithout renders an error envelope without its per-request
// request_id and with id masked, so two answers can be compared byte for byte.
func envelopeWithout(w *httptest.ResponseRecorder, id string) string {
	var env map[string]any
	_ = json.Unmarshal(w.Body.Bytes(), &env)
	delete(env, "request_id")
	b, _ := json.Marshal(env)
	return strings.ReplaceAll(string(b), id, "X")
}

func sortedStrings(in []string) []string {
	out := append([]string{}, in...)
	sort.Strings(out)
	return out
}

func tenantIDsOf(t *testing.T, f *gateFixture, c caller, path string) []string {
	t.Helper()
	var ids []string
	if strings.HasPrefix(path, "/api/v1/tenants/search") {
		var resp struct {
			Items []struct {
				ID string `json:"id"`
			} `json:"items"`
		}
		f.getOK(t, c, path, &resp)
		for _, it := range resp.Items {
			ids = append(ids, it.ID)
		}
	} else {
		var rows []struct {
			ID string `json:"id"`
		}
		f.getOK(t, c, path, &rows)
		for _, r := range rows {
			ids = append(ids, r.ID)
		}
	}
	return sortedStrings(ids)
}

var (
	exactCaller  = caller{group: "exact"}
	prefixCaller = caller{group: "prefix"}
	adminCaller  = caller{group: "admins"}
	ghostCaller  = caller{group: "ghost"}
	noRuleCaller = caller{group: "nobody"}
)

// The tenant list and search: scoped callers pass the gate and see exactly
// their own tenants.
func TestAnyTenantGate_TenantListAndSearch(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)
	cases := []struct {
		c    caller
		want []string
	}{
		{exactCaller, []string{"alpha-one"}},
		{prefixCaller, []string{"alpha-one", "alpha-two"}},
		{adminCaller, []string{"alpha-one", "alpha-two", "beta-one"}},
		{ghostCaller, nil},
		// org axis in shadow: its own org's tenant, plus the unlabeled one
		{caller{group: "orgs", org: "ORG-A"}, []string{"alpha-one", "beta-one"}},
	}
	for _, tc := range cases {
		for _, path := range []string{"/api/v1/tenants", "/api/v1/tenants/search"} {
			if got := tenantIDsOf(t, f, tc.c, path); !reflect.DeepEqual(got, sortedStrings(tc.want)) {
				t.Errorf("%s as %q (org %q) = %v, want %v", path, tc.c.group, tc.c.org, got, tc.want)
			}
		}
	}
}

// /me: a scoped caller now reaches it and sees its own patterns only.
func TestAnyTenantGate_Me(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)
	for _, tc := range []struct {
		c    caller
		want []string
	}{
		{exactCaller, []string{"alpha-one"}},
		{prefixCaller, []string{"alpha-*"}},
		{ghostCaller, []string{"no-such-tenant"}},
	} {
		var me handler.MeResponse
		f.getOK(t, tc.c, "/api/v1/me", &me)
		if got := sortedStrings(me.AccessibleTenants); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("/me as %q: accessible_tenants = %v, want %v", tc.c.group, got, tc.want)
		}
	}
}

// /groups and /groups/{id}: only groups with a readable member, and only the
// readable members; a group of another tenant answers 404 as a missing one.
func TestAnyTenantGate_Groups(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)

	type grp struct {
		ID      string   `json:"id"`
		Members []string `json:"members"`
	}
	listOf := func(c caller) map[string][]string {
		var gs []grp
		f.getOK(t, c, "/api/v1/groups", &gs)
		out := map[string][]string{}
		for _, g := range gs {
			out[g.ID] = sortedStrings(g.Members)
		}
		return out
	}
	if got, want := listOf(exactCaller), map[string][]string{
		"g-alpha-one": {"alpha-one"},
		"g-mixed":     {"alpha-one"}, // beta-one filtered out of the member list
	}; !reflect.DeepEqual(got, want) {
		t.Errorf("/groups as exact = %v, want %v", got, want)
	}
	if got, want := listOf(prefixCaller), map[string][]string{
		"g-alpha-one": {"alpha-one"},
		"g-alpha-two": {"alpha-two"},
		"g-mixed":     {"alpha-one"},
	}; !reflect.DeepEqual(got, want) {
		t.Errorf("/groups as prefix = %v, want %v", got, want)
	}
	if got := listOf(ghostCaller); len(got) != 0 {
		t.Errorf("/groups as ghost = %v, want none", got)
	}

	var one grp
	f.getOK(t, exactCaller, "/api/v1/groups/g-alpha-one", &one)
	if !reflect.DeepEqual(one.Members, []string{"alpha-one"}) {
		t.Errorf("/groups/g-alpha-one as exact: members = %v", one.Members)
	}
	hidden := f.do(t, exactCaller, http.MethodGet, "/api/v1/groups/g-alpha-two", "")
	missing := f.do(t, exactCaller, http.MethodGet, "/api/v1/groups/g-nonexistent", "")
	if hidden.Code != http.StatusNotFound || missing.Code != http.StatusNotFound {
		t.Fatalf("hidden / missing group: %d / %d, want 404 / 404", hidden.Code, missing.Code)
	}
	if envelopeWithout(hidden, "g-alpha-two") != envelopeWithout(missing, "g-nonexistent") {
		t.Errorf("a hidden group must answer exactly like a missing one:\n hidden:  %s\n missing: %s",
			hidden.Body.String(), missing.Body.String())
	}
}

// Group writes take the any-tenant WRITE gate; the per-member write check in
// the handler is the real bar.
func TestAnyTenantGate_GroupWrites(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)

	// read-only everywhere → the write gate refuses before the handler
	if w := f.do(t, prefixCaller, http.MethodDelete, "/api/v1/groups/g-alpha-one", ""); w.Code != http.StatusForbidden {
		t.Errorf("DELETE as read-only prefix caller: %d, want 403; body=%s", w.Code, w.Body.String())
	}
	// write on alpha-one only: a group of another member is still refused
	// (here: invisible, so 404 as a missing group) …
	if w := f.do(t, exactCaller, http.MethodDelete, "/api/v1/groups/g-alpha-two", ""); w.Code != http.StatusNotFound {
		t.Errorf("DELETE g-alpha-two as exact: %d, want 404; body=%s", w.Code, w.Body.String())
	}
	// … a visible group with a member it may not write is 403 …
	if w := f.do(t, exactCaller, http.MethodDelete, "/api/v1/groups/g-mixed", ""); w.Code != http.StatusForbidden {
		t.Errorf("DELETE g-mixed as exact: %d, want 403; body=%s", w.Code, w.Body.String())
	}
	// … and a group of its own tenants is now writable (before #2520: 403 at the route).
	put := f.do(t, exactCaller, http.MethodPut, "/api/v1/groups/g-new",
		`{"label":"mine","members":["alpha-one"]}`)
	if put.Code != http.StatusOK {
		t.Fatalf("PUT g-new as exact: %d, want 200; body=%s", put.Code, put.Body.String())
	}
	if w := f.do(t, exactCaller, http.MethodDelete, "/api/v1/groups/g-alpha-one", ""); w.Code != http.StatusOK {
		t.Errorf("DELETE g-alpha-one as exact: %d, want 200; body=%s", w.Code, w.Body.String())
	}
}

// /tasks/{id}: the submitter sees its task, filtered to the tenants it can read.
func TestAnyTenantGate_Tasks(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)
	id := async.NewTaskID()
	owner := rbac.SourceHumanHopB + ":" + exactCaller.email()
	if _, err := f.tasks.Submit(id, owner, func(context.Context) ([]async.TaskResult, error) {
		return []async.TaskResult{
			{TenantID: "alpha-one", Status: "ok"},
			{TenantID: "beta-one", Status: "ok"},
		}, nil
	}); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(5 * time.Second)
	for {
		if task, ok := f.tasks.Get(id); ok && task.Status == async.TaskCompleted {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("task did not complete")
		}
		time.Sleep(time.Millisecond)
	}

	var task async.Task
	f.getOK(t, exactCaller, "/api/v1/tasks/"+id, &task)
	if len(task.Results) != 1 || task.Results[0].TenantID != "alpha-one" {
		t.Errorf("task results as submitter = %+v, want only alpha-one", task.Results)
	}
	// another scoped caller: the same 404 as a task that does not exist
	if w := f.do(t, prefixCaller, http.MethodGet, "/api/v1/tasks/"+id, ""); w.Code != http.StatusNotFound {
		t.Errorf("task as non-submitter: %d, want 404", w.Code)
	}
}

// /prs: only the PRs of tenants the caller reads.
func TestAnyTenantGate_PRs(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)
	prTenants := func(c caller, query string) []string {
		var resp handler.PRListResponse
		f.getOK(t, c, "/api/v1/prs"+query, &resp)
		var ids []string
		for _, pr := range resp.PendingPRs {
			ids = append(ids, pr.TenantID)
		}
		return sortedStrings(ids)
	}
	if got := prTenants(exactCaller, ""); !reflect.DeepEqual(got, []string{"alpha-one"}) {
		t.Errorf("/prs as exact = %v", got)
	}
	if got := prTenants(prefixCaller, ""); !reflect.DeepEqual(got, []string{"alpha-one", "alpha-two"}) {
		t.Errorf("/prs as prefix = %v", got)
	}
	if got := prTenants(exactCaller, "?tenant=beta-one"); len(got) != 0 {
		t.Errorf("/prs?tenant=beta-one as exact = %v, want empty (no existence oracle)", got)
	}
}

// /events: the stream is reachable and carries only the caller's tenants.
func TestAnyTenantGate_Events(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	req := httptest.NewRequest(http.MethodGet, "/api/v1/events", nil).WithContext(ctx)
	req.Header.Set("X-Forwarded-Email", exactCaller.email())
	req.Header.Set("X-Forwarded-Groups", exactCaller.group)
	w := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		f.router.ServeHTTP(w, req)
		close(done)
	}()
	deadline := time.Now().Add(2 * time.Second)
	for f.hub.ClientCount() != 1 {
		select {
		case <-done:
			t.Fatalf("stream closed before subscribing: status=%d body=%s (want 200 — #2520)", w.Code, w.Body.String())
		default:
		}
		if time.Now().After(deadline) {
			t.Fatalf("no subscriber; status=%d body=%s", w.Code, w.Body.String())
		}
		time.Sleep(time.Millisecond)
	}
	f.hub.Broadcast(handler.ConfigChangeEvent("alpha-two"))
	f.hub.Broadcast(handler.StateChangeEvent("groups"))
	f.hub.Broadcast(handler.ConfigChangeEvent("alpha-one"))
	cancel()
	<-done

	body := w.Body.String()
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, body)
	}
	if strings.Contains(body, "alpha-two") || strings.Contains(body, `"groups"`) {
		t.Errorf("stream carried events the caller may not see: %s", body)
	}
	if !strings.Contains(body, `"tenant_id":"alpha-one"`) {
		t.Errorf("stream lost the caller's own tenant's event: %s", body)
	}
}

// Federation tokens are reachable by scoped callers now; the handler's
// per-tenant admin bar answers a tenant that exists and one that does not
// with the same body.
func TestAnyTenantGate_FederationTokensNoOracle(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)
	existing := f.do(t, exactCaller, http.MethodGet, "/api/v1/federation/tokens?tenant_id=alpha-two", "")
	absent := f.do(t, exactCaller, http.MethodGet, "/api/v1/federation/tokens?tenant_id=alpha-zzz", "")
	if existing.Code != http.StatusForbidden || absent.Code != http.StatusForbidden {
		t.Fatalf("tokens for others' / absent tenant: %d / %d, want 403 / 403", existing.Code, absent.Code)
	}
	// the refusal is the handler's per-tenant bar, not the route gate's
	if !strings.Contains(existing.Body.String(), "admin permission required on tenant alpha-two") {
		t.Errorf("403 did not come from the handler's per-tenant admin bar (route gate refused?): %s", existing.Body.String())
	}
	if a, b := envelopeWithout(existing, "alpha-two"), envelopeWithout(absent, "alpha-zzz"); a != b {
		t.Errorf("403 differs between an existing and an absent tenant:\n %s\n %s", a, b)
	}
}

// What stays on the platform "*" gate: saved views (no ownership model) and
// the platform-admin routes.
func TestAnyTenantGate_PlatformRoutesStayOnStar(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)
	for _, c := range []caller{exactCaller, prefixCaller, ghostCaller, {group: "orgs", org: "ORG-A"}} {
		for _, rt := range []struct{ method, path, body string }{
			{http.MethodGet, "/api/v1/views", ""},
			{http.MethodGet, "/api/v1/views/v-all", ""},
			{http.MethodPut, "/api/v1/views/v-all", `{"label":"x","filters":{}}`},
			{http.MethodDelete, "/api/v1/views/v-all", ""},
			{http.MethodPut, "/api/v1/federation/policy", `{"whitelist":[]}`},
			{http.MethodGet, "/api/v1/audit/tenants/alpha-one/access-report", ""},
			{http.MethodPost, "/api/v1/audit/tenants/alpha-one/access-report/dry-run", `{}`},
			{http.MethodPost, "/api/v1/federation/accounts/backfill", ""},
		} {
			// The org-scoped "*" rule passes the "*" READ gate (org-blind,
			// invariant I6) — views GET is the case to watch; it is a rule
			// listing "*", so it is the views' intended audience today.
			if c.group == "orgs" && rt.method == http.MethodGet && strings.HasPrefix(rt.path, "/api/v1/views") {
				continue
			}
			w := f.do(t, c, rt.method, rt.path, rt.body)
			if w.Code != http.StatusForbidden {
				t.Errorf("%s %s as %q: %d, want 403; body=%s", rt.method, rt.path, c.group, w.Code, w.Body.String())
				continue
			}
			// Which layer refused: the "*" read gate for a caller without a
			// "*" read grant; the handler's own platform-admin bar for the
			// "*" reader that gets past it (the gate is a coarse first layer,
			// not the bar). The views' writes sit behind the "*" WRITE gate,
			// which the read-only "*" rule does not pass.
			fromGate := strings.Contains(w.Body.String(), "insufficient permissions for tenant *")
			if want := c.group != "orgs" || strings.HasPrefix(rt.path, "/api/v1/views"); fromGate != want {
				t.Errorf("%s %s as %q: 403 from the route gate = %v, want %v; body=%s",
					rt.method, rt.path, c.group, fromGate, want, w.Body.String())
			}
		}
	}
	var vs []map[string]any
	f.getOK(t, adminCaller, "/api/v1/views", &vs)
	if len(vs) != 1 {
		t.Errorf("/views as admin = %v, want the one view", vs)
	}
}

// No rule, or no usable grant: still 403 at the gate, on every moved route.
func TestAnyTenantGate_DeniesCallersWithoutAnyGrant(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, true) // org axis enforced
	// /me is not here: it is authentication-only (TestMe_AuthenticatedOnly).
	paths := []string{"/api/v1/tenants", "/api/v1/tenants/search", "/api/v1/groups", "/api/v1/prs", "/api/v1/tasks/x", "/api/v1/federation/policy"}
	for _, c := range []caller{
		noRuleCaller,
		// org-scoped rule, no org claim, enforce on: it can grant no tenant.
		{group: "orgs"},
	} {
		for _, p := range paths {
			w := f.do(t, c, http.MethodGet, p, "")
			if w.Code != http.StatusForbidden {
				t.Errorf("%s as %q (org %q): %d, want 403; body=%s", p, c.group, c.org, w.Code, w.Body.String())
				continue
			}
			if !strings.Contains(w.Body.String(), "no tenant grants read") {
				t.Errorf("%s as %q: 403 is not the any-tenant gate's: %s", p, c.group, w.Body.String())
			}
		}
	}
	// the same org-scoped rule WITH a claim passes and sees its org only
	// (enforce: the unlabeled beta-one is hidden).
	if got := tenantIDsOf(t, f, caller{group: "orgs", org: "ORG-A"}, "/api/v1/tenants"); !reflect.DeepEqual(got, []string{"alpha-one"}) {
		t.Errorf("/tenants as orgs/ORG-A under enforce = %v, want [alpha-one]", got)
	}
}

// MED-8: a configured _rbac.yaml with zero groups still denies everyone —
// the platform admin's group included — on the any-tenant gate. (/me answers
// 200 with no grants there: TestMe_AuthenticatedOnly.)
func TestAnyTenantGate_EmptyConfigFailsClosed(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, "groups: []\n", false)
	for _, p := range []string{"/api/v1/tenants", "/api/v1/groups", "/api/v1/prs", "/api/v1/events"} {
		if w := f.do(t, adminCaller, http.MethodGet, p, ""); w.Code != http.StatusForbidden {
			t.Errorf("%s with an empty configured _rbac.yaml: %d, want 403 (MED-8); body=%s", p, w.Code, w.Body.String())
		}
	}
}

// The "before" half, stated once: the gate these routes used (read on "*")
// refuses every scoped caller the tests above now admit.
func TestAnyTenantGate_StarGateRefusedScopedCallers(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeFixtureFile(t, dir, "_rbac.yaml", anyTenantRBACYAML)
	mgr, err := rbac.NewManager(filepath.Join(dir, "_rbac.yaml"), map[string]string{"org": orgClaimHeader})
	if err != nil {
		t.Fatal(err)
	}
	ok := http.HandlerFunc(func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusOK) })
	for _, c := range []caller{exactCaller, prefixCaller, ghostCaller} {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
		req.Header.Set("X-Forwarded-Email", c.email())
		req.Header.Set("X-Forwarded-Groups", c.group)
		star, anyT := httptest.NewRecorder(), httptest.NewRecorder()
		mgr.Middleware(rbac.PermRead, nil)(ok).ServeHTTP(star, req)
		mgr.MiddlewareAnyTenant(rbac.PermRead)(ok).ServeHTTP(anyT, req)
		if star.Code != http.StatusForbidden || anyT.Code != http.StatusOK {
			t.Errorf("%q: star gate %d / any-tenant gate %d, want 403 / 200", c.group, star.Code, anyT.Code)
		}
	}
}

// The accepted, documented bit (tenant-api-hardening §3.1 "group
// visibility"): a group batch on a visible group with a member the caller can
// neither read nor write is rejected whole, and the visible members are all
// not_applied — "this group has a member you cannot write", the same bit
// PUT/DELETE's "member tenants you cannot view" tells. The hidden member is
// never named and nothing is written.
func TestAnyTenantGate_GroupBatchHiddenUnwritableMemberBit(t *testing.T) {
	t.Parallel()
	f := newGateFixture(t, anyTenantRBACYAML, false)
	w := f.do(t, exactCaller, http.MethodPost, "/api/v1/groups/g-mixed/batch", `{"patch":{"cpu":"80"}}`)
	if w.Code != http.StatusUnprocessableEntity {
		t.Fatalf("group batch on g-mixed as exact: %d, want 422; body=%s", w.Code, w.Body.String())
	}
	var resp handler.GroupBatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if len(resp.Results) != 1 || resp.Results[0].TenantID != "alpha-one" || resp.Results[0].Status != "not_applied" {
		t.Errorf("results = %+v, want only alpha-one, not_applied", resp.Results)
	}
	if strings.Contains(w.Body.String(), "beta-one") {
		t.Errorf("422 names the hidden member: %s", w.Body.String())
	}
}
