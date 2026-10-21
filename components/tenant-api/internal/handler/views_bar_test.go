package handler

// #3160: the saved-views bar. A view belongs to no tenant and no org, so the
// views handlers bar on a "*" rule WITHOUT org-scope (rbac.PlatformNonOrgScoped)
// — read for GET (open mode reads too, via readsViews), write for PUT/DELETE.
// The route gates (Middleware(perm, nil)) are org-blind and admit an
// org-scoped "*" rule; every 403 below therefore comes from the handler. The
// requests go through that same route gate, as routed in production.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/statestore"
	"github.com/vencil/tenant-api/internal/views"
)

const (
	viewsBarOrgGroup      = "views-org-star"      // "*", read+write, org-scoped
	viewsBarPlatformGroup = "views-platform-star" // "*", read+write, no org-scope
	viewsBarReaderGroup   = "views-platform-read" // "*", read only, no org-scope
	viewsBarEnvGroup      = "views-env-read"      // "*", read, environment-restricted
	viewsBarSeedID        = "seeded"
	viewsBarNewID         = "fresh"
)

const viewsBarRBACYAML = `groups:
  - name: ` + viewsBarOrgGroup + `
    tenants: ["*"]
    permissions: [read, write]
    org-scope: org
  - name: ` + viewsBarPlatformGroup + `
    tenants: ["*"]
    permissions: [read, write]
  - name: ` + viewsBarReaderGroup + `
    tenants: ["*"]
    permissions: [read]
  - name: ` + viewsBarEnvGroup + `
    tenants: ["*"]
    permissions: [read]
    environments: ["production"]
`

const viewsBarSeed = "views:\n  " + viewsBarSeedID + ":\n    label: Seeded\n    filters:\n      environment: production\n"

// viewsBarDeps builds Deps over a config dir holding one seeded view, with
// rbacMgr as both the route gate's and the handler's manager.
func viewsBarDeps(t *testing.T, rbacMgr *rbac.Manager) (*Deps, string) {
	t.Helper()
	configDir := setupConfigDir(t, map[string]string{statestore.KeyViews: viewsBarSeed})
	return &Deps{
		ConfigDir: configDir,
		State:     mustDirState(configDir),
		Views:     views.NewDirManager(configDir),
		RBAC:      rbacMgr,
	}, configDir
}

func viewsBarRBAC(t *testing.T, enforce bool) *rbac.Manager {
	t.Helper()
	m := newRBACManagerWithClaims(t, viewsBarRBACYAML, map[string]string{"org": orgEnfClaimHeader})
	if enforce {
		m.EnableOrgScopeEnforce()
	}
	return m
}

func viewsBarIdentity(req *http.Request, group, org string) *http.Request {
	req.Header.Set("X-Forwarded-Email", "views-caller@example.com")
	req.Header.Set("X-Forwarded-Groups", group)
	if org != "" {
		req.Header.Set(orgEnfClaimHeader, org)
	}
	return req
}

// viewsBarCall runs one views request through the route gate the router
// mounts for it (platformRead / platformWrite) and the handler.
func viewsBarCall(t *testing.T, d *Deps, method, group, org string) *httptest.ResponseRecorder {
	t.Helper()
	var (
		req  *http.Request
		h    http.HandlerFunc
		perm = rbac.PermRead
	)
	switch method {
	case "LIST":
		req = httptest.NewRequest("GET", "/api/v1/views", nil)
		h = ListViews(d)
	case "GET":
		req = newRequestWithChiParam("GET", "/api/v1/views/"+viewsBarSeedID, "id", viewsBarSeedID, nil)
		h = GetView(d)
	case "PUT":
		req = newRequestWithChiParam("PUT", "/api/v1/views/"+viewsBarNewID, "id", viewsBarNewID,
			bytes.NewBufferString(`{"label":"Fresh","filters":{"environment":"staging"}}`))
		req.Header.Set("Content-Type", "application/json")
		h, perm = PutView(d), rbac.PermWrite
	case "DELETE":
		req = newRequestWithChiParam("DELETE", "/api/v1/views/"+viewsBarSeedID, "id", viewsBarSeedID, nil)
		h, perm = DeleteView(d), rbac.PermWrite
	default:
		t.Fatalf("unknown method %q", method)
	}
	req = viewsBarIdentity(req, group, org)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, d.RBAC, perm, nil).ServeHTTP(w, req)
	return w
}

func viewsBarFile(t *testing.T, configDir string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join(configDir, statestore.KeyViews))
	if err != nil {
		t.Fatalf("read _views.yaml: %v", err)
	}
	return string(b)
}

