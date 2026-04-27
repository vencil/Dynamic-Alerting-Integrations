package config

// ============================================================
// Hierarchical config resolver (ADR-017 + ADR-018) — public API
// ============================================================
//
// This file provides a standalone hierarchy resolver that does not depend on
// the exporter's `main` package ConfigManager. It is imported by tenant-api
// (components/tenant-api) for the GET /tenants/{id}/effective endpoint
// introduced in v2.7.0 (§8.11.3 Phase 6).
//
// Why a second implementation? The exporter's `app/config_hierarchy.go` +
// `app/config_inheritance.go` live in `package main` so they can share state
// with ConfigManager. Cross-module borrowing would require either (a) a big
// ConfigManager → pkg/config refactor, or (b) a thin standalone resolver
// following the same parity rules as describe_tenant.py. We picked (b): the
// golden-fixture tests pin the 16-char merged_hash output, so any semantic
// drift between the two Go call sites is caught by CI.
//
// Semantic rules enforced (MUST match describe_tenant.py + app/):
//   - _metadata is never inherited
//   - YAML null / ~ in override deletes inherited key
//   - map × map → recursive deep merge
//   - array / scalar → override wins wholesale (no concat)
//   - hash = SHA-256(canonical_json(merged))[:16]
//   - canonical JSON = sort_keys + no-space + no-HTML-escape + no-trailing-newline
//   - defaults chain is L0→Ln (root first, leaf last)
//
// Limit of scope: this resolver is *read-only* and *stateless*. Each call
// re-walks the directory — fine for an API endpoint that serves a handful of
// tenants per second. The exporter's ConfigManager still owns the hot-reload
// cache for Prometheus /metrics scrapes.

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"

	"gopkg.in/yaml.v3"
)

// ErrTenantNotFound is returned by ResolveEffective when the tenant ID is not
// present anywhere in the config tree. Callers translate this to HTTP 404.
var ErrTenantNotFound = errors.New("tenant not found in config tree")

// EffectiveConfig is the merged tenant view returned by the resolver and the
// /effective handler. JSON-serialized fields match describe_tenant.py (16 hex
// chars for hashes); the trailing `json:"-"` fields are populated for the
// guard caller (v2.8.0 PR-5 redundant-override warn-tier) but stay out of the
// HTTP response so tenant-api's /effective contract doesn't grow surface for
// downstream consumers that don't need it.
type EffectiveConfig struct {
	TenantID        string         `json:"tenant_id"`
	SourceFile      string         `json:"source_file"`
	SourceHash      string         `json:"source_hash"`
	MergedHash      string         `json:"merged_hash"`
	DefaultsChain   []string       `json:"defaults_chain"`
	EffectiveConfig map[string]any `json:"effective_config"`
	Warnings        []string       `json:"warnings,omitempty"`

	// TenantOverridesRaw is the tenant.yaml override block before any
	// defaults-chain merge. Populated by ResolveEffective so the C-12
	// redundant-override check can compare raw overrides to the
	// inherited defaults at the same path. Not serialized — guard-only.
	TenantOverridesRaw map[string]any `json:"-"`

	// MergedDefaults is the defaults chain merged together for this
	// tenant's directory, BEFORE the tenant override is applied. This
	// is the "what the tenant inherits" view that the guard's
	// redundant-override check needs (different tenants under cascading
	// _defaults.yaml may inherit different merged defaults; see
	// guard.CheckInput.NewDefaultsByTenant). Not serialized — guard-only.
	MergedDefaults map[string]any `json:"-"`
}

