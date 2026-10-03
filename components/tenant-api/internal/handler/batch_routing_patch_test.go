package handler

// #2341: a flat batch patch can only write a string `_routing`, and the only
// string the route generator reads is a disabling one. Any other value —
// `on`, `slack`, an empty or blank string, and JSON null (which decodes to
// "") — is refused with 400 INVALID_BODY by validatePatchMap, before any
// write, on /tenants/batch and /groups/{id}/batch, in both write modes.
// A JSON null is refused like "": removing `_routing` is the op's `unset`
// (B2, batch_unset_test.go), not RFC 7396 null — the spec is Swagger 2.0,
// which cannot express a nullable value, so a generated client could not
// send one.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os/exec"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
)

// postTenantBatch is runBatch without the 200 assertion.
func postTenantBatch(t *testing.T, d *Deps, ops string) *httptest.ResponseRecorder {
	t.Helper()
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(`{"operations":`+ops+`}`))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	d.RBAC.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)
	return w
}

// assertRoutingPatchRefused checks a 400 INVALID_BODY whose only violation is
// on field, naming the way back to routing.
func assertRoutingPatchRefused(t *testing.T, w *httptest.ResponseRecorder, field string) {
	t.Helper()
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400; body: %s", w.Code, w.Body.String())
	}
	var bad ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &bad); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if bad.Code != CodeInvalidBody || len(bad.Violations) != 1 || bad.Violations[0].Field != field ||
		!strings.Contains(bad.Violations[0].Reason, `PUT the tenant with a _routing mapping, or remove _routing with unset: ["_routing"]`) {
		t.Errorf("response = %+v, want one %s violation on %s naming PUT / unset", bad, CodeInvalidBody, field)
	}
}

// refusedRoutingValues are JSON values of a `_routing` patch; null decodes to "".
var refusedRoutingValues = []string{`"on"`, `"slack"`, `""`, `"   "`, `null`, `"dİsable"`}

func gitHead(t *testing.T, dir string) string {
	t.Helper()
	out, err := exec.Command("git", "-C", dir, "rev-parse", "HEAD").CombinedOutput()
	if err != nil {
		t.Fatalf("git rev-parse: %v\n%s", err, out)
	}
	return strings.TrimSpace(string(out))
}

func TestBatchTenants_RoutingPatchMustDisable(t *testing.T) {
	for _, mode := range []WriteMode{WriteModePR, WriteModeDirect} {
		for _, v := range refusedRoutingValues {
			t.Run(string(mode)+"/"+v, func(t *testing.T) {
				configDir := seedGitTree(t, offTree(offDisabledChat))
				prCalls := 0
				d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
					Policy: policy.NewManager(configDir), WriteMode: mode}
				if mode == WriteModePR {
					d.PRClient = &mockPlatformClient{providerName: "github",
						createPRFunc: func(title, body, h string, labels []string) (*platform.PRInfo, error) {
							prCalls++
							return &platform.PRInfo{Number: 9, WebURL: "https://example/pr/9", State: "open"}, nil
						}}
					d.PRTracker = &mockPlatformTracker{}
				}
				before := gitHead(t, configDir)
				w := postTenantBatch(t, d, `[{"tenant_id":"t-off","patch":{"_routing":`+v+`}}]`)
				assertRoutingPatchRefused(t, w, `operations[0].patch["_routing"]`)
				if prCalls != 0 || gitHead(t, configDir) != before {
					t.Errorf("refused batch wrote something: PR calls %d, HEAD moved %v", prCalls, gitHead(t, configDir) != before)
				}
			})
		}
		t.Run(string(mode)+"/disable is accepted", func(t *testing.T) {
			configDir := seedGitTree(t, offTree("    _routing_profile: domain-ok\n"))
			d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: adminRBAC(t),
				Policy: policy.NewManager(configDir), WriteMode: mode}
			if mode == WriteModePR {
				d.PRClient = &mockPlatformClient{providerName: "github"}
				d.PRTracker = &mockPlatformTracker{}
			}
			if w := postTenantBatch(t, d, `[{"tenant_id":"t-off","patch":{"_routing":" Disabled "}}]`); w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
			}
		})
	}
}

func TestGroupBatch_RoutingPatchMustDisable(t *testing.T) {
	for _, mode := range []WriteMode{WriteModePR, WriteModeDirect} {
		for _, v := range refusedRoutingValues {
			t.Run(string(mode)+"/"+v, func(t *testing.T) {
				f := newGroupBatchFixture(t, mode)
				before := f.mainSHA(t)
				assertRoutingPatchRefused(t, f.post(t, `{"patch":{"_routing":`+v+`}}`), `patch["_routing"]`)
				if f.prCalls != 0 || f.mainSHA(t) != before {
					t.Errorf("refused group batch wrote something: PR calls %d", f.prCalls)
				}
			})
		}
		t.Run(string(mode)+"/disable is accepted", func(t *testing.T) {
			f := newGroupBatchFixture(t, mode)
			if w := f.post(t, `{"patch":{"_routing":"off"}}`); w.Code != http.StatusOK {
				t.Fatalf("status = %d, want 200; body: %s", w.Code, w.Body.String())
			}
		})
	}
}
