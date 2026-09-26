package handler

// #2078 handler coverage: a write for an id another conf.d file already
// declares is refused by the writer (gitops.ErrTenantDeclaredElsewhere); every
// tenant write surface must render it as 409 TENANT_DECLARED_ELSEWHERE (the
// direct-mode batch, which has no per-op HTTP status, as results[].code), and
// ⛔ none may echo the other file's path or name — only the server log sees it.

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

	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
)

const (
	elsewhereTenant  = "sub-t"
	elsewhereDirName = "team-hidden"
	elsewhereFile    = elsewhereDirName + "/" + elsewhereTenant + "-owner.yaml"
)

func elsewhereBody() string {
	return "tenants:\n  " + elsewhereTenant + ":\n    _silent_mode: \"warning\"\n"
}

// seedElsewhereTree writes the subdirectory declaration and commits it on
// "main" (PR mode branches from there).
func seedElsewhereTree(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	p := filepath.Join(dir, filepath.FromSlash(elsewhereFile))
	if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(p, []byte(elsewhereBody()), 0o644); err != nil {
		t.Fatal(err)
	}
	for _, args := range [][]string{
		{"init"}, {"config", "user.email", "t@t.com"}, {"config", "user.name", "T"},
		{"add", "."}, {"commit", "-m", "seed"}, {"branch", "-M", "main"},
	} {
		if out, err := exec.Command("git", append([]string{"-C", dir}, args...)...).CombinedOutput(); err != nil {
			t.Skipf("git %v: %v\n%s", args, err, out)
		}
	}
	return dir
}

// assertNoOtherFileLeak fails if the response names the declaring file.
func assertNoOtherFileLeak(t *testing.T, body string) {
	t.Helper()
	for _, leak := range []string{elsewhereDirName, elsewhereTenant + "-owner"} {
		if strings.Contains(body, leak) {
			t.Errorf("response leaks the other file (%q):\n%s", leak, body)
		}
	}
}

func assertDeclaredElsewhere409(t *testing.T, w *httptest.ResponseRecorder) {
	t.Helper()
	if w.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409; body=%s", w.Code, w.Body.String())
	}
	var env struct {
		Code string `json:"code"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("unmarshal: %v; body=%s", err, w.Body.String())
	}
	if env.Code != CodeTenantDeclaredElsewhere {
		t.Errorf("code = %q, want %q", env.Code, CodeTenantDeclaredElsewhere)
	}
	assertNoOtherFileLeak(t, w.Body.String())
}

func assertNoTopLevelFile(t *testing.T, dir string) {
	t.Helper()
	if _, err := os.Stat(filepath.Join(dir, elsewhereTenant+".yaml")); !os.IsNotExist(err) {
		t.Errorf("%s.yaml was created (stat err=%v)", elsewhereTenant, err)
	}
}

func TestPutTenant_DeclaredElsewhereIsConflict(t *testing.T) {
	t.Run("direct", func(t *testing.T) {
		dir := seedElsewhereTree(t)
		d := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), WriteMode: WriteModeDirect}
		w := httptest.NewRecorder()
		PutTenant(d)(w, newRequestWithChiParam("PUT", "/api/v1/tenants/"+elsewhereTenant, "id", elsewhereTenant,
			bytes.NewBufferString(elsewhereBody())))
		assertDeclaredElsewhere409(t, w)
		assertNoTopLevelFile(t, dir)
	})
	t.Run("pr", func(t *testing.T) {
		dir := seedElsewhereTree(t)
		gh := &mockPlatformClient{
			providerName: "github",
			createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
				t.Error("CreatePR reached for a refused write")
				return &platform.PRInfo{Number: 1}, nil
			},
		}
		d := &Deps{ConfigDir: dir, Writer: newTestWriter(dir), WriteMode: WriteModePR,
			PRClient: gh, PRTracker: &mockPlatformTracker{}}
		w := httptest.NewRecorder()
		PutTenant(d)(w, newRequestWithChiParam("PUT", "/api/v1/tenants/"+elsewhereTenant, "id", elsewhereTenant,
			bytes.NewBufferString(elsewhereBody())))
		assertDeclaredElsewhere409(t, w)
		assertNoTopLevelFile(t, dir)
	})
}

func TestBatchTenants_DeclaredElsewhere(t *testing.T) {
	const reqBody = `{"operations":[{"tenant_id":"` + elsewhereTenant + `","patch":{"_silent_mode":"critical"}}]}`
	serve := func(t *testing.T, d *Deps, rbacMgr *rbac.Manager) *httptest.ResponseRecorder {
		t.Helper()
		req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(reqBody))
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("X-Forwarded-Email", "alice@example.com")
		req.Header.Set("X-Forwarded-Groups", "admins")
		w := httptest.NewRecorder()
		rbacMgr.Middleware(rbac.PermRead, nil)(BatchTenants(d)).ServeHTTP(w, req)
		return w
	}

	// Direct mode answers per op (the batch as a whole is 200), so the stable
	// code rides on results[].code.
	t.Run("direct", func(t *testing.T) {
		dir := seedElsewhereTree(t)
		rbacMgr := adminRBAC(t)
		w := serve(t, &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: rbacMgr, WriteMode: WriteModeDirect}, rbacMgr)
		if w.Code != http.StatusOK {
			t.Fatalf("status = %d, want 200; body=%s", w.Code, w.Body.String())
		}
		var resp BatchResponse
		if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
			t.Fatalf("unmarshal: %v", err)
		}
		if len(resp.Results) != 1 || resp.Results[0].Status != "error" ||
			resp.Results[0].Code != CodeTenantDeclaredElsewhere {
			t.Fatalf("results = %+v, want one error with code %s", resp.Results, CodeTenantDeclaredElsewhere)
		}
		assertNoOtherFileLeak(t, w.Body.String())
		assertNoTopLevelFile(t, dir)
	})

	t.Run("pr", func(t *testing.T) {
		dir := seedElsewhereTree(t)
		rbacMgr := adminRBAC(t)
		gh := &mockPlatformClient{
			providerName: "github",
			createPRFunc: func(title, body, head string, labels []string) (*platform.PRInfo, error) {
				t.Error("CreatePR reached for a refused batch")
				return &platform.PRInfo{Number: 1}, nil
			},
		}
		w := serve(t, &Deps{Writer: newTestWriter(dir), ConfigDir: dir, RBAC: rbacMgr,
			WriteMode: WriteModePR, PRClient: gh, PRTracker: &mockPlatformTracker{}}, rbacMgr)
		assertDeclaredElsewhere409(t, w)
		assertNoTopLevelFile(t, dir)
	})
}
