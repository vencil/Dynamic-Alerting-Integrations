package testutil

// FakeGenerator: a stand-in for the generator container's service
// (`da-tools generator-service`, #2903) for Go unit tests — an HTTP server on
// a Unix socket speaking the three endpoints with the service's shapes
// (docs/cli-reference.md §generator-service). Each answer is the test's to
// choose; the default validate answer is a `pass` that names exactly what
// was sent, which is what the real service answers for a tree that passes.
//
// It is for unit tests only: the integration test runs the real service
// (internal/handler/generator_real_test.go).

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"testing"
)

// FakeWrite is one write of a validate request, decoded.
type FakeWrite struct {
	Path    string
	Content []byte
}

// FakeValidateRequest is one POST /v1/validate as received.
type FakeValidateRequest struct {
	BaseCommit string
	Writes     []FakeWrite
	DeadlineMS int64
}

// FakeGenerator is a fake generator service. Change the hooks with the
// Set* methods (they are read under mu by the serving goroutine); Requests
// records every validate request in order.
type FakeGenerator struct {
	Socket string

	mu sync.Mutex
	// Validate answers POST /v1/validate: an HTTP status and a JSON body.
	// nil → 200 PassVerdict(req, nil).
	Validate func(n int, req FakeValidateRequest) (int, any)
	// Status answers GET /v1/status. nil → 200 ready.
	Status func() (int, any)
	// Metrics answers GET /metrics. nil → 200 one da_generator_build_info.
	Metrics  func() (int, string)
	requests []FakeValidateRequest
	srv      *http.Server
}

// StartFakeGenerator serves a FakeGenerator on a socket in a fresh short
// directory (a Unix socket path is limited to ~107 bytes, more than a
// t.TempDir path may leave); both are removed when t ends.
func StartFakeGenerator(t testing.TB) *FakeGenerator {
	t.Helper()
	dir, err := os.MkdirTemp("", "fg")
	if err != nil {
		t.Fatal(err)
	}
	f := &FakeGenerator{Socket: filepath.Join(dir, "g.sock")}
	ln, err := net.Listen("unix", f.Socket)
	if err != nil {
		_ = os.RemoveAll(dir)
		t.Fatal(err)
	}
	f.srv = &http.Server{Handler: http.HandlerFunc(f.serve)}
	go func() { _ = f.srv.Serve(ln) }()
	t.Cleanup(func() {
		_ = f.srv.Close()
		_ = os.RemoveAll(dir)
	})
	return f
}

// Requests is every validate request received so far.
func (f *FakeGenerator) Requests() []FakeValidateRequest {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]FakeValidateRequest(nil), f.requests...)
}

// SetValidate replaces the validate hook.
func (f *FakeGenerator) SetValidate(fn func(n int, req FakeValidateRequest) (int, any)) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Validate = fn
}

// SetStatus replaces the status hook.
func (f *FakeGenerator) SetStatus(fn func() (int, any)) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Status = fn
}

// SetMetrics replaces the metrics hook.
func (f *FakeGenerator) SetMetrics(fn func() (int, string)) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.Metrics = fn
}

// PassVerdict is the service's 200 `pass` naming exactly req's base, writes
// and policySHA (nil: no deployment policy file).
func PassVerdict(req FakeValidateRequest, policySHA *string) map[string]any {
	v := Verdict("pass", req, policySHA)
	v["rc"] = 0
	return v
}

// FindingsVerdict is a 200 `findings` with these findings (each a map in
// the --findings-json entry shape) and configDir as the document's
// config_dir.
func FindingsVerdict(req FakeValidateRequest, configDir string, findings ...map[string]any) map[string]any {
	v := Verdict("findings", req, nil)
	v["rc"] = 1
	if findings == nil {
		findings = []map[string]any{}
	}
	v["findings"] = map[string]any{"schema": "da-tools.findings/v1", "config_dir": configDir,
		"exit_code": 1, "findings": findings}
	return v
}

// RejectedVerdict is a 200 `rejected` with reason.
func RejectedVerdict(reason string) map[string]any {
	return map[string]any{"result": "rejected", "reason": reason, "message": "fake " + reason,
		"rc": nil, "findings": nil, "stderr_tail": "", "validated": nil}
}

// Verdict is a 200 body with result and a `validated` naming req.
func Verdict(result string, req FakeValidateRequest, policySHA *string) map[string]any {
	files := make([]map[string]any, 0, len(req.Writes))
	for _, w := range req.Writes {
		sum := sha256.Sum256(w.Content)
		files = append(files, map[string]any{"path": w.Path, "sha256": hex.EncodeToString(sum[:])})
	}
	var policy any
	if policySHA != nil {
		policy = *policySHA
	}
	return map[string]any{"result": result, "rc": nil, "findings": nil, "stderr_tail": "",
		"validated": map[string]any{"base_commit": req.BaseCommit, "files": files, "policy_sha256": policy}}
}

// Finding is one --findings-json entry; empty tenant / file are null.
func Finding(kind, tenant, file, message string) map[string]any {
	str := func(s string) any {
		if s == "" {
			return nil
		}
		return s
	}
	return map[string]any{"kind": kind, "severity": "error", "blocks": "always", "tenant": str(tenant),
		"policy": nil, "file": str(file), "field": nil, "message": message}
}

func (f *FakeGenerator) serve(w http.ResponseWriter, r *http.Request) {
	reply := func(status int, body any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(body)
	}
	switch r.URL.Path {
	case "/v1/validate":
		raw, _ := io.ReadAll(r.Body)
		var wire struct {
			BaseCommit string `json:"base_commit"`
			Writes     []struct {
				Path       string `json:"path"`
				ContentB64 string `json:"content_b64"`
			} `json:"writes"`
			DeadlineMS int64 `json:"deadline_ms"`
		}
		if err := json.Unmarshal(raw, &wire); err != nil {
			reply(400, map[string]any{"error": "BAD_REQUEST", "message": err.Error()})
			return
		}
		req := FakeValidateRequest{BaseCommit: wire.BaseCommit, DeadlineMS: wire.DeadlineMS}
		for _, wr := range wire.Writes {
			c, err := base64.StdEncoding.DecodeString(wr.ContentB64)
			if err != nil {
				reply(400, map[string]any{"error": "BAD_REQUEST", "message": err.Error()})
				return
			}
			req.Writes = append(req.Writes, FakeWrite{Path: wr.Path, Content: c})
		}
		f.mu.Lock()
		f.requests = append(f.requests, req)
		n, hook := len(f.requests), f.Validate
		f.mu.Unlock()
		if hook == nil {
			reply(200, PassVerdict(req, nil))
			return
		}
		status, body := hook(n, req)
		reply(status, body)
	case "/v1/status":
		f.mu.Lock()
		hook := f.Status
		f.mu.Unlock()
		if hook == nil {
			reply(200, map[string]any{"policy": map[string]any{"configured": false, "available": true},
				"amtool_available": true, "ready": true})
			return
		}
		status, body := hook()
		reply(status, body)
	case "/metrics":
		f.mu.Lock()
		hook := f.Metrics
		f.mu.Unlock()
		status, text := 200, "# TYPE da_generator_build_info gauge\nda_generator_build_info{generator_version=\"fake\",da_guard_version=\"\"} 1\n"
		if hook != nil {
			status, text = hook()
		}
		w.Header().Set("Content-Type", "text/plain; version=0.0.4")
		w.WriteHeader(status)
		_, _ = io.WriteString(w, text)
	default:
		reply(404, map[string]any{"error": "NOT_FOUND"})
	}
}
