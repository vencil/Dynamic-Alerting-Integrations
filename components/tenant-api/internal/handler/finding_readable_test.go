package handler

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

const findingReadableRBAC = `groups:
  - name: fr-admins
    tenants: ["*"]
    permissions: [read, write, admin]
  - name: fr-scoped
    tenants: ["visible", "a.yml"]
    permissions: [read, write]
`

// disclosedTo runs disclosedFindings for one file-only, related finding as
// the caller in group, through the RBAC middleware that stamps the principal
// (authorized on `visible`, which both groups may read).
func disclosedTo(t *testing.T, mgr *rbac.Manager, group, file string) bool {
	t.Helper()
	d := &Deps{RBAC: mgr, TenantOrg: tenantorg.NewForTest(&tenantorg.Config{})}
	var shown []generator.Finding
	h := func(_ http.ResponseWriter, r *http.Request) {
		f := file
		shown, _ = disclosedFindings(d, r, []generator.Finding{{Kind: "k", File: &f, Related: true}})
	}
	req := httptest.NewRequest("GET", "/x", nil)
	req.Header.Set("X-Forwarded-Email", "fr@example.com")
	req.Header.Set("X-Forwarded-Groups", group)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, mgr, rbac.PermRead, func(*http.Request) string { return "visible" }).ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("%s: middleware answered %d: %s", group, w.Code, w.Body.String())
	}
	return len(shown) == 1
}

// TestFindingReadable_FileNamesItsTenantByTheOneRule pins who sees a
// file-only generator finding (#3206). The file's tenant is the one name rule
// (confdname.TenantNamedBy), and a file that names no tenant by it is shown
// only to a caller who sees the whole tree — never passed to a scoped caller
// on the ground that it names nobody (fail-safe).
//
// Each row is asked once as a caller who sees the whole tree (fr-admins) and
// once as a caller who may read `visible` and `a.yml` only (fr-scoped).
func TestFindingReadable_FileNamesItsTenantByTheOneRule(t *testing.T) {
	t.Parallel()
	mgr := newRBACManager(t, findingReadableRBAC)
	for _, tc := range []struct {
		file   string
		scoped bool // shown to fr-scoped; fr-admins is shown every row
	}{
		{"visible.yaml", true},
		{"Secret.yaml", false},
		{"Secret.YAML", false}, // the extension in any case: tenant `Secret`
		{"team/Secret.yml", false},
		// Only the last extension is stripped: tenant `a.yml`, which the
		// scoped caller may read (the hand-written rule read `a`).
		{"a.yml.yaml", true},
		{"Secret.yml.yaml", false},
		// Names no tenant by the rule: withheld from a scoped caller.
		{".yaml", false},
		{".x.yaml", false},
		{"..yaml", false},
		{"_defaults.yaml", false},
		{"team/_domain_policy.yaml", false},
		{"notes.txt", false},
	} {
		if !disclosedTo(t, mgr, "fr-admins", tc.file) {
			t.Errorf("%s: withheld from a caller who sees the whole tree", tc.file)
		}
		if got := disclosedTo(t, mgr, "fr-scoped", tc.file); got != tc.scoped {
			t.Errorf("%s: shown to the scoped caller = %v, want %v", tc.file, got, tc.scoped)
		}
	}
}
