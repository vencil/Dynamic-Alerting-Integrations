package rbac

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// GrantsSomeTenant (#2520) is the any-tenant route gate's bar. These tests
// pin it from two sides: a table of the cases the decision turns on, and an
// equivalence with the per-tenant read decision (AllowedInOrgRead) over
// witness tenants — "passes the gate" must mean exactly "some tenant could be
// shown", never more (a gate wider than the filters) and never less (a caller
// the filters would serve, refused at the door).

func gstPrincipal(groups []string, claims map[string]string) *VerifiedPrincipal {
	return &VerifiedPrincipal{Email: "u@example.com", Groups: groups, Claims: claims}
}

func TestGrantsSomeTenant_Cases(t *testing.T) {
	t.Parallel()
	cfg := &RBACConfig{Groups: []GroupRule{
		{Name: "exact", Tenants: []string{"svc-one"}, Permissions: []Permission{PermRead}},
		{Name: "prefix-rw", Tenants: []string{"svc-*"}, Permissions: []Permission{PermRead, PermWrite}},
		{Name: "admin", Tenants: []string{"*"}, Permissions: []Permission{PermAdmin}},
		{Name: "no-tenants", Tenants: nil, Permissions: []Permission{PermRead}},
		{Name: "bad-pattern", Tenants: []string{"**"}, Permissions: []Permission{PermRead}}, // NewForTest skips validateConfig
		{Name: "no-perms", Tenants: []string{"*"}},
		{Name: "org", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
		{Name: "env", Tenants: []string{"svc-*"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}},
	}}
	shadow := NewForTest(cfg)
	enforce := NewForTest(cfg)
	enforce.EnableOrgScopeEnforce()

	cases := []struct {
		name          string
		groups        []string
		claims        map[string]string
		want          Permission
		shadow, enfor bool
	}{
		{"exact id grants read", []string{"exact"}, nil, PermRead, true, true},
		{"exact id does not grant write", []string{"exact"}, nil, PermWrite, false, false},
		{"prefix grants write", []string{"prefix-rw"}, nil, PermWrite, true, true},
		{"admin covers read", []string{"admin"}, nil, PermRead, true, true},
		{"rule naming no tenant", []string{"no-tenants"}, nil, PermRead, false, false},
		{"malformed pattern only", []string{"bad-pattern"}, nil, PermRead, false, false},
		{"rule granting no permission", []string{"no-perms"}, nil, PermRead, false, false},
		{"no matching rule", []string{"stranger"}, nil, PermRead, false, false},
		{"anonymous", nil, nil, PermRead, false, false},
		{"org-scoped with claim", []string{"org"}, map[string]string{"org": "ORG-A"}, PermRead, true, true},
		{"org-scoped without claim: shadow only", []string{"org"}, nil, PermRead, true, false},
		{"metadata-restricted rule still counts", []string{"env"}, nil, PermRead, true, true},
	}
	for _, tc := range cases {
		p := gstPrincipal(tc.groups, tc.claims)
		if tc.groups == nil {
			p = nil
		}
		if got := shadow.GrantsSomeTenant(p, tc.want); got != tc.shadow {
			t.Errorf("%s (org shadow): got %v, want %v", tc.name, got, tc.shadow)
		}
		if got := enforce.GrantsSomeTenant(p, tc.want); got != tc.enfor {
			t.Errorf("%s (org enforce): got %v, want %v", tc.name, got, tc.enfor)
		}
	}
}

// MED-8 and open mode are unchanged from the "*" gate.
func TestGrantsSomeTenant_ZeroGroups(t *testing.T) {
	t.Parallel()
	open := NewForTest(&RBACConfig{})
	failClosed := NewForTest(&RBACConfig{})
	failClosed.failClosedOnEmpty = true
	p := gstPrincipal([]string{"anyone"}, nil)
	for _, want := range []Permission{PermRead, PermWrite, PermAdmin} {
		if failClosed.GrantsSomeTenant(p, want) {
			t.Errorf("fail-closed empty config granted %s (MED-8)", want)
		}
		if got, exp := open.GrantsSomeTenant(p, want), want == PermRead; got != exp {
			t.Errorf("open mode %s: got %v, want %v", want, got, exp)
		}
		if got, exp := open.GrantsSomeTenant(p, want), open.Allowed(p, "*", want); got != exp {
			t.Errorf("open mode %s: any-tenant %v differs from the \"*\" gate %v", want, got, exp)
		}
	}
}

// witnessIDs returns tenant ids a pattern matches: the id itself, a concrete
// id under a prefix, any id for "*".
func witnessIDs(pat string) []string {
	switch {
	case pat == "*":
		return []string{"anything"}
	case strings.HasSuffix(pat, "*"):
		return []string{strings.TrimSuffix(pat, "*") + "w"}
	default:
		return []string{pat}
	}
}

// GrantsSomeTenant ⇔ some witness tenant (an id the caller's patterns name,
// with no org label, the caller's org, or another org) passes the per-tenant
// read decision the filters use. Run in both org modes.
func TestGrantsSomeTenant_EquivalentToSomeReadableWitness(t *testing.T) {
	t.Parallel()
	rules := []GroupRule{
		{Name: "a", Tenants: []string{"svc-one"}, Permissions: []Permission{PermRead}},
		{Name: "b", Tenants: []string{"svc-*"}, Permissions: []Permission{PermWrite}},
		{Name: "c", Tenants: []string{"*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
		{Name: "d", Tenants: []string{"ops-*"}, Permissions: []Permission{PermRead}, OrgScope: "org"},
		{Name: "e", Tenants: nil, Permissions: []Permission{PermAdmin}},
		{Name: "f", Tenants: []string{"*"}},
	}
	principals := []*VerifiedPrincipal{nil}
	for _, r := range rules {
		principals = append(principals,
			gstPrincipal([]string{r.Name}, nil),
			gstPrincipal([]string{r.Name}, map[string]string{"org": "ORG-A"}))
	}
	principals = append(principals, gstPrincipal([]string{"c", "e"}, nil), gstPrincipal([]string{"zzz"}, nil))

	for _, enforceOrg := range []bool{false, true} {
		m := NewForTest(&RBACConfig{Groups: rules})
		if enforceOrg {
			m.EnableOrgScopeEnforce()
		}
		for _, p := range principals {
			for _, want := range []Permission{PermRead, PermWrite} {
				witness := false
				for _, r := range rules {
					for _, pat := range r.Tenants {
						for _, id := range witnessIDs(pat) {
							for _, orgs := range [][]string{nil, {"ORG-A"}, {"ORG-B"}} {
								if m.AllowedInOrgRead(p, id, want, orgs) {
									witness = true
								}
							}
						}
					}
				}
				if got := m.GrantsSomeTenant(p, want); got != witness {
					var groups []string
					var claims map[string]string
					if p != nil {
						groups, claims = p.Groups, p.Claims
					}
					t.Errorf("enforce=%v groups=%v claims=%v want=%s: GrantsSomeTenant=%v, some readable witness=%v",
						enforceOrg, groups, claims, want, got, witness)
				}
			}
		}
	}
}

// The middleware: 403 with the any-tenant body when no rule grants, 200 and
// the principal in context when one does; never a would-deny count.
func TestMiddlewareAnyTenant(t *testing.T) {
	t.Parallel()
	m := NewForTest(&RBACConfig{Groups: []GroupRule{
		{Name: "scoped", Tenants: []string{"svc-one"}, Permissions: []Permission{PermRead}, Environments: []string{"production"}},
	}})
	rec := newFakeScopeRecorder()
	m.SetScopeAuditor(rec)
	var seen *VerifiedPrincipal
	h := m.MiddlewareAnyTenant(PermRead)(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = RequestPrincipal(r)
		w.WriteHeader(http.StatusOK)
	}))
	serve := func(group string) *httptest.ResponseRecorder {
		req := httptest.NewRequest(http.MethodGet, "/api/v1/tenants", nil)
		req.Header.Set("X-Forwarded-Email", "u@example.com")
		req.Header.Set("X-Forwarded-Groups", group)
		w := httptest.NewRecorder()
		h.ServeHTTP(w, req)
		return w
	}
	if w := serve("scoped"); w.Code != http.StatusOK || seen == nil || seen.Email != "u@example.com" {
		t.Errorf("scoped caller: %d, principal %+v; want 200 with the principal in context", w.Code, seen)
	}
	w := serve("stranger")
	if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), `"code":"FORBIDDEN"`) ||
		!strings.Contains(w.Body.String(), "no tenant grants read") {
		t.Errorf("stranger: %d %s; want 403 FORBIDDEN naming no tenant", w.Code, w.Body.String())
	}
	if len(rec.counts) != 0 {
		t.Errorf("the gate recorded would-deny observations %v; it judges no tenant and must record none", rec.counts)
	}
	req := httptest.NewRequest(http.MethodGet, "/api/v1/tenants", nil)
	w401 := httptest.NewRecorder()
	h.ServeHTTP(w401, req)
	if w401.Code != http.StatusUnauthorized {
		t.Errorf("no identity: %d, want 401", w401.Code)
	}
}