// ResolveEffective walks `configDir` looking for the tenant file that defines
// `tenantID`, collects the _defaults.yaml chain from root down to the tenant's
// directory, and returns the merged config plus dual hashes.
//
// Returns (nil, ErrTenantNotFound) when the tenant isn't present — callers
// should translate to 404. All other errors (bad YAML, unreadable file, etc.)
// are returned as-is for 500-class handling.
//
// The paths in DefaultsChain and SourceFile are relative to `configDir` so
// the JSON response doesn't leak container paths like `/conf.d/...`.
func ResolveEffective(configDir, tenantID string) (*EffectiveConfig, error) {
	absRoot, err := filepath.Abs(configDir)
	if err != nil {
		return nil, fmt.Errorf("resolve root %q: %w", configDir, err)
	}
	absRoot = filepath.Clean(absRoot)

	info, err := os.Stat(absRoot)
	if err != nil {
		return nil, fmt.Errorf("stat %q: %w", absRoot, err)
	}
	if !info.IsDir() {
		return nil, fmt.Errorf("%q is not a directory", absRoot)
	}

	// Single pass: find tenant file + collect _defaults.yaml paths.
	defaultsByDir := make(map[string]string) // dir → absolute path of its _defaults.yaml
	var tenantFile string
	var tenantBytes []byte

	walkErr := filepath.WalkDir(absRoot, func(path string, d fs.DirEntry, werr error) error {
		if werr != nil {
			// Tolerate permission errors on individual entries — match the
			// exporter's behavior and Python's rglob semantics.
			return nil
		}
		name := d.Name()
		if d.IsDir() {
			if path != absRoot && strings.HasPrefix(name, ".") {
				return fs.SkipDir
			}
			return nil
		}
		if strings.HasPrefix(name, ".") {
			return nil
		}
		lower := strings.ToLower(name)
		if !strings.HasSuffix(lower, ".yaml") && !strings.HasSuffix(lower, ".yml") {
			return nil
		}
		clean := filepath.Clean(path)

		if strings.HasPrefix(name, "_") {
			// Only _defaults.yaml enters the chain map. .yaml wins over .yml
			// if both exist at the same level.
			if lower == "_defaults.yaml" {
				defaultsByDir[filepath.Dir(clean)] = clean
			} else if lower == "_defaults.yml" {
				// Don't overwrite an existing .yaml entry at the same dir.
				dir := filepath.Dir(clean)
				if _, exists := defaultsByDir[dir]; !exists {
					defaultsByDir[dir] = clean
				}
			}
			return nil
		}

		// Tenant candidate — peek at `tenants:` keys.
		data, rerr := os.ReadFile(clean)
		if rerr != nil {
			return nil
		}
		var doc struct {
			Tenants map[string]yaml.Node `yaml:"tenants"`
		}
		if perr := yaml.Unmarshal(data, &doc); perr != nil {
			return nil
		}
		if _, ok := doc.Tenants[tenantID]; ok {
			if tenantFile != "" && tenantFile != clean {
				// Duplicate definition across files — same error-loud principle
				// as the exporter's scanner. Returning via the walk-closure
				// needs care: filepath.WalkDir will propagate this out.
				return fmt.Errorf(
					"duplicate tenant ID %q: defined in both %s and %s",
					tenantID, tenantFile, clean)
			}
			tenantFile = clean
			tenantBytes = data
		}
		return nil
	})
	if walkErr != nil {
		return nil, walkErr
	}
	if tenantFile == "" {
		return nil, ErrTenantNotFound
	}

	// Build the defaults chain from root down to the tenant file's directory.
	chain := make([]string, 0, 4)
	current := filepath.Dir(tenantFile)
	rootClean := absRoot
	var rev []string
	for {
		if p, ok := defaultsByDir[current]; ok {
			rev = append(rev, p)
		}
		if current == rootClean {
			break
		}
		parent := filepath.Dir(current)
		if parent == current {
			break
		}
		current = parent
	}
	// Reverse so L0 (root) comes first — matches describe_tenant.py line 152.
	for i := len(rev) - 1; i >= 0; i-- {
		chain = append(chain, rev[i])
	}

	// Read defaults bodies in order.
	defaultsYAML := make([][]byte, 0, len(chain))
	for _, p := range chain {
		b, rerr := os.ReadFile(p)
		if rerr != nil {
			return nil, fmt.Errorf("read defaults %q: %w", p, rerr)
		}
		defaultsYAML = append(defaultsYAML, b)
	}

	merged, mergedDefaults, tenantRaw, err := computeEffectiveConfigBytesDetailed(tenantBytes, tenantID, defaultsYAML)
	if err != nil {
		return nil, err
	}

	cjson, err := canonicalJSONBytes(merged)
	if err != nil {
		return nil, err
	}
	mergedSum := sha256.Sum256(cjson)
	sourceSum := sha256.Sum256(tenantBytes)

	// Convert absolute paths to repo-relative for the response body.
	relChain := make([]string, len(chain))
	for i, p := range chain {
		if r, rerr := filepath.Rel(absRoot, p); rerr == nil {
			relChain[i] = filepath.ToSlash(r)
		} else {
			relChain[i] = filepath.ToSlash(p)
		}
	}
	relSource := filepath.ToSlash(tenantFile)
	if r, rerr := filepath.Rel(absRoot, tenantFile); rerr == nil {
		relSource = filepath.ToSlash(r)
	}

	return &EffectiveConfig{
		TenantID:           tenantID,
		SourceFile:         relSource,
		SourceHash:         fmt.Sprintf("%x", sourceSum)[:16],
		MergedHash:         fmt.Sprintf("%x", mergedSum)[:16],
		DefaultsChain:      relChain,
		EffectiveConfig:    merged,
		TenantOverridesRaw: tenantRaw,
		MergedDefaults:     mergedDefaults,
	}, nil
}

