package federation

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/tenant-api/internal/federation/logvis"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/statestore"
)

const (
	logVisTenant = "tenant-alpha"
	logVisPath   = "/api/v1/tenants/" + logVisTenant + "/federation/log-visibility"
)

// logVisAdminRBAC grants admin on logVisTenant only.
const logVisAdminRBAC = `groups:
  - name: platform-admins
    tenants: ["tenant-alpha"]
    permissions: [admin]
`

func logVisDeps(t *testing.T, rbacYAML string) (*handler.Deps, string) {
	t.Helper()
	dir := t.TempDir()
	return &handler.Deps{ConfigDir: dir, State: newDirState(t, dir), RBAC: newRBACManager(t, rbacYAML)}, dir
}

func decodeLogVis(t *testing.T, body []byte) LogVisibilityResponse {
	t.Helper()
	var got LogVisibilityResponse
	if err := json.Unmarshal(body, &got); err != nil {
		t.Fatalf("unmarshal %s: %v", body, err)
	}
	return got
}

func TestGetTenantLogVisibility_NoRecordIsPlatformDefault(t *testing.T) {
	t.Parallel()
	d, _ := logVisDeps(t, "")
	w := executeWithRBAC(t, GetTenantLogVisibility(d), fedReq(t, "GET", logVisPath, "id", logVisTenant, ""))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), `"fields":null`) {
		t.Errorf("default must serialise as fields:null, body: %s", w.Body.String())
	}
	got := decodeLogVis(t, w.Body.Bytes())
	want := logvis.VisibleFields(&logvis.Selection{})
	if !reflect.DeepEqual(got.VisibleFields, want) {
		t.Errorf("visible_fields = %v, want every curated field %v", got.VisibleFields, want)
	}
}

func TestPutTenantLogVisibility_ForbiddenWithoutTenantAdmin(t *testing.T) {
	t.Parallel()
	d, dir := logVisDeps(t, logVisAdminRBAC)
	other := "/api/v1/tenants/tenant-beta/federation/log-visibility"
	w := executeWithRBAC(t, PutTenantLogVisibility(d), fedReq(t, "PUT", other, "id", "tenant-beta", `{"fields":["status"]}`))
	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403, body: %s", w.Code, w.Body.String())
	}
	if _, err := os.Stat(filepath.Join(dir, statestore.LogVisibilityKey("tenant-beta"))); !os.IsNotExist(err) {
		t.Error("a refused PUT must not write a record")
	}
}

// TestPutTenantLogVisibility_CannotWidenPastPlatformSet: a selection naming a
// field outside the platform-curated set is refused and nothing is written.
func TestPutTenantLogVisibility_CannotWidenPastPlatformSet(t *testing.T) {
	t.Parallel()
	for _, body := range []string{
		`{"fields":["status","upstream"]}`,
		`{"fields":["pod_node"]}`,
		`{"fields":["k8s_namespace"]}`,
	} {
		d, dir := logVisDeps(t, logVisAdminRBAC)
		w := executeWithRBAC(t, PutTenantLogVisibility(d), fedReq(t, "PUT", logVisPath, "id", logVisTenant, body))
		if w.Code != http.StatusBadRequest {
			t.Fatalf("%s: status = %d, want 400, body: %s", body, w.Code, w.Body.String())
		}
		if !strings.Contains(w.Body.String(), "not a field the platform makes visible") {
			t.Errorf("%s: body should name the containment rule: %s", body, w.Body.String())
		}
		if _, err := os.Stat(filepath.Join(dir, statestore.LogVisibilityKey(logVisTenant))); !os.IsNotExist(err) {
			t.Errorf("%s: a refused selection must not be written", body)
		}
	}
}

func TestPutTenantLogVisibility_RejectsUnknownKey(t *testing.T) {
	t.Parallel()
	d, _ := logVisDeps(t, logVisAdminRBAC)
	// "field" (singular) would decode as fields:null — a silent reset to the
	// default — if unknown keys were ignored.
	w := executeWithRBAC(t, PutTenantLogVisibility(d), fedReq(t, "PUT", logVisPath, "id", logVisTenant, `{"field":["status"]}`))
	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want 400, body: %s", w.Code, w.Body.String())
	}
}

func TestPutTenantLogVisibility_StoresAndResets(t *testing.T) {
	t.Parallel()
	d, _ := logVisDeps(t, logVisAdminRBAC)

	w := executeWithRBAC(t, PutTenantLogVisibility(d), fedReq(t, "PUT", logVisPath, "id", logVisTenant, `{"fields":["path","status"]}`))
	if w.Code != http.StatusOK {
		t.Fatalf("PUT status = %d, want 200, body: %s", w.Code, w.Body.String())
	}
	put := decodeLogVis(t, w.Body.Bytes())
	wantVisible := append(append([]string{}, logvis.AlwaysVisibleFields...), "status", "path")
	if !reflect.DeepEqual(put.VisibleFields, wantVisible) {
		t.Errorf("PUT visible_fields = %v, want %v", put.VisibleFields, wantVisible)
	}

	w = executeWithRBAC(t, GetTenantLogVisibility(d), fedReq(t, "GET", logVisPath, "id", logVisTenant, ""))
	got := decodeLogVis(t, w.Body.Bytes())
	if got.Fields == nil || !reflect.DeepEqual(*got.Fields, []string{"path", "status"}) {
		t.Fatalf("GET fields = %v, want [path status]", got.Fields)
	}

	// null returns to the platform default.
	w = executeWithRBAC(t, PutTenantLogVisibility(d), fedReq(t, "PUT", logVisPath, "id", logVisTenant, `{"fields":null}`))
	if w.Code != http.StatusOK {
		t.Fatalf("reset status = %d, body: %s", w.Code, w.Body.String())
	}
	w = executeWithRBAC(t, GetTenantLogVisibility(d), fedReq(t, "GET", logVisPath, "id", logVisTenant, ""))
	if got := decodeLogVis(t, w.Body.Bytes()); got.Fields != nil {
		t.Errorf("after reset fields = %v, want null (default)", *got.Fields)
	}
}

func TestPutTenantLogVisibility_EmptyKeepsOnlyAlwaysVisible(t *testing.T) {
	t.Parallel()
	d, _ := logVisDeps(t, logVisAdminRBAC)
	w := executeWithRBAC(t, PutTenantLogVisibility(d), fedReq(t, "PUT", logVisPath, "id", logVisTenant, `{"fields":[]}`))
	if w.Code != http.StatusOK {
		t.Fatalf("status = %d, body: %s", w.Code, w.Body.String())
	}
	if got := decodeLogVis(t, w.Body.Bytes()); !reflect.DeepEqual(got.VisibleFields, logvis.AlwaysVisibleFields) {
		t.Errorf("visible_fields = %v, want only %v", got.VisibleFields, logvis.AlwaysVisibleFields)
	}
}
