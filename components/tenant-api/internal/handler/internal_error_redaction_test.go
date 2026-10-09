package handler

// #1700: an INTERNAL_ERROR body never carries the server's own error text.
// WriteErrorEnvelope withholds it (logging it under the request_id), so a call
// site that answers `err.Error()` cannot leak a path, and neither can the next
// one written. The federation reproduction from the issue lives in
// federation/internal_error_redaction_test.go.

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/go-chi/chi/v5/middleware"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
)

const leakyText = "read federation subset: read /srv/conf.d/_federation/t.yaml: is a directory"

func envelopeBody(t *testing.T, status int, code, msg string) ErrorResponse {
	t.Helper()
	w := httptest.NewRecorder()
	WriteErrorEnvelope(w, httptest.NewRequest("GET", "/x", nil), status, ErrorResponse{Error: msg, Code: code})
	var got ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("unmarshal %s: %v", w.Body.String(), err)
	}
	return got
}

func TestWriteErrorEnvelope_WithholdsInternalErrorText(t *testing.T) {
	t.Parallel()
	got := envelopeBody(t, http.StatusInternalServerError, CodeInternal, leakyText)
	if got.Error != msgInternal || got.Code != CodeInternal {
		t.Errorf("envelope = %+v, want code %s with %q", got, CodeInternal, msgInternal)
	}
	// WriteJSONError infers INTERNAL_ERROR from the status — the shape of
	// every one of the 21 call sites #1700 counted.
	w := httptest.NewRecorder()
	WriteJSONError(w, nil, http.StatusInternalServerError, leakyText)
	if strings.Contains(w.Body.String(), "/srv/conf.d") {
		t.Errorf("WriteJSONError 500 leaks the text: %s", w.Body.String())
	}
}

// What stays verbatim: the fixed INTERNAL_ERROR sentences that name an
// operation and nothing about the server, and every other code — 503s with
// their designed messages, 4xx, and the codes made for text the client is
// meant to read.
func TestWriteErrorEnvelope_KeepsWhatIsMeantForTheClient(t *testing.T) {
	t.Parallel()
	for msg := range publicInternalMessages {
		if got := envelopeBody(t, http.StatusInternalServerError, CodeInternal, msg); got.Error != msg {
			t.Errorf("public INTERNAL_ERROR text %q became %q", msg, got.Error)
		}
	}
	for _, c := range []struct {
		status int
		code   string
	}{
		{http.StatusServiceUnavailable, CodeWriteOverloaded},
		{http.StatusServiceUnavailable, CodeUpstream},
		{http.StatusBadRequest, CodeBadRequest},
		{http.StatusInternalServerError, CodeConfigDecode},
		{http.StatusInternalServerError, CodeBaseRestoreFailed},
	} {
		if got := envelopeBody(t, c.status, c.code, leakyText); got.Error != leakyText {
			t.Errorf("%d %s: text became %q, want it unchanged", c.status, c.code, got.Error)
		}
	}
}

// syncBuffer: production slog lines from other tests may land concurrently.
type syncBuffer struct {
	mu  sync.Mutex
	buf bytes.Buffer
}

func (b *syncBuffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.Write(p)
}

func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

// The withheld text is not lost: it is logged with the request_id the client
// receives, so an operator can go from the response to the cause.
//
// Intentionally NOT t.Parallel(): it swaps the process-wide slog default, as
// TestSlogRequestLogger_EmitsStructuredLine does.
func TestWriteErrorEnvelope_LogsWithheldTextUnderTheRequestID(t *testing.T) {
	orig := slog.Default()
	defer slog.SetDefault(orig)
	var logs syncBuffer
	slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))

	w := httptest.NewRecorder()
	h := middleware.RequestID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		WriteJSONError(w, r, http.StatusInternalServerError, leakyText)
	}))
	h.ServeHTTP(w, httptest.NewRequest("GET", "/api/v1/tenants/t/federation", nil))

	var got ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.RequestID == "" {
		t.Fatalf("no request_id in %s — the client has nothing to quote", w.Body.String())
	}
	line := ""
	for _, l := range strings.Split(logs.String(), "\n") {
		if strings.Contains(l, got.RequestID) && strings.Contains(l, "detail withheld") {
			line = l
		}
	}
	if !strings.Contains(line, "/srv/conf.d/_federation/t.yaml") {
		t.Errorf("no log line carries the withheld text under request_id %s; logs:\n%s", got.RequestID, logs.String())
	}
}