// ============================================================
// Internals — duplicated with app/config_inheritance.go. When updating one,
// update the other and the golden fixtures will catch any drift.
// ============================================================

func computeEffectiveConfigBytes(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
) (map[string]any, error) {
	merged, _, _, err := computeEffectiveConfigBytesDetailed(tenantYAMLBytes, tenantID, defaultsChainYAML)
	return merged, err
}

// computeEffectiveConfigBytesDetailed extends the legacy helper with
// the two intermediate maps the C-12 PR-5 redundant-override check
// needs:
//
//   - mergedDefaults: the defaults chain merged together, BEFORE the
//     tenant override is applied. Captured as a deep-copy snapshot so
//     subsequent merging into `merged` doesn't mutate it.
//   - tenantRaw:      the tenant.yaml override block, raw. Returned
//     by extractTenantRawH and never mutated past this point.
//
// Caller-friendly contract: these two maps are *also* what the guard
// library treats as the tuple (NewDefaults, TenantOverrides) per
// tenant. We deliberately return them separately rather than letting
// downstream callers re-derive: re-derivation requires re-running the
// merge engine and would invite drift.
func computeEffectiveConfigBytesDetailed(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
) (merged, mergedDefaults, tenantRaw map[string]any, err error) {
	merged = make(map[string]any)
	for i, defBytes := range defaultsChainYAML {
		var raw any
		if err := yaml.Unmarshal(defBytes, &raw); err != nil {
			return nil, nil, nil, fmt.Errorf("parse defaults[%d]: %w", i, err)
		}
		block := extractDefaultsBlockH(normalizeYAMLToJSONH(raw))
		if block == nil {
			continue
		}
		merged = deepMergeH(merged, block)
	}

	// Snapshot the merged-defaults state BEFORE the tenant override
	// is applied. deepCopyMapH defends against shared sub-maps that
	// the subsequent deepMergeH could mutate via aliasing.
	mergedDefaults = deepCopyMapH(merged)

	var tenantDoc any
	if err := yaml.Unmarshal(tenantYAMLBytes, &tenantDoc); err != nil {
		return nil, nil, nil, fmt.Errorf("parse tenant: %w", err)
	}
	tenantRaw, err = extractTenantRawH(normalizeYAMLToJSONH(tenantDoc), tenantID)
	if err != nil {
		return nil, nil, nil, err
	}
	merged = deepMergeH(merged, tenantRaw)
	return merged, mergedDefaults, tenantRaw, nil
}

func deepMergeH(base, override map[string]any) map[string]any {
	result := deepCopyMapH(base)
	if override == nil {
		return result
	}
	for k, v := range override {
		if k == "_metadata" {
			continue
		}
		if v == nil {
			delete(result, k)
			continue
		}
		if overrideMap, ok := v.(map[string]any); ok {
			if baseMap, ok2 := result[k].(map[string]any); ok2 {
				result[k] = deepMergeH(baseMap, overrideMap)
				continue
			}
		}
		result[k] = deepCopyValueH(v)
	}
	return result
}

func deepCopyMapH(m map[string]any) map[string]any {
	if m == nil {
		return make(map[string]any)
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = deepCopyValueH(v)
	}
	return out
}

func deepCopyValueH(v any) any {
	switch t := v.(type) {
	case map[string]any:
		return deepCopyMapH(t)
	case []any:
		arr := make([]any, len(t))
		for i := range t {
			arr[i] = deepCopyValueH(t[i])
		}
		return arr
	default:
		return v
	}
}

func normalizeYAMLToJSONH(v any) any {
	switch t := v.(type) {
	case map[any]any:
		out := make(map[string]any, len(t))
		for k, val := range t {
			ks, ok := k.(string)
			if !ok {
				ks = fmt.Sprintf("%v", k)
			}
			out[ks] = normalizeYAMLToJSONH(val)
		}
		return out
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, val := range t {
			out[k] = normalizeYAMLToJSONH(val)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i := range t {
			out[i] = normalizeYAMLToJSONH(t[i])
		}
		return out
	default:
		return v
	}
}

func extractDefaultsBlockH(doc any) map[string]any {
	m, ok := doc.(map[string]any)
	if !ok {
		return nil
	}
	if inner, ok := m["defaults"].(map[string]any); ok {
		return inner
	}
	return m
}

func extractTenantRawH(doc any, tenantID string) (map[string]any, error) {
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

func canonicalJSONBytes(data any) ([]byte, error) {
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
