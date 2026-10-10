package handler

// #2778: a write body over TA_MAX_BODY_BYTES was silently truncated.
//
// readLimitedBody used to be io.ReadAll(io.LimitReader(r.Body, d.MaxBody())),
// which stops at the cap with no error. A YAML body whose first MaxBody bytes
// still parse was then written as if it were the whole request — the tail the
// client sent simply disappeared from conf.d. These tests pin the replacement
// behavior: over the cap is a 413, before anything reaches the writer.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const truncTenantID = "trunc-t"

// truncatableTenantBody returns a PUT body and the cap that cuts it exactly
// after its first, self-contained document line set: body[:limit] is valid
// tenant YAML on its own, body is one byte past the cap.
func truncatableTenantBody() (body string, limit int64) {
	head := "tenants:\n  " + truncTenantID + ":\n    _silent_mode: \"critical\"\n"
	// The tail is a comment so that the WHOLE body is also a valid write: the
	// only thing that may decide the outcome is the size, not the content.
	tail := "#"
	return head + tail, int64(len(head))
}

// TestPutTenant_OverBodyCapIs413NotTruncatedWrite is the end-to-end form of
// #2778 on the direct write path: a body one byte over the cap whose head is a
// complete tenant document must be refused with 413, and nothing written.
func TestPutTenant_OverBodyCapIs413NotTruncatedWrite(t *testing.T) {
	t.Parallel()
	configDir := initGitConfigDir(t)
	rbacMgr := adminRBAC(t)
	body, limit := truncatableTenantBody()
	d := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: rbacMgr,
		WriteMode: WriteModeDirect, MaxBodyBytes: limit}

	req := newRequestWithChiParam("PUT", "/api/v1/tenants/"+truncTenantID, "id", truncTenantID,
		bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(d), rbacMgr, "write", TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("PUT of a %d-byte body under a %d-byte cap: status = %d, want 413; body: %s",
			len(body), limit, w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "TA_MAX_BODY_BYTES") {
		t.Errorf("the 413 does not name the knob that raises the cap: %s", w.Body.String())
	}
	if _, err := os.Stat(filepath.Join(configDir, truncTenantID+".yaml")); !os.IsNotExist(err) {
		t.Errorf("a refused oversize PUT left a tenant file behind (stat err = %v)", err)
	}

	// Control: the same body under a cap it fits in is written. Without this
	// the 413 above could come from anything that rejects this body.
	d2 := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: rbacMgr,
		WriteMode: WriteModeDirect, MaxBodyBytes: int64(len(body))}
	req2 := newRequestWithChiParam("PUT", "/api/v1/tenants/"+truncTenantID, "id", truncTenantID,
		bytes.NewBufferString(body))
	req2.Header.Set("X-Forwarded-Email", "alice@example.com")
	req2.Header.Set("X-Forwarded-Groups", "admins")
	w2 := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutTenant(d2), rbacMgr, "write", TenantIDFromPath).ServeHTTP(w2, req2)
	if w2.Code != http.StatusOK {
		t.Fatalf("control: the same body at exactly the cap: status = %d, want 200; body: %s",
			w2.Code, w2.Body.String())
	}
}

// TestReadLimitedBody_Boundary pins the helper every non-batch write handler
// shares: exactly the cap is accepted whole, one byte past it is a 413 with
// the code and the knob, and nothing is returned for the caller to go on with.
func TestReadLimitedBody_Boundary(t *testing.T) {
	t.Parallel()
	const limit = 16
	for _, tc := range []struct {
		name    string
		size    int
		wantOK  bool
		wantSts int
	}{
		{"under the cap", limit - 1, true, http.StatusOK},
		{"exactly the cap", limit, true, http.StatusOK},
		{"one byte past the cap", limit + 1, false, http.StatusRequestEntityTooLarge},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			payload := strings.Repeat("a", tc.size)
			req := httptest.NewRequest("PUT", "/x", bytes.NewBufferString(payload))
			w := httptest.NewRecorder()
			body, ok := readLimitedBody(w, req, &Deps{MaxBodyBytes: limit})
			if ok != tc.wantOK {
				t.Fatalf("%d-byte body under a %d-byte cap: ok = %v, want %v; response: %s",
					tc.size, limit, ok, tc.wantOK, w.Body.String())
			}
			if ok {
				if string(body) != payload {
					t.Errorf("read %d bytes of a %d-byte body", len(body), tc.size)
				}
				return
			}
			if body != nil {
				t.Errorf("refused read still returned %d bytes", len(body))
			}
			if w.Code != tc.wantSts {
				t.Fatalf("status = %d, want %d", w.Code, tc.wantSts)
			}
			var resp map[string]any
			if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
				t.Fatalf("413 body is not JSON: %v: %s", err, w.Body.String())
			}
			if resp["code"] != CodePayloadTooLarge {
				t.Errorf("413 code = %v, want %q", resp["code"], CodePayloadTooLarge)
			}
			msg, _ := resp["error"].(string)
			if !strings.Contains(msg, "TA_MAX_BODY_BYTES") {
				t.Errorf("413 does not name TA_MAX_BODY_BYTES: %q", msg)
			}
			if strings.Contains(msg, "batch") {
				t.Errorf("a non-batch 413 carries the batch endpoints' explanation: %q", msg)
			}
		})
	}
}
