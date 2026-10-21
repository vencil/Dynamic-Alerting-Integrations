package handler

// #3175: a git config naming a filter / diff / merge driver refuses every
// write and preview. The answer is the server's: a fixed text (500
// INTERNAL_ERROR for the write and /diff, valid: false for /validate), the
// scope and key only in the log. Which keys and scopes count is pinned in
// gitops.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func TestGitDriverConfig_WriteAndPreviewAnswers(t *testing.T) {
	t.Parallel()
	dir := seedGitTree(t, wmTree())
	if out, err := exec.Command("git", "-C", dir, "config", "filter.x.clean", "cat").CombinedOutput(); err != nil {
		t.Fatalf("git config: %v\n%s", err, out)
	}
	head := gitHead(t, dir)
	before, err := os.ReadFile(filepath.Join(dir, "t-a.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	d := &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: adminRBAC(t), WriteMode: WriteModeDirect, WriteResults: &WriteResults{}}
	const body = "tenants:\n  t-a:\n    mysql_connections: \"60\"\n"

	noKey := func(what string, w *httptest.ResponseRecorder) {
		t.Helper()
		if strings.Contains(w.Body.String(), "filter.x") {
			t.Errorf("%s body names the key: %s", what, w.Body.String())
		}
	}
	for name, w := range map[string]*httptest.ResponseRecorder{
		"PUT":   wmPut(d, body, ""),
		"/diff": wmServe(d, DiffTenant(d), newRequestWithChiParam("POST", "/api/v1/tenants/t-a/diff", "id", "t-a", bytes.NewBufferString(body))),
	} {
		if w.Code != http.StatusInternalServerError {
			t.Errorf("%s status = %d, want 500; body: %s", name, w.Code, w.Body.String())
			continue
		}
		var env ErrorResponse
		if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
			t.Fatal(err)
		}
		if env.Code != CodeInternal || env.Error != msgWriteFailed {
			t.Errorf("%s envelope = %+v, want %s with msgWriteFailed", name, env, CodeInternal)
		}
		noKey(name, w)
	}

	vw := wmServe(d, ValidateTenant(d), newRequestWithChiParam("POST", "/api/v1/tenants/t-a/validate", "id", "t-a", bytes.NewBufferString(body)))
	var vr ValidateResponse
	if err := json.Unmarshal(vw.Body.Bytes(), &vr); err != nil {
		t.Fatalf("/validate: %v; body: %s", err, vw.Body.String())
	}
	if vr.Valid || len(vr.Warnings) != 1 || vr.Warnings[0] != msgGitDriverConfigured {
		t.Errorf("/validate = %+v, want valid: false with msgGitDriverConfigured", vr)
	}
	noKey("/validate", vw)

	bw := wmBatch(d, "")
	if bw.Code != http.StatusInternalServerError || !strings.Contains(bw.Body.String(), CodeInternal) {
		t.Errorf("batch = %d %s, want 500 %s", bw.Code, bw.Body.String(), CodeInternal)
	}
	noKey("batch", bw)

	if gitHead(t, dir) != head {
		t.Error("HEAD moved")
	}
	if got, _ := os.ReadFile(filepath.Join(dir, "t-a.yaml")); string(got) != string(before) {
		t.Errorf("t-a.yaml changed: %q", got)
	}
}
