package main

// GET /api/v1/me is authentication-only (#2520 owner decision): like OIDC
// userinfo or GitHub GET /user it answers "who am I and what do my rules
// say", so it requires an identity and no grant. A caller with no effective
// grant gets 200 with its own (possibly empty, possibly non-effective) rules —
// never anyone else's rules or any tenant beyond its own rule patterns.
//
// Every 200 below for a caller without an effective read grant fails if /me
// goes back on the any-tenant gate (that gate answers them 403 "no tenant
// grants read").

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"testing"

	"github.com/vencil/tenant-api/internal/handler"
)

func (f *gateFixture) me(t *testing.T, c caller) handler.MeResponse {
	t.Helper()
	w := f.do(t, c, http.MethodGet, "/api/v1/me", "")
	if w.Code != http.StatusOK {
		t.Fatalf("/me as %q (org %q): status %d, want 200 (authentication-only); body=%s",
			c.group, c.org, w.Code, w.Body.String())
	}
	var me handler.MeResponse
	if err := json.Unmarshal(w.Body.Bytes(), &me); err != nil {
		t.Fatalf("/me decode: %v; body=%s", err, w.Body.String())
	}
	return me
}

// assertOnlyOwnRule: the response names exactly the caller's rule (or none)
// and exactly that rule's tenant patterns.
func assertOnlyOwnRule(t *testing.T, label string, me handler.MeResponse, rules map[string][]string, tenants []string) {
	t.Helper()
	if me.Permissions == nil {
		me.Permissions = map[string][]string{}
	}
	if !reflect.DeepEqual(me.Permissions, rules) {
		t.Errorf("%s: permissions = %v, want %v (only the caller's own matched rules)", label, me.Permissions, rules)
	}
	if got := sortedStrings(me.AccessibleTenants); !reflect.DeepEqual(got, sortedStrings(tenants)) {
		t.Errorf("%s: accessible_tenants = %v, want %v", label, got, tenants)
	}
}

func TestMe_AuthenticatedOnly(t *testing.T) {
	t.Parallel()

	t.Run("org-scoped star rule without org claim under enforce", func(t *testing.T) {
		t.Parallel()
		f := newGateFixture(t, anyTenantRBACYAML, true)
		c := caller{group: "orgs"}
		// The any-tenant gate refuses this caller everywhere else …
		if w := f.do(t, c, http.MethodGet, "/api/v1/tenants", ""); w.Code != http.StatusForbidden {
			t.Fatalf("/tenants as orgs without claim under enforce: %d, want 403", w.Code)
		}
		// … /me shows its own rule, which grants it nothing here, and no org
		// key it carries no value for.
		me := f.me(t, c)
		assertOnlyOwnRule(t, "orgs/no-claim/enforce", me, map[string][]string{"orgs": {"read"}}, []string{"*"})
		if len(me.OrgClaimKeys) != 0 {
			t.Errorf("org_claim_keys = %v, want none (the caller has no org claim)", me.OrgClaimKeys)
		}
		if me.Email != c.email() {
			t.Errorf("email = %q, want %q", me.Email, c.email())
		}
	})

	t.Run("no matching rule", func(t *testing.T) {
		t.Parallel()
		f := newGateFixture(t, anyTenantRBACYAML, true)
		me := f.me(t, noRuleCaller)
		assertOnlyOwnRule(t, "no rule", me, map[string][]string{}, nil)
		if me.Groups == nil || me.AccessibleTenants == nil {
			t.Errorf("groups / accessible_tenants must encode as [] not null: %+v", me)
		}
	})

	t.Run("MED-8 configured but zero groups", func(t *testing.T) {
		t.Parallel()
		f := newGateFixture(t, "groups: []\n", false)
		// The platform admin's group gets no grant — /me says so, and every
		// data route still refuses it.
		me := f.me(t, adminCaller)
		assertOnlyOwnRule(t, "MED-8", me, map[string][]string{}, nil)
		if w := f.do(t, adminCaller, http.MethodGet, "/api/v1/tenants", ""); w.Code != http.StatusForbidden {
			t.Errorf("/tenants under MED-8: %d, want 403", w.Code)
		}
	})

	t.Run("open mode", func(t *testing.T) {
		t.Parallel()
		f := newGateFixture(t, "", false) // no --rbac path
		me := f.me(t, noRuleCaller)
		assertOnlyOwnRule(t, "open mode", me, map[string][]string{}, nil)
	})

	t.Run("scoped caller sees its own rule only", func(t *testing.T) {
		t.Parallel()
		f := newGateFixture(t, anyTenantRBACYAML, false)
		assertOnlyOwnRule(t, "exact", f.me(t, exactCaller),
			map[string][]string{"exact": {"read", "write"}}, []string{"alpha-one"})
	})

	t.Run("unauthenticated", func(t *testing.T) {
		t.Parallel()
		f := newGateFixture(t, anyTenantRBACYAML, false)
		req := httptest.NewRequest(http.MethodGet, "/api/v1/me", nil)
		req.Header.Set("X-Forwarded-Groups", "admins") // groups without an identity
		w := httptest.NewRecorder()
		f.router.ServeHTTP(w, req)
		if w.Code != http.StatusUnauthorized {
			t.Errorf("/me without X-Forwarded-Email: %d, want 401; body=%s", w.Code, w.Body.String())
		}
	})
}
