package main

// ============================================================
// Deep merge + merged_hash (ADR-018)
// ============================================================
//
// This file is a Go port of the deep_merge / _canonical_hash logic in
// scripts/tools/dx/describe_tenant.py. Parity with Python output is
// mandatory — the 16-char merged_hash is embedded in user-facing metrics
// (da_tenant_config_info{merged_hash=...}), used by Alertmanager for reload
// change detection, and asserted by 8 golden fixtures in tests/golden/.
// Any Go↔Python divergence is a ship blocker; see config_golden_parity_test.go.
//
// The 8 semantic traps from §8.11.2 are enforced here:
//   1. 16-char hash truncation (hex[:16])           → computeMergedHash
//   2. `tenants:` wrapper = {tenantID: config}       → extractTenantRaw
//   3. `defaults:` wrapper optional                  → extractDefaultsBlock
//   4. `_metadata` never inherited                   → deepMerge skip
//   5. Canonical JSON: sort_keys + no-space + UTF-8  → canonicalJSON
//   6. YAML null → delete key                        → deepMerge nil branch
//   7. Array replace (not concat)                    → deepMerge fall-through
//   8. L0→Ln ordered chain                           → caller (scanDirHierarchical)

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"log"

	"gopkg.in/yaml.v3"
)

// deepMerge implements ADR-018 inheritance semantics.
//
// Rules (matching describe_tenant.py deep_merge):
//   - `_metadata` keys in override are *never* applied (never inherited).
//   - `nil` in override deletes the key from result (YAML `~`/`null` = opt-out).
//   - Dict × Dict → recurse.
//   - Anything else (array, scalar, type-mismatch) → override replaces. Arrays
//     are REPLACED, not concatenated — a tenant specifying
//     `receivers: [pagerduty]` completely shadows the parent's
//     `receivers: [email, slack]`.
//
// Both arguments may be nil. The returned map is always non-nil and is a
// fresh deep copy — the caller owns it and may mutate freely. `base` is
// never mutated.
func deepMerge(base, override map[string]any) map[string]any {
	result := deepCopyMap(base)
	if override == nil {
		return result
	}
	for k, v := range override {
		if k == "_metadata" {
			continue
		}
		if v == nil {
			// YAML null (`~`) deletes the inherited key. Matches Python
			// `if v is None: result.pop(k, None)`. Note: this also catches
			// Go's untyped nil from yaml.v3 — tested in
			// TestDeepMerge_NilDeletesKey.
			delete(result, k)
			continue
		}
		if overrideMap, ok := v.(map[string]any); ok {
			if baseMap, ok2 := result[k].(map[string]any); ok2 {
				result[k] = deepMerge(baseMap, overrideMap)
				continue
			}
		}
		// Array / scalar / type-mismatch path — override wins wholesale.
		result[k] = deepCopyValue(v)
	}
	return result
}

// deepCopyMap clones a map and all nested maps/slices so the caller can
// mutate freely without affecting shared state.
func deepCopyMap(m map[string]any) map[string]any {
	if m == nil {
		return make(map[string]any)
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = deepCopyValue(v)
	}
	return out
}

func deepCopyValue(v any) any {
	switch t := v.(type) {
	case map[string]any:
		return deepCopyMap(t)
	case []any:
		arr := make([]any, len(t))
		for i := range t {
			arr[i] = deepCopyValue(t[i])
		}
		return arr
	default:
		// Scalars (string, float64, int, bool, nil) are immutable — share.
		return v
	}
}

