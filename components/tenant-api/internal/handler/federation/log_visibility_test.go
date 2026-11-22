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
	if !reflect.DeepEqual(got.SelectedVisibleFields, want) {
		t.Errorf("selected_visible_fields = %v, want every curated field %v", got.SelectedVisibleFields, want)
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

// TestPutTenantLogVisibility_MalformedBodyNeverResetsToDefault: every body
// below would decode as fields:null — a silent widening back to every curated
// field — under Go's default json decoding. Each must be a 400 that writes
// nothing, leaving the tenant's earlier, narrower selection in place.
func TestPutTenantLogVisibility_MalformedBodyNeverResetsToDefault(t *testing.T) {
	t.Parallel()
	for name, body := range map[string]string{
		"misspelt key":     `{"field":["status"]}`,
		"other casing":     `{"Fields":["status"]}`,
		"no fields key":    `{}`,
		"trailing object":  `{"fields":["status"]}{"fields":null}`,
		"trailing garbage": `{"fields":["status"]} x`,
		"json null":        `null`,
		"not a list":       `{"fields":"status"}`,
		"extra key":        `{"fields":["status"],"reset":true}`,
		"array body":       `["status"]`,
		"empty body":       ``,
		"both casings":     `{"fields":["path"],"Fields":null}`,
	} {
		d, dir := logVisDeps(t, logVisAdminRBAC)
		key := filepath.Join(dir, statestore.LogVisibilityKey(logVisTenant))
		if err := os.WriteFile(key, []byte("fields:\n    - path\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		w := executeWithRBAC(t, PutTenantLogVisibility(d), fedReq(t, "PUT", logVisPath, "id", logVisTenant, body))
		if w.Code != http.StatusBadRequest {
			t.Errorf("%s (%s): status = %d, want 400, body: %s", name, body, w.Code, w.Body.String())
			continue
		}
		if got, _ := os.ReadFile(key); string(got) != "fields:\n    - path\n" {
			t.Errorf("%s: the stored selection changed to %q", name, got)
		}
	}
	// A body without the key is told how to reset, not just that it failed.
	d, _ := logVisDeps(t, logVisAdminRBAC)
	w := executeWithRBAC(t, PutTenantLogVisibility(d), fedReq(t, "PUT", logVisPath, "id", logVisTenant, `{}`))
	if !strings.Contains(w.Body.String(), `\"fields\" is required`) {
		t.Errorf(`{} should be refused as a missing "fields" key, body: %s`, w.Body.String())
	}
}

// TestPutTenantLogVisibility_OverCapBodyIs413: a body padded with whitespace
// to the cap and followed by more JSON must not be parsed from its truncated
// prefix (which would pass the trailing-data check and write a 200).
func TestPutTenantLogVisibility_OverCapBodyIs413(t *testing.T) {
	t.Parallel()
	d, dir := logVisDeps(t, logVisAdminRBAC)
	d.MaxBodyBytes = 64
	head := `{"fields":["status"]}`
	body := head + strings.Repeat(" ", 64-len(head)) + `{"fields":null}`
	w := executeWithRBAC(t, PutTenantLogVisibility(d), fedReq(t, "PUT", logVisPath, "id", logVisTenant, body))
	if w.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("status = %d, want 413, body: %s", w.Code, w.Body.String())
	}
	if _, err := os.Stat(filepath.Join(dir, statestore.LogVisibilityKey(logVisTenant))); !os.IsNotExist(err) {
		t.Error("an over-cap PUT must not write a record")
	}
}

// TestGetTenantLogVisibility_DamagedRecordIs500: a stored record that does not
// parse strictly is an error, never read as the default (every field).
func TestGetTenantLogVisibility_DamagedRecordIs500(t *testing.T) {
	t.Parallel()
	d, dir := logVisDeps(t, "")
	if err := os.WriteFile(filepath.Join(dir, statestore.LogVisibilityKey(logVisTenant)), []byte("bogus: 1\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	w := executeWithRBAC(t, GetTenantLogVisibility(d), fedReq(t, "GET", logVisPath, "id", logVisTenant, ""))
	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500, body: %s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "selected_visible_fields") {
		t.Errorf("a damaged record must not answer with a selection: %s", w.Body.String())
	}
}

// logVisReadWriteRBAC grants read and write, but not admin, on logVisTenant.
const logVisReadWriteRBAC = `groups:
  - name: platform-admins
    tenants: ["tenant-alpha"]
    permissions: [read, write]
`

// TestPutTenantLogVisibility_ReadWriteIsNotAdmin pins the permission LEVEL:
// write on the tenant is not enough, the bar is admin (as for the metric
// subset and token issuance).
func TestPutTenantLogVisibility_ReadWriteIsNotAdmin(t *testing.T) {
	t.Parallel()
	d, dir := logVisDeps(t, logVisReadWriteRBAC)
	w := executeWithRBAC(t, PutTenantLogVisibility(d), fedReq(t, "PUT", logVisPath, "id", logVisTenant, `{"fields":["status"]}`))
	if w.Code != http.StatusForbidden {
		t.Fatalf("status = %d, want 403, body: %s", w.Code, w.Body.String())
	}
	if !strings.Contains(w.Body.String(), "log visibility") {
		t.Errorf("403 should come from the handler's admin gate: %s", w.Body.String())
	}
	if _, err := os.Stat(filepath.Join(dir, statestore.LogVisibilityKey(logVisTenant))); !os.IsNotExist(err) {
		t.Error("a refused PUT must not write a record")
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
	if !reflect.DeepEqual(put.SelectedVisibleFields, wantVisible) {
		t.Errorf("PUT selected_visible_fields = %v, want %v", put.SelectedVisibleFields, wantVisible)
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
	if got := decodeLogVis(t, w.Body.Bytes()); !reflect.DeepEqual(got.SelectedVisibleFields, logvis.AlwaysVisibleFields) {
		t.Errorf("selected_visible_fields = %v, want only %v", got.SelectedVisibleFields, logvis.AlwaysVisibleFields)
	}
}
