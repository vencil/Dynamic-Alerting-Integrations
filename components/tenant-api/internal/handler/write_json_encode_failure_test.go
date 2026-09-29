package handler

// write_json_encode_failure_test.go — a value encoding/json refuses must not
// ship as `200` with an empty body.
//
// writeJSON used to commit the status line before encoding, so when Encode
// failed there was nothing left to report: the client got a success it could
// not parse and the log got nothing. The reachable case is /effective on a
// tenant whose YAML holds `.inf` / `-.inf` / `.nan` — pkg/config resolves it
// (merged_hash renders it the way Python's json.dumps does), but
// effective_config itself is a float64 ±Inf / NaN, which JSON cannot carry.

import (
	"encoding/json"
	"math"
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"strings"
	"testing"
)

func TestWriteJSON_UnencodableValue_Is500Envelope(t *testing.T) {
	t.Parallel()

	for _, v := range []float64{math.Inf(1), math.Inf(-1), math.NaN()} {
		w := httptest.NewRecorder()
		writeJSON(w, http.StatusOK, map[string]any{"v": v})

		if w.Code != http.StatusInternalServerError {
			t.Fatalf("%v: status = %d, want 500 (body %q)", v, w.Code, w.Body.String())
		}
		var env map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
			t.Fatalf("%v: body is not JSON: %v (%q)", v, err, w.Body.String())
		}
		if env["code"] != CodeInternal {
			t.Errorf("%v: code = %v, want %s", v, env["code"], CodeInternal)
		}
		if msg, _ := env["error"].(string); !strings.Contains(msg, "unsupported value") {
			t.Errorf("%v: error = %q, want it to name the encode error", v, msg)
		}
	}
}

// The success path must be byte-identical to encoding straight into the
// response: same Encoder, same trailing newline.
func TestWriteJSON_EncodableValue_UnchangedBytes(t *testing.T) {
	t.Parallel()

	w := httptest.NewRecorder()
	writeJSON(w, http.StatusCreated, map[string]any{"a": "<&>", "n": 1.5})

	if w.Code != http.StatusCreated {
		t.Fatalf("status = %d, want 201", w.Code)
	}
	if got, want := w.Body.String(), "{\"a\":\"\\u003c\\u0026\\u003e\",\"n\":1.5}\n"; got != want {
		t.Errorf("body = %q, want %q", got, want)
	}
	if ct := w.Header().Get("Content-Type"); ct != "application/json" {
		t.Errorf("Content-Type = %q, want application/json", ct)
	}
}

func TestGetTenantEffective_NonFiniteFloat_Is500NotEmpty200(t *testing.T) {
	t.Parallel()

	dir := t.TempDir()
	writeFile(t, filepath.Join(dir, "t-inf.yaml"), `tenants:
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
`)

	h := GetTenantEffective(&Deps{ConfigDir: dir})
	req := newRequestWithChiParam("GET", "/api/v1/tenants/t-inf/effective", "id", "t-inf", nil)
	w := httptest.NewRecorder()
	h(w, req)

	if w.Code != http.StatusInternalServerError {
		t.Fatalf("status = %d, want 500 (body %q)", w.Code, w.Body.String())
	}
	var env ErrorResponse
	if err := json.Unmarshal(w.Body.Bytes(), &env); err != nil {
		t.Fatalf("body is not a JSON error envelope: %v (%q)", err, w.Body.String())
	}
	if env.Code != CodeInternal || !strings.Contains(env.Error, "+Inf") {
		t.Errorf("envelope = %+v, want code %s naming +Inf", env, CodeInternal)
	}
}