func TestWriteErrorIsForClient(t *testing.T) {
	t.Parallel()
	forClient := []error{
		fmt.Errorf("%w: YAML must contain tenants.t section", gitops.ErrValidation),
		fmt.Errorf("%w: %q", gitops.ErrReservedTenantID, "_defaults"),
		fmt.Errorf("%w %q: bad", gitops.ErrInvalidTenantID, "A"),
		fmt.Errorf("%w for t: %w", gitops.ErrMergeFailed, errors.New("cannot patch structured key")),
		fmt.Errorf("%w: %q is claimed by %v", confd.ErrAmbiguousTenantFile, "t", []string{"t.yaml", "t.yml"}),
		fmt.Errorf("%w: %s", confd.ErrNotRegularFile, "t.yaml"),
	}
	for _, err := range forClient {
		if !writeErrorIsForClient(err) {
			t.Errorf("%v: want it shown to the client", err)
		}
	}
	notForClient := []error{
		errors.New("rename temp file: rename /srv/conf.d/.t.yaml.tmp /srv/conf.d/t.yaml: permission denied"),
		fmt.Errorf("read current tenant file for t: %w", os.ErrPermission),
		fmt.Errorf("git commit: %w", errors.New("exit status 128")),
	}
	for _, err := range notForClient {
		if writeErrorIsForClient(err) {
			t.Errorf("%v: a server-side failure must not be shown", err)
		}
		if got := writeErrorText(nil, err); got != msgWriteFailed {
			t.Errorf("writeErrorText(%v) = %q, want %q", err, got, msgWriteFailed)
		}
	}
}

// The batch per-op message: a server-side failure on one op becomes
// msgBatchOpFailed, while a failure about the tenant's own file keeps its
// text. (A file the merge cannot use is refused as "not loadable" before the
// merge runs, so ErrMergeFailed's classification is pinned by
// TestWriteErrorIsForClient rather than here.)
func TestBatchTenants_ServerSideOpFailureNamesNoServerPath(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{
		// db-b's file is a list: refused as a file the client must repair.
		"db-b.yaml": "- not a tenant mapping\n",
		// db-c is claimed by two files: a conf.d layout the operator fixes.
		"db-c.yaml": "tenants:\n  db-c:\n    _silent_mode: \"warning\"\n",
		"db-c.yml":  "tenants:\n  db-c:\n    _silent_mode: \"warning\"\n",
	})
	initGitRepo(t, configDir)
	// db-a's file is a directory: reading it fails server-side.
	if err := os.MkdirAll(filepath.Join(configDir, "db-a.yaml", "x"), 0o755); err != nil {
		t.Fatal(err)
	}
	rbacMgr := adminRBAC(t)
	deps := &Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect}
	body := `{"operations":[
		{"tenant_id":"db-a","patch":{"_silent_mode":"warning"}},
		{"tenant_id":"db-b","patch":{"_silent_mode":"warning"}},
		{"tenant_id":"db-c","patch":{"_silent_mode":"critical"}}
	]}`
	req := httptest.NewRequest("POST", "/api/v1/tenants/batch", bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(BatchTenants(deps), rbacMgr, "read", nil).ServeHTTP(w, req)

	if strings.Contains(w.Body.String(), configDir) {
		t.Errorf("batch response carries conf.d's path: %s", w.Body.String())
	}
	var resp BatchResponse
	if err := json.Unmarshal(w.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal %s: %v", w.Body.String(), err)
	}
	byTenant := map[string]BatchResult{}
	for _, r := range resp.Results {
		byTenant[r.TenantID] = r
	}
	if got := byTenant["db-a"]; got.Status != "error" || got.Message != msgBatchOpFailed {
		t.Errorf("db-a (server-side failure) = %+v, want error %q", got, msgBatchOpFailed)
	}
	if got := byTenant["db-b"]; got.Status != "error" || got.Code != CodeTenantConfigNotLoadable || !strings.Contains(got.Message, "db-b") {
		t.Errorf("db-b (client-facing failure) = %+v, want its own not-loadable text", got)
	}
	if got := byTenant["db-c"]; got.Status != "error" || !strings.Contains(got.Message, "is claimed by") {
		t.Errorf("db-c (two files claim it) = %+v, want its own ambiguous-tenant text", got)
	}
}