func TestViewsBar_OrgScopedStarRefused(t *testing.T) {
	t.Parallel()
	for _, mode := range []struct {
		name    string
		enforce bool
	}{{"enforce", true}, {"shadow", false}} {
		t.Run(mode.name, func(t *testing.T) {
			t.Parallel()

			// Writes: an org-scoped "*" writer is refused whatever org it
			// carries, and _views.yaml is left byte-identical.
			for _, method := range []string{"PUT", "DELETE"} {
				for _, org := range []string{orgEnfMemberOrg, orgEnfOutsiderOrg} {
					t.Run(method+"_org_scoped_403_unchanged_"+org, func(t *testing.T) {
						t.Parallel()
						d, configDir := viewsBarDeps(t, viewsBarRBAC(t, mode.enforce))
						w := viewsBarCall(t, d, method, viewsBarOrgGroup, org)
						if w.Code != http.StatusForbidden {
							t.Fatalf("status = %d, want 403 for an org-scoped \"*\" writer; body=%s", w.Code, w.Body.String())
						}
						if !strings.Contains(w.Body.String(), "non-org-scoped") {
							t.Errorf("403 body does not name the non-org-scoped bar; body=%s", w.Body.String())
						}
						if got := viewsBarFile(t, configDir); got != viewsBarSeed {
							t.Errorf("denied %s changed _views.yaml:\n got %q\nwant %q", method, got, viewsBarSeed)
						}
					})
				}
			}

			// Contrast: a non-org-scoped "*" writer writes.
			t.Run("PUT_non_org_scoped_200_written", func(t *testing.T) {
				t.Parallel()
				d, configDir := viewsBarDeps(t, viewsBarRBAC(t, mode.enforce))
				w := viewsBarCall(t, d, "PUT", viewsBarPlatformGroup, orgEnfOutsiderOrg)
				if w.Code != http.StatusOK {
					t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
				}
				if !strings.Contains(viewsBarFile(t, configDir), viewsBarNewID+":") {
					t.Errorf("allowed PUT did not write view %q: %s", viewsBarNewID, viewsBarFile(t, configDir))
				}
			})
			t.Run("DELETE_non_org_scoped_200_removed", func(t *testing.T) {
				t.Parallel()
				d, configDir := viewsBarDeps(t, viewsBarRBAC(t, mode.enforce))
				w := viewsBarCall(t, d, "DELETE", viewsBarPlatformGroup, "")
				if w.Code != http.StatusOK {
					t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
				}
				if strings.Contains(viewsBarFile(t, configDir), viewsBarSeedID+":") {
					t.Errorf("allowed DELETE left view %q in place", viewsBarSeedID)
				}
			})
			// A non-org-scoped "*" READER may not write: the level is the
			// caller's, not a bare "is non-org-scoped" check.
			t.Run("PUT_non_org_scoped_reader_403", func(t *testing.T) {
				t.Parallel()
				d, configDir := viewsBarDeps(t, viewsBarRBAC(t, mode.enforce))
				w := viewsBarCall(t, d, "PUT", viewsBarReaderGroup, "")
				if w.Code != http.StatusForbidden {
					t.Fatalf("status = %d, want 403; body=%s", w.Code, w.Body.String())
				}
				if got := viewsBarFile(t, configDir); got != viewsBarSeed {
					t.Errorf("denied PUT changed _views.yaml: %q", got)
				}
			})

			// Reads: an org-scoped "*" reader is refused both GETs; a
			// non-org-scoped one reads, environment-restricted or not.
			for _, method := range []string{"LIST", "GET"} {
				t.Run(method+"_org_scoped_403", func(t *testing.T) {
					t.Parallel()
					d, _ := viewsBarDeps(t, viewsBarRBAC(t, mode.enforce))
					w := viewsBarCall(t, d, method, viewsBarOrgGroup, orgEnfMemberOrg)
					if w.Code != http.StatusForbidden {
						t.Fatalf("status = %d, want 403 for an org-scoped \"*\" reader; body=%s", w.Code, w.Body.String())
					}
					if strings.Contains(w.Body.String(), "Seeded") {
						t.Errorf("403 body carries view content: %s", w.Body.String())
					}
				})
				for _, group := range []string{viewsBarReaderGroup, viewsBarEnvGroup} {
					t.Run(method+"_non_org_scoped_200_"+group, func(t *testing.T) {
						t.Parallel()
						d, _ := viewsBarDeps(t, viewsBarRBAC(t, mode.enforce))
						w := viewsBarCall(t, d, method, group, "")
						if w.Code != http.StatusOK {
							t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
						}
						if !strings.Contains(w.Body.String(), "Seeded") {
							t.Errorf("200 body lacks the seeded view: %s", w.Body.String())
						}
					})
				}
			}
		})
	}
}

