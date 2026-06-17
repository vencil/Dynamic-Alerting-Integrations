package handler

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

// TestResolveWriteSource_Default: no header (or an unknown value) → the
// tenant-manager UI attribution, byte-for-byte the historical strings.
func TestResolveWriteSource_Default(t *testing.T) {
	t.Parallel()
	for _, hv := range []string{"", "bogus", "Threshold-Governance" /* case-sensitive */} {
		req := httptest.NewRequest("PUT", "/api/v1/tenants/db-a", nil)
		if hv != "" {
			req.Header.Set(WriteSourceHeader, hv)
		}
		ws := resolveWriteSource(req)
		if ws.sourceLine != "tenant-manager UI" {
			t.Errorf("header %q: sourceLine = %q, want UI default", hv, ws.sourceLine)
		}
		if got := ws.titleSingle("db-a"); got != "[tenant-api] Update db-a configuration" {
			t.Errorf("header %q: title = %q, want UI default", hv, got)
		}
		got := ws.labels()
		if len(got) != 2 || !hasLabel(got, "tenant-api") || !hasLabel(got, "auto-generated") {
			t.Errorf("header %q: labels = %v, want exactly [tenant-api auto-generated]", hv, got)
		}
	}
}

// TestResolveWriteSource_Governance: the allowlisted header value selects the
// governance attribution — distinct title, an extra label, and an honest
// Source line that does NOT claim the UI.
func TestResolveWriteSource_Governance(t *testing.T) {
	t.Parallel()
	req := httptest.NewRequest("PUT", "/api/v1/tenants/db-a", nil)
	req.Header.Set(WriteSourceHeader, WriteSourceThresholdGovernance)
	ws := resolveWriteSource(req)

	if strings.Contains(ws.sourceLine, "tenant-manager UI") {
		t.Errorf("governance sourceLine must not claim the UI: %q", ws.sourceLine)
	}
	if got := ws.titleSingle("db-a"); !strings.Contains(got, "threshold-governance") {
		t.Errorf("governance title = %q, want a threshold-governance marker", got)
	}
	labels := ws.labels()
	if !hasLabel(labels, "threshold-governance") {
		t.Errorf("governance labels = %v, want a threshold-governance label", labels)
	}
	if !hasLabel(labels, "tenant-api") || !hasLabel(labels, "auto-generated") {
		t.Errorf("governance labels = %v, want the base labels preserved", labels)
	}
}

// TestWriteSourceLabels_NoAliasing: labels() must hand back an independent slice
// each call so a caller appending to it cannot corrupt the package-level
// attribution (createPRAndRegister passes the result straight into CreatePR).
func TestWriteSourceLabels_NoAliasing(t *testing.T) {
	t.Parallel()
	ws := knownWriteSources[WriteSourceThresholdGovernance]
	a := ws.labels()
	a = append(a, "MUTATED")
	a[0] = "MUTATED"
	b := ws.labels()
	if b[0] == "MUTATED" || hasLabel(b, "MUTATED") {
		t.Errorf("labels() aliased shared state: second call = %v", b)
	}
	if len(ws.extraLabels) != 1 || ws.extraLabels[0] != "threshold-governance" {
		t.Errorf("extraLabels corrupted by caller append: %v", ws.extraLabels)
	}
}

// TestPutTenant_Governance_PRAttribution is the end-to-end guard: a PUT carrying
// the governance header produces a PR whose title, labels, and body Source line
// are the governance channel — proving the honest attribution reaches CreatePR.
// Uses a real git dir (so WritePR succeeds) + a capturing mock client, wrapped
// in the RBAC middleware so the operator identity lands in the request context
// (the recipe TestPutTenant_PRMode_ForgeForbidden proves reaches CreatePR).
func TestPutTenant_Governance_PRAttribution(t *testing.T) {
	t.Parallel()
	configDir := initGitConfigDir(t)
	writer := newTestWriter(configDir)
	rbacMgr := adminRBAC(t)

	var gotTitle, gotBody string
	var gotLabels []string
	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
			gotTitle, gotBody, gotLabels = title, body, labels
			return &platform.PRInfo{Number: 7, WebURL: "https://gh/7", State: "open", HeadRef: headBranch}, nil
		},
	}
	mockTracker := &mockPlatformTracker{}

	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: mockTracker, RBAC: rbacMgr})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	req.Header.Set("X-Forwarded-Email", "governance-bot@platform.local")
	req.Header.Set("X-Forwarded-Groups", "admins")
	req.Header.Set(WriteSourceHeader, WriteSourceThresholdGovernance)
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if !strings.Contains(gotTitle, "threshold-governance") {
		t.Errorf("PR title = %q, want a threshold-governance marker", gotTitle)
	}
	if !hasLabel(gotLabels, "threshold-governance") {
		t.Errorf("PR labels = %v, want a threshold-governance label", gotLabels)
	}
	if strings.Contains(gotBody, "tenant-manager UI") {
		t.Errorf("PR body falsely claims the UI as source: %q", gotBody)
	}
	if !strings.Contains(gotBody, "governance-bot@platform.local") {
		t.Errorf("PR body = %q, want the operator identity preserved", gotBody)
	}
}

// TestPutTenant_DefaultSource_PRAttribution is the regression guard for the
// historical UI path: a PUT WITHOUT the header keeps the exact pre-#656 title /
// labels / body, so existing tenant-manager edits are unchanged.
func TestPutTenant_DefaultSource_PRAttribution(t *testing.T) {
	t.Parallel()
	configDir := initGitConfigDir(t)
	writer := newTestWriter(configDir)
	rbacMgr := adminRBAC(t)

	var gotTitle, gotBody string
	var gotLabels []string
	mockClient := &mockPlatformClient{
		providerName: "github",
		createPRFunc: func(title, body, headBranch string, labels []string) (*platform.PRInfo, error) {
			gotTitle, gotBody, gotLabels = title, body, labels
			return &platform.PRInfo{Number: 8, WebURL: "https://gh/8", State: "open", HeadRef: headBranch}, nil
		},
	}
	mockTracker := &mockPlatformTracker{}

	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR, PRClient: mockClient, PRTracker: mockTracker, RBAC: rbacMgr})
	body := bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n")
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a", body)
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
	}
	if gotTitle != "[tenant-api] Update db-a configuration" {
		t.Errorf("default PR title = %q, want the pre-#656 UI title", gotTitle)
	}
	if !strings.Contains(gotBody, "**Source:** tenant-manager UI") {
		t.Errorf("default PR body = %q, want the UI source line", gotBody)
	}
	if hasLabel(gotLabels, "threshold-governance") || len(gotLabels) != 2 {
		t.Errorf("default PR labels = %v, want only the 2 base labels", gotLabels)
	}
}

func hasLabel(ss []string, want string) bool {
	for _, s := range ss {
		if s == want {
			return true
		}
	}
	return false
}