// End to end on the direct write path: a write that fails for a server-side
// reason (conf.d is not a git repository, so the commit fails) keeps its
// status — the tenant write fallback is a 400 — but answers msgWriteFailed
// instead of git's own text.
func TestPutTenant_ServerSideWriteFailureNamesNoServerPath(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, nil) // deliberately not a git repo
	rbacMgr := adminRBAC(t)
	h := PutTenant(&Deps{Writer: newTestWriter(configDir), ConfigDir: configDir, RBAC: rbacMgr, WriteMode: WriteModeDirect})
	req := newRequestWithChiParam("PUT", "/api/v1/tenants/db-a", "id", "db-a",
		bytes.NewBufferString("tenants:\n  db-a:\n    _silent_mode: \"critical\"\n"))
	req.Header.Set("X-Forwarded-Email", "alice@example.com")
	req.Header.Set("X-Forwarded-Groups", "admins")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(h, rbacMgr, "write", TenantIDFromPath).ServeHTTP(w, req)

	if w.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want the 400 fallback (the fault did not fire as designed); body: %s", w.Code, w.Body.String())
	}
	var got ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got.Error != msgWriteFailed {
		t.Errorf("error = %q, want %q (git's own text stays in the log)", got.Error, msgWriteFailed)
	}
}

// The metric-discovery 502: the upstream error is a *url.Error naming the
// internal Prometheus URL, host and IP. Its own code (UPSTREAM_ERROR) is not
// withheld by the envelope, so the handler writes fixed text itself.
func TestDiscoverMetrics_UpstreamFailureNamesNoUpstream(t *testing.T) {
	t.Parallel()
	const upstream = "http://127.0.0.1:1" // nothing listens: connection refused
	h := DiscoverMetrics(&Deps{MetricDiscoverer: fedpolicy.NewMetricDiscoverer(upstream)})
	w := httptest.NewRecorder()
	h(w, newRequestWithChiParam("GET", "/api/v1/tenants/db-a/metrics", "id", "db-a", nil))

	if w.Code != http.StatusBadGateway {
		t.Fatalf("status = %d, want 502 (the fault did not fire as designed); body: %s", w.Code, w.Body.String())
	}
	if strings.Contains(w.Body.String(), "127.0.0.1") {
		t.Errorf("502 body names the upstream: %s", w.Body.String())
	}
}

// _groups.yaml that does not decode answers CONFIG_DECODE_ERROR with the
// decoder's text: the operator must fix the file, and the text says what is
// wrong. Without its own code the envelope would withhold it.
func TestPutGroup_UnparseableGroupsFileIsConfigDecodeError(t *testing.T) {
	t.Parallel()
	configDir := setupConfigDir(t, map[string]string{"_groups.yaml": "grops:\n  g-a:\n    label: a\n"})
	initGitRepo(t, configDir)
	rbacMgr := newRBACManager(t, partialWriterRBAC)
	d := &Deps{Writer: gitops.NewWriter(configDir, configDir), Groups: groups.NewManager(configDir), RBAC: rbacMgr}
	body, _ := json.Marshal(PutGroupRequest{Label: "gx", Members: []string{"t-owned"}})
	req := newRequestWithChiParam("PUT", "/api/v1/groups/g-x", "id", "g-x", bytes.NewBuffer(body))
	req.Header.Set("X-Forwarded-Email", "op@example.com")
	req.Header.Set("X-Forwarded-Groups", "readers,owners")
	w := httptest.NewRecorder()
	wrapWithRBACMiddleware(PutGroup(d), rbacMgr, rbac.PermRead, nil).ServeHTTP(w, req)

	var got ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &got); err != nil {
		t.Fatalf("unmarshal %s: %v", w.Body.String(), err)
	}
	if w.Code != http.StatusInternalServerError || got.Code != CodeConfigDecode || !strings.Contains(got.Error, "_groups.yaml") {
		t.Errorf("= %d %+v, want 500 %s naming _groups.yaml", w.Code, got, CodeConfigDecode)
	}
	if strings.Contains(w.Body.String(), configDir) {
		t.Errorf("body carries conf.d's path: %s", w.Body.String())
	}
}