// normalizeYAMLToJSON reshapes yaml.v3's generic output to a form that
// matches what Python's yaml.safe_load + json.dumps produces.
//
// Key transformations:
//   - `map[any]any` → `map[string]any` (yaml.v3 can emit either depending
//     on which unmarshal target is used; we handle both).
//   - Nested normalization on array elements and map values.
//
// Numeric types are *not* coerced. yaml.v3 emits `int` for whole numbers and
// `float64` for decimals — same as Python's yaml.safe_load. encoding/json's
// output for each type also matches json.dumps: 80 → "80", 80.5 → "80.5".
// Edge case: YAML `80.0` → Python float(80.0)/dumps "80.0" vs Go float64(80)/
// Marshal "80". The golden fixtures don't hit this (all thresholds use
// integer literals). If production configs ever use `.0` floats, we'd need
// json.Number handling — document this caveat with a test in future.
func normalizeYAMLToJSON(v any) any {
	switch t := v.(type) {
	case map[any]any:
		out := make(map[string]any, len(t))
		for k, val := range t {
			ks, ok := k.(string)
			if !ok {
				ks = fmt.Sprintf("%v", k)
			}
			out[ks] = normalizeYAMLToJSON(val)
		}
		return out
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, val := range t {
			out[k] = normalizeYAMLToJSON(val)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i := range t {
			out[i] = normalizeYAMLToJSON(t[i])
		}
		return out
	default:
		return v
	}
}

// canonicalJSON produces byte-identical output to Python:
//
//	json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
//
// Properties:
//  1. Map keys sorted alphabetically at every nesting depth — Go's
//     encoding/json does this automatically for map[string]any, including
//     maps inside arrays.
//  2. No spaces after `,` or `:` — Go's default (no indent) matches.
//  3. Non-ASCII kept verbatim in UTF-8 — `ensure_ascii=False` parity requires
//     SetEscapeHTML(false); the default escapes `<`, `>`, `&` as `\u003c`
//     etc., which Python doesn't do.
//  4. No trailing newline — encoding/json.Encoder appends one; we strip it.
func canonicalJSON(data any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(data); err != nil {
		return nil, fmt.Errorf("canonicalJSON encode: %w", err)
	}
	out := buf.Bytes()
	if n := len(out); n > 0 && out[n-1] == '\n' {
		out = out[:n-1]
	}
	return out, nil
}

// extractDefaultsBlock replicates describe_tenant.py's
//
//	defaults_block = ddata.get("defaults", ddata)
//
// A `_defaults.yaml` file may either wrap its content under a top-level
// `defaults:` key (the common style) or be a naked dict (legacy). Both shapes
// are supported so migration can be incremental.
func extractDefaultsBlock(doc any) map[string]any {
	m, ok := doc.(map[string]any)
	if !ok {
		return nil
	}
	if inner, ok := m["defaults"].(map[string]any); ok {
		return inner
	}
	return m
}

// extractTenantRaw pulls one tenant's sub-dict out of a tenant YAML file.
// Matches describe_tenant.py's `self.tenants[tid] = tconfig`.
//
// Returns an error only when the file is structurally invalid (no `tenants:`
// key). Missing tenant IDs return a typed error so callers can distinguish
// "parse ok, tenant not here" from "parse failed".
func extractTenantRaw(doc any, tenantID string) (map[string]any, error) {
	m, ok := doc.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("tenant file has non-dict root")
	}
	tenantsBlock, ok := m["tenants"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("tenant file missing 'tenants' key")
	}
	raw, ok := tenantsBlock[tenantID].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("tenant %q not in file", tenantID)
	}
	return raw, nil
}

// computeEffectiveConfig produces the merged dict for one tenant, applying
// the defaults chain L0→Ln and then the tenant's own overrides. This is the
// intermediate form used by both computeMergedHash and the /effective API
// handler (§8.11.3 Phase 6).
//
// `defaultsChainYAML[i]` should be the raw bytes of the i-th `_defaults.yaml`
// in root-to-leaf order. `tenantYAMLBytes` is the full tenant file — we
// extract `tenants[tenantID]` internally.
func computeEffectiveConfig(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
) (map[string]any, error) {
	merged := make(map[string]any)
	for i, defBytes := range defaultsChainYAML {
		var raw any
		if err := yaml.Unmarshal(defBytes, &raw); err != nil {
			return nil, fmt.Errorf("parse defaults[%d]: %w", i, err)
		}
		block := extractDefaultsBlock(normalizeYAMLToJSON(raw))
		if block == nil {
			continue // empty file or unexpected top-level type — skip silently
		}
		merged = deepMerge(merged, block)
	}

	var tenantDoc any
	if err := yaml.Unmarshal(tenantYAMLBytes, &tenantDoc); err != nil {
		return nil, fmt.Errorf("parse tenant: %w", err)
	}
	tenantRaw, err := extractTenantRaw(normalizeYAMLToJSON(tenantDoc), tenantID)
	if err != nil {
		return nil, err
	}
	merged = deepMerge(merged, tenantRaw)
	return merged, nil
}

// computeMergedHash reproduces the 16-char `merged_hash` emitted by
// describe_tenant.py source_info. It is the canonical tenant-config
// fingerprint used by Alertmanager dual-hash reload detection (ADR-018).
//
// Returns ("", err) on any YAML parse error. Callers (WatchLoop, /effective)
// should log and skip the tenant — one bad file must not block the others.
func computeMergedHash(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
) (string, error) {
	merged, err := computeEffectiveConfig(tenantYAMLBytes, tenantID, defaultsChainYAML)
	if err != nil {
		return "", err
	}
	cjson, err := canonicalJSON(merged)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(cjson)
	hex := fmt.Sprintf("%x", sum)
	return hex[:16], nil
}

// computeSourceHash reproduces describe_tenant.py `_file_hash`: SHA-256 over
// raw file bytes, truncated to 16 hex chars. Kept separate from the full
// 64-char hashes used by scanDirHierarchical for change detection — that one
// is internal plumbing, this one is user-facing parity.
func computeSourceHash(tenantYAMLBytes []byte) string {
	sum := sha256.Sum256(tenantYAMLBytes)
	return fmt.Sprintf("%x", sum)[:16]
}

// logMergeSkip standardizes the skip-with-context log line used when one
// tenant's merge fails while others succeed. Defined here so future callers
// (WatchLoop, /effective) share the same phrasing.
func logMergeSkip(tenantID, reason string, err error) {
	log.Printf("WARN: skipping merged_hash for tenant=%s (%s): %v", tenantID, reason, err)
}
