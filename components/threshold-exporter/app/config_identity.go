package main

// GET /api/v1/config/identity — WHICH bytes this process is serving (#2069).
//
// A machine-read contract, versioned by its `schema` field: patch-config
// (scripts/tools/ops/patch_config.py) computes the same composite from the
// ConfigMap it just wrote and polls this until the two agree, so a pod that
// reloaded SOMEONE ELSE's earlier write can no longer pass as having loaded
// this one. /api/v1/config stays the human-read page; its one-second
// `Last reload` line was all a verifier had before.
//
//	{"schema":1,"loaded":true,"mode":"directory",
//	 "last_reload":"2026-09-27T01:02:03.456789Z",
//	 "config_hash":"<64 hex>","parse_failed":["t-b.yaml"]}
//
//   - config_hash is lastHash: in directory mode the walker's composite
//     (pkg/config/tree_scan.go: the SHA-256 of the per-file SHA-256 hex
//     digests concatenated in sorted root-relative key order, over every
//     file IsScannedFileName keeps); in single-file mode the SHA-256 of that
//     file's bytes. tests/shared/config_identity_golden.json pins the
//     directory rule for both the exporter and patch-config.
//   - parse_failed is the root-relative keys, sorted, of the files the
//     installed config left out because their bytes did not parse (see
//     flatScanState.parseFailed). Always a list, never null. Empty in
//     single-file mode: a single file that does not parse is never installed.
//   - last_reload is the install time in UTC, RFC 3339 with nanoseconds, or
//     "" before the first install (loaded:false). The response is 200 either
//     way: "nothing loaded yet" is a state this endpoint reports, not a
//     failure of the endpoint — /ready is the gate for that.
//
// ⛔ ONE RLock FOR ALL FIELDS. installConfig writes config, lastHash,
// lastReload and m.flat (parseFailed) in one lock window; reading them in
// several would let a reload land between the reads and pair one install's
// hash with another's parse_failed. Reloads are serialised by reloadMu
// (#2122), but this reader does not take it, and within ONE reload the
// hierarchy plane is written in a DIFFERENT lock window from the one this
// reads, so nothing from m.hierarchy (hashes, merged_hash) may be added here.

import (
	"encoding/json"
	"net/http"
	"time"
)

// configIdentitySchema is the `schema` of the identity document. Bump it
// when a field changes meaning or goes away; adding a field does not.
const configIdentitySchema = 1

// ConfigIdentity is the installed config's identity, read in one lock window.
type ConfigIdentity struct {
	Loaded      bool
	Mode        string
	LastReload  time.Time
	ConfigHash  string
	ParseFailed []string // sorted; never nil
}

// Identity returns what the last install put in place (see the file header).
func (m *ConfigManager) Identity() ConfigIdentity {
	m.mu.RLock()
	defer m.mu.RUnlock()
	id := ConfigIdentity{
		Loaded:      m.loaded,
		Mode:        m.Mode(),
		LastReload:  m.lastReload,
		ConfigHash:  m.lastHash,
		ParseFailed: []string{},
	}
	if m.isDir {
		// A copy: the slice is replaced wholesale on every commit and never
		// mutated, but the caller owns what it is handed.
		id.ParseFailed = append(id.ParseFailed, m.flat.parseFailed...)
	}
	return id
}

// configIdentityDoc is the wire shape (schema 1).
type configIdentityDoc struct {
	Schema      int      `json:"schema"`
	Loaded      bool     `json:"loaded"`
	Mode        string   `json:"mode"`
	LastReload  string   `json:"last_reload"`
	ConfigHash  string   `json:"config_hash"`
	ParseFailed []string `json:"parse_failed"`
}

func (id ConfigIdentity) doc() configIdentityDoc {
	d := configIdentityDoc{
		Schema:      configIdentitySchema,
		Loaded:      id.Loaded,
		Mode:        id.Mode,
		ConfigHash:  id.ConfigHash,
		ParseFailed: id.ParseFailed,
	}
	if d.ParseFailed == nil {
		d.ParseFailed = []string{}
	}
	if id.Loaded {
		d.LastReload = id.LastReload.UTC().Format(time.RFC3339Nano)
	}
	return d
}

// configIdentityHandler serves GET (and HEAD) /api/v1/config/identity.
func configIdentityHandler(manager *ConfigManager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet && r.Method != http.MethodHead {
			w.Header().Set("Allow", "GET, HEAD")
			http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
			return
		}
		body, err := json.Marshal(manager.Identity().doc())
		if err != nil { // not reachable: every field is a plain value
			http.Error(w, "cannot encode identity", http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "no-store")
		w.WriteHeader(http.StatusOK)
		if r.Method == http.MethodGet {
			_, _ = w.Write(append(body, '\n'))
		}
	}
}
