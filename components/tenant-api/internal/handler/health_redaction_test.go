package handler

// /ready answers without authentication (cmd/server/routes.go mounts it
// outside the auth middleware), so its body must not carry the config dir,
// the human socket path or an error's text — those name server paths
// (#2811). Each failure branch answers a fixed reason; the details go to the
// log under the request_id. Logs are captured through Deps.readyLog, a per-Deps
// seam, so these tests never swap the process-wide slog default.

import (
	"encoding/json"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/go-chi/chi/v5/middleware"
)

// jsonLogger is a JSON slog.Logger writing to buf.
func jsonLogger(buf *syncBuffer) *slog.Logger {
	return slog.New(slog.NewJSONHandler(buf, nil))
}

// readyResult is one /ready call as a caller and an operator see it.
type readyResult struct {
	code      int
	raw       string
	body      map[string]string
	requestID string
}

// serveReady drives Ready behind chi's RequestID middleware, as the router
// does, so the log line carries a real request_id.
func serveReady(t *testing.T, d *Deps) readyResult {
	t.Helper()
	var reqID string
	h := middleware.RequestID(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		reqID = middleware.GetReqID(r.Context())
		Ready(d)(w, r)
	}))
	w := httptest.NewRecorder()
	h.ServeHTTP(w, httptest.NewRequest("GET", "/ready", nil))
	var body map[string]string
	if err := json.Unmarshal(w.Body.Bytes(), &body); err != nil {
		t.Fatalf("unmarshal /ready body %q: %v", w.Body.String(), err)
	}
	if reqID == "" {
		t.Fatal("RequestID middleware set no request id")
	}
	return readyResult{code: w.Code, raw: w.Body.String(), body: body, requestID: reqID}
}

// assertRedacted checks a 503 body is exactly {status: not_ready, reason}
// and names none of secrets.
func assertRedacted(t *testing.T, got readyResult, reason string, secrets ...string) {
	t.Helper()
	if got.code != http.StatusServiceUnavailable {
		t.Errorf("status = %d, want 503", got.code)
	}
	want := map[string]string{"status": "not_ready", "reason": reason}
	if len(got.body) != len(want) || got.body["status"] != want["status"] || got.body["reason"] != want["reason"] {
		t.Errorf("body = %s, want exactly %v", got.raw, want)
	}
	for _, s := range secrets {
		if s != "" && strings.Contains(got.raw, s) {
			t.Errorf("body %s exposes %q", got.raw, s)
		}
	}
}

// assertLogged checks one log line carries the request_id, the reason and
// every detail the body withheld.
func assertLogged(t *testing.T, logs *syncBuffer, got readyResult, reason string, details ...string) {
	t.Helper()
	for _, line := range strings.Split(logs.String(), "\n") {
		if !strings.Contains(line, got.requestID) || !strings.Contains(line, reason) {
			continue
		}
		for _, s := range details {
			if !strings.Contains(line, s) {
				t.Errorf("log line under request_id %s lacks %q: %s", got.requestID, s, line)
			}
		}
		return
	}
	t.Errorf("no log line carries request_id %s and reason %s; logs:\n%s", got.requestID, reason, logs.String())
}

func TestReady_MissingDir_BodyWithholdsPathAndError(t *testing.T) {
	t.Parallel()
	missing := filepath.Join(t.TempDir(), "does-not-exist")
	_, statErr := os.Stat(missing)
	if statErr == nil {
		t.Fatal("fixture: the missing dir exists")
	}
	logs := &syncBuffer{}

	got := serveReady(t, &Deps{ConfigDir: missing, readyLog: jsonLogger(logs)})

	assertRedacted(t, got, readyReasonConfigDirUnavailable, missing, statErr.Error(), "no such file")
	assertLogged(t, logs, got, readyReasonConfigDirUnavailable, missing, statErr.Error())
}

func TestReady_NotADirectory_BodyWithholdsPath(t *testing.T) {
	t.Parallel()
	file := filepath.Join(t.TempDir(), "imposter")
	if err := os.WriteFile(file, []byte("not a dir"), 0o600); err != nil {
		t.Fatal(err)
	}
	logs := &syncBuffer{}

	got := serveReady(t, &Deps{ConfigDir: file, readyLog: jsonLogger(logs)})

	assertRedacted(t, got, readyReasonConfigDirNotADir, file, "not a directory")
	assertLogged(t, logs, got, readyReasonConfigDirNotADir, file)
}

// NOT parallel: Ready writes the process-global humanSocketUp gauge
// (saveHumanSocketGauge restores it, as the other socket tests do).
func TestReady_HumanSocketDown_BodyWithholdsSocketAndError(t *testing.T) {
	saveHumanSocketGauge(t)
	ln, _ := listenUDS(t) // skip where UDS binding is unsupported
	_ = ln.Close()
	dir := t.TempDir()
	sock := filepath.Join(t.TempDir(), "never-created.sock")
	logs := &syncBuffer{}

	got := serveReady(t, &Deps{ConfigDir: dir, HumanSocketPath: sock, readyLog: jsonLogger(logs)})

	assertRedacted(t, got, readyReasonHumanSocketDown, sock, dir, "dial")
	assertLogged(t, logs, got, readyReasonHumanSocketDown, sock, dir, "dial")
}

func TestReady_OK_BodyIsStatusOnly(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	logs := &syncBuffer{}

	got := serveReady(t, &Deps{ConfigDir: dir, readyLog: jsonLogger(logs)})

	if got.code != http.StatusOK || got.raw != `{"status":"ready"}`+"\n" {
		t.Errorf("Ready = (%d, %q), want 200 {\"status\":\"ready\"}", got.code, got.raw)
	}
	if logs.String() != "" {
		t.Errorf("a ready pod logged a readiness failure: %s", logs.String())
	}
}
