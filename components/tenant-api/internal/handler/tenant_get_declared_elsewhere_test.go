package handler

// #3172: GET /tenants/{id} carries declared_elsewhere — whether a conf.d file
// other than the tenant's own declares the id — the condition of the #2078
// guard's 409 TENANT_DECLARED_ELSEWHERE, and, when the own file declares the
// id too, of /effective's 409. ⛔ One bit: the body never names the other
// file or the other tenants it declares.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/rbac"
)

const (
	// The tenant under test and a neighbour the other file also declares.
	// Placeholders, not real tenant ids.
	deSubject   = "de-subject"
	deNeighbour = "de-neighbour"
	deOtherDir  = "team-sibling"
	deOtherFile = deOtherDir + "/sibling-owner.yaml"
)

func deOwnBody() string {
	return "tenants:\n  " + deSubject + ":\n    _silent_mode: \"warning\"\n"
}

// deTree is a conf.d holding deSubject's own top-level file and, in a
// subdirectory, a file declaring deNeighbour — and deSubject too when dup.
// It is a git repository (the diff resolves against HEAD).
func deTree(t *testing.T, ownBody string, dup bool) string {
	t.Helper()
	dir := setupConfigDir(t, map[string]string{deSubject + ".yaml": ownBody})
	other := "tenants:\n  " + deNeighbour + ":\n    _silent_mode: \"critical\"\n"
	if dup {
		other += "  " + deSubject + ":\n    _silent_mode: \"critical\"\n"
	}
	writeFile(t, filepath.Join(dir, filepath.FromSlash(deOtherFile)), other)
	initGitRepo(t, dir)
	return dir
}

// getDeclaredElsewhere GETs deSubject and returns the raw declared_elsewhere
// value (nil when the key is absent) and the body.
func getDeclaredElsewhere(t *testing.T, h http.Handler, req *http.Request) (*bool, string) {
	t.Helper()
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	if w.Code != http.StatusOK {
		t.Fatalf("GET status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	var body map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("unmarshal: %v; body=%s", err, w.Body.String())
	}
	raw, ok := body["declared_elsewhere"]
	if !ok {
		return nil, w.Body.String()
	}
	v, isBool := raw.(bool)
	if !isBool {
		t.Fatalf("declared_elsewhere = %#v, want a JSON boolean", raw)
	}
	return &v, w.Body.String()
}

func getSubjectReq() *http.Request {
	return newRequestWithChiParam("GET", "/api/v1/tenants/"+deSubject, "id", deSubject, nil)
}

// The flag is the verdict /effective and /diff give on the same tree: true
// with a 409 from both when another file declares the id, false with neither
// refusing when none does.
func TestGetTenant_DeclaredElsewhereAgreesWithEffectiveAndDiff(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name string
		dup  bool
	}{{"declared by another file", true}, {"declared by its own file only", false}} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := deTree(t, deOwnBody(), tc.dup)
			d := &Deps{ConfigDir: dir, Writer: newTestWriter(dir)}

			got, body := getDeclaredElsewhere(t, GetTenant(d), getSubjectReq())
			if got == nil || *got != tc.dup {
				t.Fatalf("declared_elsewhere = %v, want %v; body=%s", fmtBoolPtr(got), tc.dup, body)
			}
			assertNoDeOtherLeak(t, body, dir)

			ew := httptest.NewRecorder()
			GetTenantEffective(d)(ew, newRequestWithChiParam("GET",
				"/api/v1/tenants/"+deSubject+"/effective", "id", deSubject, nil))
			if (ew.Code == http.StatusConflict) != *got {
				t.Errorf("/effective status = %d while GET declared_elsewhere = %v; body=%s", ew.Code, *got, ew.Body.String())
			}

			dw := httptest.NewRecorder()
			DiffTenant(d)(dw, newRequestWithChiParam("POST", "/api/v1/tenants/"+deSubject+"/diff", "id", deSubject,
				bytes.NewBufferString(deOwnBody())))
			refused := dw.Code == http.StatusConflict && strings.Contains(dw.Body.String(), string(errcode.TenantDeclaredElsewhere))
			if refused != *got {
				t.Errorf("/diff = %d %s while GET declared_elsewhere = %v", dw.Code, dw.Body.String(), *got)
			}
		})
	}
}

// The flag is the write guard's verdict, which is wider than /effective's
// duplicate: an own file that declares nothing for the id (a `tenants: {}`
// shell) while another file does is no duplicate — /effective serves the
// other file's declaration — but the #2078 guard refuses a write into the
// own file (/diff answers its 409 here), and GET says so.
func TestGetTenant_DeclaredElsewhereWhenOwnFileIsAShell(t *testing.T) {
	t.Parallel()
	dir := deTree(t, "tenants: {}\n", true)
	d := &Deps{ConfigDir: dir, Writer: newTestWriter(dir)}

	got, body := getDeclaredElsewhere(t, GetTenant(d), getSubjectReq())
	if got == nil || !*got {
		t.Fatalf("declared_elsewhere = %v, want true; body=%s", fmtBoolPtr(got), body)
	}
	dw := httptest.NewRecorder()
	DiffTenant(d)(dw, newRequestWithChiParam("POST", "/api/v1/tenants/"+deSubject+"/diff", "id", deSubject,
		bytes.NewBufferString(deOwnBody())))
	if dw.Code != http.StatusConflict || !strings.Contains(dw.Body.String(), string(errcode.TenantDeclaredElsewhere)) {
		t.Errorf("/diff = %d %s, want 409 %s", dw.Code, dw.Body.String(), errcode.TenantDeclaredElsewhere)
	}
}

