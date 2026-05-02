package main

// ============================================================
// /api/v1/tenants/simulate HTTP handler (v2.8.0 Phase .c C-7b)
// ============================================================
//
// Thin transport wrapper over SimulateEffective. All semantic
// decisions (validation, merge rules, error taxonomy) live in
// config_simulate.go; this file only translates HTTP ↔ Go types.
//
// Contract (chosen to mirror /effective so reviewers can diff
// the two endpoints field-for-field):
//
//   POST /api/v1/tenants/simulate
//   Content-Type: application/json
//   Body: {
//     "tenant_id":           "<id>",
//     "tenant_yaml":         "<base64 raw bytes>",
//     "defaults_chain_yaml": ["<b64 L0>", "<b64 L1>", ...]
//   }
//   200 → SimulateResponse JSON
//   400 → {"error": "<msg>"}            // bad request shape / parse failure
//   404 → {"error": "<msg>"}            // tenant_id not in tenant_yaml
//   405 → {"error": "method not allowed"}
//   413 → {"error": "request too large"}
//
// Why base64 for YAML payloads: YAML inside JSON requires escaping
// quotes/newlines, which is fragile when callers paste from a file.
// base64 is one well-defined transcoding; encoding/json's []byte
// type already does this for us via the standard struct tags. The
// resulting body is ~33% larger but always round-trips byte-exact —
// critical because merged_hash is computed over the raw bytes.

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
)

// simulateMaxBodyBytes caps a single /simulate request body. 1 MiB
// covers a tenant.yaml + a deep defaults chain comfortably (~10 ×
// 100 KiB) while keeping a malicious caller from holding the whole
// HTTP buffer pool. Aligns with the existing 8 KiB MaxHeaderBytes
// + the rest of the listen-side defaults in main.go.
const simulateMaxBodyBytes = 1 << 20 // 1 MiB

func simulateHandler() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeSimulateError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}

		// http.MaxBytesReader replaces the body with one that returns
		// http.MaxBytesError once the cap is exceeded. Cheaper than
		// reading the full Content-Length up front and lets us reject
		// chunked uploads too.
		body := http.MaxBytesReader(w, r.Body, simulateMaxBodyBytes)
		defer body.Close()

		var req SimulateRequest
		dec := json.NewDecoder(body)
		dec.DisallowUnknownFields()
		if err := dec.Decode(&req); err != nil {
			var maxErr *http.MaxBytesError
			switch {
			case errors.As(err, &maxErr):
				writeSimulateError(w, http.StatusRequestEntityTooLarge, "request too large")
			case errors.Is(err, io.EOF):
				// io.EOF here = client sent an empty body (zero-byte
				// POST). Distinct failure mode from "body exceeded cap"
				// even though the symptom (no SimulateRequest decoded)
				// is the same. 400 keeps callers honest.
				writeSimulateError(w, http.StatusBadRequest, "empty request body")
			default:
				writeSimulateError(w, http.StatusBadRequest, "invalid request body: "+err.Error())
			}
			return
		}

		resp, err := SimulateEffective(req)
		if err != nil {
			if errors.Is(err, ErrSimulateTenantNotFound) {
				writeSimulateError(w, http.StatusNotFound, err.Error())
				return
			}
			writeSimulateError(w, http.StatusBadRequest, err.Error())
			return
		}

		w.Header().Set("Content-Type", "application/json")
		// Encoder errors here are nearly always client disconnects after
		// we've already written the status line — there's no useful
		// recovery path and nothing to surface to the caller.
		_ = json.NewEncoder(w).Encode(resp)
	}
}

func writeSimulateError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}
