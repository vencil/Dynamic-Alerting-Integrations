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

// TestPutTenant_PRAttribution pins the single-tenant PR's title, labels and
// body Source line. tenant-api has no machine caller (ADR-027 §4): the write
// source header the removed threshold-govern write path used to send selects
// nothing any more, so a PUT carrying it is attributed exactly like one
// without it.
func TestPutTenant_PRAttribution(t *testing.T) {
	t.Parallel()
	for name, header := range map[string]string{
		"no header":                   "",
		"removed write-source header": "threshold-governance",
	} {
		t.Run(name, func(t *testing.T) {
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
			if header != "" {
				req.Header.Set("X-DA-Write-Source", header)
			}
			w := httptest.NewRecorder()
			wrapWithRBACMiddleware(h, rbacMgr, rbac.PermWrite, TenantIDFromPath).ServeHTTP(w, req)

			if w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
			}
			if gotTitle != "[tenant-api] Update db-a configuration" {
				t.Errorf("PR title = %q, want the UI title", gotTitle)
			}
			if !strings.Contains(gotBody, "**Source:** tenant-manager UI") || !strings.Contains(gotBody, "alice@example.com") {
				t.Errorf("PR body = %q, want the UI source line and the operator", gotBody)
			}
			if len(gotLabels) != 2 || gotLabels[0] != "tenant-api" || gotLabels[1] != "auto-generated" {
				t.Errorf("PR labels = %v, want exactly [tenant-api auto-generated]", gotLabels)
			}
		})
	}
}