// Every 200 shape carries the flag: API-managed, not loadable (config_error)
// and git-managed (the own file declares a second tenant).
func TestGetTenant_DeclaredElsewhereOnEveryShape(t *testing.T) {
	t.Parallel()
	for _, tc := range []struct {
		name, own, wantManagedBy, wantConfigError string
	}{
		{"api-managed", deOwnBody(), managedByAPI, ""},
		{"not loadable", "tenants:\n  " + deSubject + ":\n    mysql_connections: !!int abc\n", managedByAPI, "invalid_config"},
		{"git-managed", deOwnBody() + "  de-third:\n    _silent_mode: \"warning\"\n", managedByGit, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := deTree(t, tc.own, true)
			d := &Deps{ConfigDir: dir, Writer: newTestWriter(dir)}
			got, body := getDeclaredElsewhere(t, GetTenant(d), getSubjectReq())
			var shape struct {
				ManagedBy   string `json:"managed_by"`
				ConfigError string `json:"config_error"`
			}
			_ = json.Unmarshal([]byte(body), &shape)
			if shape.ManagedBy != tc.wantManagedBy || shape.ConfigError != tc.wantConfigError {
				t.Fatalf("shape = %+v, want managed_by %q config_error %q (fixture drifted); body=%s",
					shape, tc.wantManagedBy, tc.wantConfigError, body)
			}
			if got == nil || !*got {
				t.Errorf("declared_elsewhere = %v, want true; body=%s", fmtBoolPtr(got), body)
			}
		})
	}
}

// A caller who may read only deSubject gets the bit — and nothing about the
// other file: not its name, its directory, nor the neighbour it declares.
func TestGetTenant_DeclaredElsewhereReaderSeesOnlyTheBit(t *testing.T) {
	t.Parallel()
	dir := deTree(t, deOwnBody(), true)
	mgr := newRBACManager(t, `groups:
  - name: subject-readers
    tenants: ["`+deSubject+`"]
    permissions: [read]
`)
	d := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), RBAC: mgr}
	as := func(r *http.Request) *http.Request {
		r.Header.Set("X-Forwarded-Email", "reader@example.com")
		r.Header.Set("X-Forwarded-Groups", "subject-readers")
		return r
	}
	h := wrapWithRBACMiddleware(GetTenant(d), mgr, rbac.PermRead, TenantIDFromPath)

	got, body := getDeclaredElsewhere(t, h, as(getSubjectReq()))
	if got == nil || !*got {
		t.Fatalf("declared_elsewhere = %v, want true; body=%s", fmtBoolPtr(got), body)
	}
	assertNoDeOtherLeak(t, body, dir)

	// The bit is no news to this caller: /effective and /diff, at the same
	// read permission, already answer 409 for it (hardening doc §3.1).
	ew := httptest.NewRecorder()
	wrapWithRBACMiddleware(GetTenantEffective(d), mgr, rbac.PermRead, TenantIDFromPath).ServeHTTP(ew,
		as(newRequestWithChiParam("GET", "/api/v1/tenants/"+deSubject+"/effective", "id", deSubject, nil)))
	if ew.Code != http.StatusConflict {
		t.Errorf("/effective as the reader = %d %s, want 409", ew.Code, ew.Body.String())
	}
	dw := httptest.NewRecorder()
	wrapWithRBACMiddleware(DiffTenant(d), mgr, rbac.PermRead, TenantIDFromPath).ServeHTTP(dw,
		as(newRequestWithChiParam("POST", "/api/v1/tenants/"+deSubject+"/diff", "id", deSubject,
			bytes.NewBufferString(deOwnBody()))))
	if dw.Code != http.StatusConflict || !strings.Contains(dw.Body.String(), string(errcode.TenantDeclaredElsewhere)) {
		t.Errorf("/diff as the reader = %d %s, want 409 %s", dw.Code, dw.Body.String(), errcode.TenantDeclaredElsewhere)
	}

	// Control: the caller really cannot read the neighbour.
	w := httptest.NewRecorder()
	h.ServeHTTP(w, as(newRequestWithChiParam("GET", "/api/v1/tenants/"+deNeighbour, "id", deNeighbour, nil)))
	if w.Code != http.StatusForbidden {
		t.Errorf("GET %s as the subject-only reader = %d, want 403 (the fixture must not grant it)", deNeighbour, w.Code)
	}
}

// Unjudged is absent, never false: without a Writer, and when the conf.d
// walk cannot run (the Writer's conf.d does not exist).
func TestGetTenant_DeclaredElsewhereAbsentWhenNotJudged(t *testing.T) {
	t.Parallel()
	dir := deTree(t, deOwnBody(), true)
	for name, d := range map[string]*Deps{
		"no writer":   {ConfigDir: dir},
		"walk failed": {ConfigDir: dir, Writer: newTestWriter(filepath.Join(dir, "conf.d-missing"))},
	} {
		got, body := getDeclaredElsewhere(t, GetTenant(d), getSubjectReq())
		if got != nil {
			t.Errorf("%s: declared_elsewhere = %v, want the key absent; body=%s", name, *got, body)
		}
	}
}

func assertNoDeOtherLeak(t *testing.T, body, dir string) {
	t.Helper()
	for _, leak := range []string{deOtherDir, "sibling-owner", deNeighbour, dir} {
		if strings.Contains(body, leak) {
			t.Errorf("GET body names the other file or its tenants (%q):\n%s", leak, body)
		}
	}
}

func fmtBoolPtr(p *bool) string {
	if p == nil {
		return "<absent>"
	}
	if *p {
		return "true"
	}
	return "false"
}
