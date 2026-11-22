package federation

// #2778 follow-up: the three federation write handlers used to read their body
// with io.ReadAll(io.LimitReader(r.Body, d.MaxBody())), which stops at the cap
// WITHOUT an error. A JSON body padded with whitespace to the cap and going on
// past it then decoded from its truncated prefix and was ACTED ON — a token
// issued, a whitelist or subset written — behind a 2xx. They now read through
// handler.ReadLimitedBody: over the cap is a 413 PAYLOAD_TOO_LARGE, before any
// state is touched.
//
// Every case is a pair: the over-cap body is the at-cap body plus ONE trailing
// space, so the whole over-cap body is valid JSON too and only its size can
// decide the outcome; the at-cap control must succeed, or the 413 could come
// from anything that rejects the body.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
)

const bodyCapTenant = "tenant-alpha"

// padToCap returns head padded with trailing whitespace to exactly limit
// bytes (still one valid JSON value), and that body plus one more space.
func padToCap(t *testing.T, head string, limit int) (atCap, overCap string) {
	t.Helper()
	if len(head) > limit {
		t.Fatalf("head is %d bytes, over the %d-byte cap it must fit in", len(head), limit)
	}
	atCap = head + strings.Repeat(" ", limit-len(head))
	return atCap, atCap + " "
}

// assertPayloadTooLarge checks the 413 envelope: the code, and the knob that
// raises the cap, so the operator has something to act on.
func assertPayloadTooLarge(t *testing.T, w *httptest.ResponseRecorder) {
	t.Helper()
	if w.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413; body: %s", w.Code, w.Body.String())
	}
	var env handler.ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("413 body is not an error envelope: %v; body: %s", err, w.Body.String())
	}
	if env.Code != errcode.PayloadTooLarge {
		t.Errorf("code = %q, want %q", env.Code, errcode.PayloadTooLarge)
	}
	if !strings.Contains(env.Error, "TA_MAX_BODY_BYTES") {
		t.Errorf("the 413 does not name the knob that raises the cap: %s", env.Error)
	}
}

func TestCreateFederationToken_OverBodyCapIs413AndIssuesNothing(t *testing.T) {
	t.Parallel()
	const limit = 256
	atCap, overCap := padToCap(t, `{"tenant_id":"`+bodyCapTenant+`","description":"cap"}`, limit)

	post := func(d *handler.Deps, mgr *rbac.Manager, body string) *httptest.ResponseRecorder {
		req := httptest.NewRequest("POST", "/api/v1/federation/tokens", bytes.NewBufferString(body))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Forwarded-Email", "ops@example.com")
		req.Header.Set("X-Forwarded-Groups", "fed-admins")
		w := httptest.NewRecorder()
		wrapWithRBACMiddleware(CreateFederationToken(d), mgr, rbac.PermRead, nil).ServeHTTP(w, req)
		return w
	}

	mgr := newRBACManager(t, fedAdminRBAC)
	d := &handler.Deps{RBAC: mgr, Federation: newTestFederation(t), MaxBodyBytes: limit}
	assertPayloadTooLarge(t, post(d, mgr, overCap))
	recs, err := d.Federation.List(bodyCapTenant)
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(recs) != 0 {
		t.Errorf("a refused over-cap request issued %d token(s): %+v", len(recs), recs)
	}

	// Control: the same body one byte shorter, i.e. exactly at the cap.
	if w := post(d, mgr, atCap); w.Code != http.StatusCreated {
		t.Fatalf("control at exactly the cap: status = %d, want 201; body: %s", w.Code, w.Body.String())
	}
}

func TestPutFederationPolicy_OverBodyCapIs413AndWritesNothing(t *testing.T) {
	t.Parallel()
	const limit = 128
	atCap, overCap := padToCap(t, `{"whitelist":[{"metric":"mysql_up"}]}`, limit)

	newDeps := func() (*handler.Deps, string) {
		configDir := setupConfigDir(t, nil)
		initGitRepo(t, configDir)
		return &handler.Deps{
			ConfigDir:        configDir,
			State:            newDirState(t, configDir),
			Writer:           newTestWriter(configDir),
			FederationPolicy: fedpolicy.NewDirManager(configDir),
			RBAC:             newRBACManager(t, platformAdminRBAC),
			MaxBodyBytes:     limit,
		}, configDir
	}

	d, dir := newDeps()
	w := executeWithRBAC(t, PutFederationPolicy(d), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", overCap))
	assertPayloadTooLarge(t, w)
	if _, err := os.Stat(filepath.Join(dir, "_federation_policy.yaml")); !os.IsNotExist(err) {
		t.Errorf("a refused over-cap PUT wrote the whitelist (stat err = %v)", err)
	}
	if d.FederationPolicy.IsWhitelisted("mysql_up") {
		t.Error("a refused over-cap PUT changed the live whitelist")
	}

	d2, dir2 := newDeps()
	w2 := executeWithRBAC(t, PutFederationPolicy(d2), fedReq(t, "PUT", "/api/v1/federation/policy", "", "", atCap))
	if w2.Code != http.StatusOK {
		t.Fatalf("control at exactly the cap: status = %d, want 200; body: %s", w2.Code, w2.Body.String())
	}
	if _, err := os.Stat(filepath.Join(dir2, "_federation_policy.yaml")); err != nil {
		t.Errorf("control: whitelist not written: %v", err)
	}
}

func TestPutTenantFederation_OverBodyCapIs413AndWritesNothing(t *testing.T) {
	t.Parallel()
	const limit = 64
	atCap, overCap := padToCap(t, `{"metrics":["mysql_up"]}`, limit)
	path := "/api/v1/tenants/" + bodyCapTenant + "/federation"

	newDeps := func() (*handler.Deps, string) {
		configDir := setupConfigDir(t, nil)
		initGitRepo(t, configDir)
		return &handler.Deps{
			ConfigDir: configDir,
			State:     newDirState(t, configDir),
			Writer:    newTestWriter(configDir),
			FederationPolicy: fedpolicy.NewManagerForTest(&fedpolicy.Config{
				Whitelist: []fedpolicy.WhitelistEntry{{Metric: "mysql_up"}},
			}),
			RBAC:         newRBACManager(t, logVisAdminRBAC), // admin on bodyCapTenant
			MaxBodyBytes: limit,
		}, configDir
	}

	d, dir := newDeps()
	w := executeWithRBAC(t, PutTenantFederation(d), fedReq(t, "PUT", path, "id", bodyCapTenant, overCap))
	assertPayloadTooLarge(t, w)
	if _, err := os.Stat(federationSubsetPath(dir, bodyCapTenant)); !os.IsNotExist(err) {
		t.Errorf("a refused over-cap PUT wrote the subset (stat err = %v)", err)
	}

	d2, dir2 := newDeps()
	w2 := executeWithRBAC(t, PutTenantFederation(d2), fedReq(t, "PUT", path, "id", bodyCapTenant, atCap))
	if w2.Code != http.StatusOK {
		t.Fatalf("control at exactly the cap: status = %d, want 200; body: %s", w2.Code, w2.Body.String())
	}
	if _, err := os.Stat(federationSubsetPath(dir2, bodyCapTenant)); err != nil {
		t.Errorf("control: subset not written: %v", err)
	}
}
