package handler

// X-DA-Base-Hash coverage for PUT /api/v1/tenants/{id}.
//
// The header is opt-in, so the two things worth pinning are the ones a caller
// cannot observe for themselves: that omitting it changes nothing, and that
// every way of asking for the gate and not getting it is an error rather than
// an unconditional overwrite.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	gh "github.com/vencil/tenant-api/internal/github"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

const (
	baseHashSeed   = "tenants:\n  db-a:\n    _silent_mode: \"warning\"\n"
	baseHashUpdate = "tenants:\n  db-a:\n    _silent_mode: \"critical\"\n"
)

// baseHashFixture seeds a tenant file in a real git repo and returns the
// handler, the file path, and the source_hash a client would have read.
// The handler is wrapped in the RBAC middleware because the writer needs a
// real author email: rbac.RequestEmail reads it from the request CONTEXT, which
// only the middleware populates, and git refuses to commit with an empty ident.
func baseHashFixture(t *testing.T) (http.Handler, string, string) {
	t.Helper()
	configDir := setupConfigDir(t, nil)
	initGitRepo(t, configDir)
	gw := gitops.NewWriter(configDir, configDir)
	h := wrapWithRBACMiddleware(
		PutTenant(&Deps{Writer: gw, WriteMode: WriteModeDirect}),
		newRBACManager(t, `groups:
  - name: admins
    tenants: ["*"]
    permissions: [read, write, admin]
`), rbac.PermWrite, TenantIDFromPath)

	path := filepath.Join(configDir, "db-a.yaml")
	if err := os.WriteFile(path, []byte(baseHashSeed), 0o644); err != nil {
		t.Fatalf("seed: %v", err)
	}
	runGit(t, configDir, "add", "db-a.yaml")
	runGit(t, configDir, "commit", "-m", "seed")
	return h, path, cfg.ComputeSourceHash([]byte(baseHashSeed))
}

func putWithBaseHash(t *testing.T, h http.Handler, body, baseHash string) *httptest.ResponseRecorder {
	t.Helper()
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a",
		bytes.NewBufferString(body))
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	if baseHash != "" {
		req.Header.Set(BaseHashHeader, baseHash)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, req)
	return w
}

func TestPutTenant_NoBaseHashHeaderWritesUnconditionally(t *testing.T) {
	t.Parallel()
	h, path, _ := baseHashFixture(t)

	if w := putWithBaseHash(t, h, baseHashUpdate, ""); w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", w.Code, w.Body.String())
	}
	assertFile(t, path, baseHashUpdate)
}

func TestPutTenant_CurrentBaseHashWrites(t *testing.T) {
	t.Parallel()
	h, path, hash := baseHashFixture(t)

	if w := putWithBaseHash(t, h, baseHashUpdate, hash); w.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200: %s", w.Code, w.Body.String())
	}
	assertFile(t, path, baseHashUpdate)
}

func TestPutTenant_StaleBaseHashConflicts(t *testing.T) {
	t.Parallel()
	h, path, stale := baseHashFixture(t)

	// Someone else writes first, so `stale` no longer describes the file.
	if w := putWithBaseHash(t, h, "tenants:\n  db-a:\n    _silent_mode: \"all\"\n", ""); w.Code != http.StatusOK {
		t.Fatalf("competing write: status = %d: %s", w.Code, w.Body.String())
	}
	current := cfg.ComputeSourceHash([]byte("tenants:\n  db-a:\n    _silent_mode: \"all\"\n"))

	w := putWithBaseHash(t, h, baseHashUpdate, stale)
	if w.Code != http.StatusConflict {
		t.Fatalf("status = %d, want 409: %s", w.Code, w.Body.String())
	}
	var env map[string]any
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("decode envelope: %v (%s)", err, w.Body.String())
	}
	if env["code"] != CodeConflict {
		t.Errorf("code = %v, want %q", env["code"], CodeConflict)
	}
	if got := env["current_source_hash"]; got != current {
		t.Errorf("current_source_hash = %v, want %q", got, current)
	}
	// The losing write must not have landed.
	assertFile(t, path, "tenants:\n  db-a:\n    _silent_mode: \"all\"\n")
}

// A header that cannot be a source_hash is a 400, never a silently
// unconditional write: the caller asked for a gate and has to learn it did not
// get one.
func TestPutTenant_MalformedBaseHashIsRejectedNotIgnored(t *testing.T) {
	t.Parallel()
	for _, bad := range []string{
		"not-a-hash",
		"DEADBEEFDEADBEEF",     // uppercase: ComputeSourceHash never emits it
		"0123456789abcde",      // 15 chars
		"0123456789abcdef0",    // 17 chars
		"\"0123456789abcdef\"", // quoted, as an If-Match habit would write it
	} {
		t.Run(bad, func(t *testing.T) {
			t.Parallel()
			h, path, _ := baseHashFixture(t)
			w := putWithBaseHash(t, h, baseHashUpdate, bad)
			if w.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400: %s", w.Code, w.Body.String())
			}
			assertFile(t, path, baseHashSeed)
		})
	}
}

// A header that is PRESENT but blank is a caller asking for the gate and
// handing over nothing to gate on. It must not fall through to the
// unconditional-write path — Header.Get flattens absent and present-blank to
// the same "", which is exactly how that silent downgrade would happen.
func TestPutTenant_BlankBaseHashHeaderIsRejected(t *testing.T) {
	t.Parallel()
	for name, value := range map[string]string{
		"empty":           "",
		"spaces":          "   ",
		"tab-and-newline": "\t",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			h, path, _ := baseHashFixture(t)
			req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a",
				bytes.NewBufferString(baseHashUpdate))
			req.Header.Set("X-Forwarded-Email", "op@example.com")
			req.Header.Set("X-Forwarded-Groups", "admins")
			req.Header.Set(BaseHashHeader, value) // present, blank
			w := httptest.NewRecorder()
			h.ServeHTTP(w, req)

			if w.Code != http.StatusBadRequest {
				t.Fatalf("status = %d, want 400: %s", w.Code, w.Body.String())
			}
			assertFile(t, path, baseHashSeed)
		})
	}
}

func assertFile(t *testing.T, path, want string) {
	t.Helper()
	got, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	if string(got) != want {
		t.Errorf("file =\n%q\nwant\n%q", got, want)
	}
}

// In PR write-back mode the on-disk file tracks the base branch, so a base
// hash cannot witness a pending PR's change. The handler must say so instead
// of writing and answering 200 — a caller that asked for a precondition and
// silently did not get one is the failure this whole header exists to prevent.
func TestPutTenant_BaseHashRejectedInPRMode(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil)
	writer := newTestWriter(configDir)
	cleanupForgeMetricsRegistries(t)
	ghClient, _ := gh.NewClient("token", "owner/repo", "main")
	tracker := gh.NewTracker(ghClient, 1<<30)

	h := PutTenant(&Deps{Writer: writer, WriteMode: WriteModePR,
		PRClient: ghClient, PRTracker: tracker})
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a",
		bytes.NewBufferString(baseHashUpdate))
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set(BaseHashHeader, cfg.ComputeSourceHash([]byte(baseHashSeed)))
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusNotImplemented {
		t.Fatalf("status = %d, want 501: %s", w.Code, w.Body.String())
	}
	if _, err := os.Stat(filepath.Join(configDir, "db-a.yaml")); !os.IsNotExist(err) {
		t.Errorf("the refused request still wrote the tenant file: stat err = %v", err)
	}
}
