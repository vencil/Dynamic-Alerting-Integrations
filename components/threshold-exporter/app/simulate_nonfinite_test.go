package main

// simulate_nonfinite_test.go — /simulate on a tenant whose YAML holds `.inf`.
//
// Before: the handler encoded straight into the response after committing 200,
// encoding/json refused the +Inf in effective_config, and the caller got 200
// with an empty body. Now the value is sent as the text Python's json.dumps
// writes, and merged_hash — computed from the original tree — is Python's.

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func TestSimulateHandler_NonFiniteFloat_ReadableWithPythonHash(t *testing.T) {
	t.Parallel()

	body, _ := json.Marshal(config.SimulateRequest{
		TenantID: "t-inf",
		TenantYAML: []byte(`tenants:
  t-inf:
    _routing:
      receiver:
        type: webhook
        url: "https://hooks.example.com/a"
      overrides:
        - alertname: HighCPU
          match:
            severity: .inf
          receiver:
            type: webhook
            url: "https://hooks.example.com/b"
`),
	})
	rec := httptest.NewRecorder()
	simulateHandler()(rec, httptest.NewRequest(http.MethodPost, "/api/v1/tenants/simulate", bytes.NewReader(body)))

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want 200 (body %q)", rec.Code, rec.Body.String())
	}
	var resp config.SimulateResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("body is not JSON: %v (%q)", err, rec.Body.String())
	}
	ov := resp.Config["_routing"].(map[string]any)["overrides"].([]any)[0].(map[string]any)
	if got := ov["match"].(map[string]any)["severity"]; got != "Infinity" {
		t.Errorf("severity = %#v, want \"Infinity\"", got)
	}
	// sha256 of Python's json.dumps(sort_keys=True, separators=(",", ":"),
	// ensure_ascii=False) of the same tenant body with the built-in table
	// (#3832) laid under it, first 16 hex (same fixture as pkg/config's
	// RoutingOverrideInf test).
	if resp.MergedHash != "487e10b578be6600" {
		t.Errorf("merged_hash = %q, want Python's 487e10b578be6600", resp.MergedHash)
	}
}