// Open mode (no _rbac.yaml) still reads the views — through the route gate
// and, called bare, through the handler's own bar — and still may not write
// them (open mode grants read only). The fail-closed-empty state (a configured
// _rbac.yaml with zero groups) reads nothing, in the handler as at the route.
func TestViewsBar_ZeroGroupStates(t *testing.T) {
	t.Parallel()
	t.Run("open_mode_reads", func(t *testing.T) {
		t.Parallel()
		for _, method := range []string{"LIST", "GET"} {
			d, _ := viewsBarDeps(t, newRBACManager(t, ""))
			if w := viewsBarCall(t, d, method, "anyone", ""); w.Code != http.StatusOK {
				t.Fatalf("%s: status = %d, want 200 in open mode; body=%s", method, w.Code, w.Body.String())
			}
		}
	})
	t.Run("open_mode_handler_bar", func(t *testing.T) {
		t.Parallel()
		d, configDir := viewsBarDeps(t, newRBACManager(t, ""))
		w := httptest.NewRecorder()
		ListViews(d)(w, httptest.NewRequest("GET", "/api/v1/views", nil))
		if w.Code != http.StatusOK {
			t.Fatalf("bare ListViews: status = %d, want 200 in open mode; body=%s", w.Code, w.Body.String())
		}
		pw := httptest.NewRecorder()
		PutView(d)(pw, newRequestWithChiParam("PUT", "/api/v1/views/"+viewsBarNewID, "id", viewsBarNewID,
			bytes.NewBufferString(`{"label":"Fresh","filters":{"environment":"staging"}}`)))
		if pw.Code != http.StatusForbidden {
			t.Fatalf("bare PutView: status = %d, want 403 in open mode; body=%s", pw.Code, pw.Body.String())
		}
		if got := viewsBarFile(t, configDir); got != viewsBarSeed {
			t.Errorf("open-mode PUT changed _views.yaml: %q", got)
		}
	})
	t.Run("fail_closed_empty_reads_nothing", func(t *testing.T) {
		t.Parallel()
		d, _ := viewsBarDeps(t, newRBACManager(t, "groups: []\n"))
		if w := viewsBarCall(t, d, "LIST", "anyone", ""); w.Code != http.StatusForbidden {
			t.Fatalf("routed: status = %d, want 403; body=%s", w.Code, w.Body.String())
		}
		w := httptest.NewRecorder()
		ListViews(d)(w, httptest.NewRequest("GET", "/api/v1/views", nil))
		if w.Code != http.StatusForbidden {
			t.Fatalf("bare ListViews: status = %d, want 403 in fail-closed-empty; body=%s", w.Code, w.Body.String())
		}
	})
}

// A views change event goes to exactly the callers who may GET the views
// (readsViews), not to the narrower every-tenant bar other tenantless events
// use: an environment-restricted "*" reader gets it, an org-scoped one does
// not (#3160).
func TestViewsBar_EventMatchesGET(t *testing.T) {
	t.Parallel()
	m := viewsBarRBAC(t, true)
	d := &Deps{RBAC: m}
	viewsEvt := StateChangeEvent(viewsEntity)
	groupsEvt := StateChangeEvent("groups")
	if viewsEvt.Tenant != "" {
		t.Fatalf("premise broken: the views event is addressed to tenant %q", viewsEvt.Tenant)
	}
	for _, c := range []struct {
		group      string
		claims     map[string]string
		wantViews  bool
		wantGroups bool
	}{
		{viewsBarOrgGroup, map[string]string{"org": orgEnfMemberOrg}, false, false},
		{viewsBarReaderGroup, nil, true, true},
		{viewsBarEnvGroup, nil, true, false},
	} {
		p := &rbac.VerifiedPrincipal{Groups: []string{c.group}, Claims: c.claims}
		if got := eventVisible(d, p, viewsEvt); got != c.wantViews {
			t.Errorf("%s: views event visible = %v, want %v", c.group, got, c.wantViews)
		}
		if got := eventVisible(d, p, groupsEvt); got != c.wantGroups {
			t.Errorf("%s: groups event visible = %v, want %v", c.group, got, c.wantGroups)
		}
	}
	open := &Deps{RBAC: newRBACManager(t, "")}
	if !eventVisible(open, &rbac.VerifiedPrincipal{Groups: []string{"anyone"}}, viewsEvt) {
		t.Errorf("open mode lost the views event")
	}
	closed := &Deps{RBAC: newRBACManager(t, "groups: []\n")}
	if eventVisible(closed, &rbac.VerifiedPrincipal{Groups: []string{"anyone"}}, viewsEvt) {
		t.Errorf("fail-closed-empty received the views event")
	}
	// The wire shape is unchanged: the entity rides in tenant_id.
	b, err := json.Marshal(viewsEvt)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), `"tenant_id":"views"`) {
		t.Errorf("views event wire shape changed: %s", b)
	}
}
