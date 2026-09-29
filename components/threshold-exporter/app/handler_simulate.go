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
//   400 → {"error": "<msg>"}            // bad request shape / parse failure,
//                                       // incl. a tenant_yaml or L0 the
//                                       // exporter would skip on load (#1981)
//   404 → {"error": "<msg>"}            // tenant_id not in tenant_yaml
//   405 → {"error": "method not allowed"}
//   413 → {"error": "request too large"}
//
// ⚠️ 400 BEFORE 404 (#1981): a tenant_yaml or L0 the exporter would skip
// is a 400 even when tenant_id is also absent from the file — the whole
// file is dropped, so its tenant set is not the thing to fix first. That
// 400's {error} lists at most 10 decode errors plus "… and M more errors
// (N total)".
//
// ⚠️ The request carries no platform files, so the root platform files'
// per-tenant `tenants:` layer that /effective applies (#2019) is NOT
// applied here — see config.SimulateEffective's header. A tenant's
// `_profile` IS expanded (#2117), but only from the `profiles:` block of
// the chain's ROOT entry (L0): `_profiles.yaml` is not part of the request
// shape, so a profile defined only there is not expanded here.
//
// Why base64 for YAML payloads: YAML inside JSON requires escaping
// quotes/newlines, which is fragile when callers paste from a file.
// base64 is one well-defined transcoding; encoding/json's []byte
// type already does this for us via the standard struct tags. The
// resulting body is ~33% larger but always round-trips byte-exact —
// critical because merged_hash is computed over the raw bytes.

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"net/http"

	"github.com/vencil/threshold-exporter/pkg/config"
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
		defer func() { _ = body.Close() }()

		var req config.SimulateRequest
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

		resp, err := config.SimulateEffective(req)
		if err != nil {
			if errors.Is(err, config.ErrSimulateTenantNotFound) {
				writeSimulateError(w, http.StatusNotFound, err.Error())
				return
			}
			writeSimulateError(w, http.StatusBadRequest, err.Error())
			return
		}

		// A YAML `.inf` / `.nan` leaves a non-finite float in the tree, which
		// JSON cannot carry: send it as the text Python's json.dumps writes.
		// merged_hash was computed from the original tree, so it is unchanged.
		resp.Config = config.NonFiniteAsText(resp.Config)

		// Encode before committing the status line: encoding straight into
		// w sent 200 first, so a value encoding/json refuses shipped as 200
		// with an empty body.
		var buf bytes.Buffer
		if err := json.NewEncoder(&buf).Encode(resp); err != nil {
			writeSimulateError(w, http.StatusInternalServerError, "response encode failed: "+err.Error())
			return
		}
		w.Header().Set("Content-Type", "application/json")
		// A write error here is nearly always a client disconnect —
		// nothing to surface to the caller.
		_, _ = w.Write(buf.Bytes())
	}
}

func writeSimulateError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}
