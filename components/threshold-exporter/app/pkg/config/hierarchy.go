package config

// ============================================================
// Hierarchical config resolver (ADR-016 + ADR-017) — public API
// ============================================================
//
// This file provides a standalone hierarchy resolver that does not depend on
// the exporter's `main` package ConfigManager. It is imported by tenant-api
// (components/tenant-api) for the GET /tenants/{id}/effective endpoint
// introduced in v2.7.0 (§8.11.3 Phase 6).
//
// Not a second implementation. The WALK is the exporter's own: ResolveEffective
// runs one ScanDirTree (tree_scan.go, the one conf.d walker the exporter's
// ConfigManager also uses) and reads the tenant's file, the defaults set and
// every file's bytes off that scan (W2, #1677). The MERGE core below is the
// one the exporter calls too — app/config_inheritance.go is a set of thin
// wrappers over DeepMerge / ComputeMergedHash in this file. And since #1674
// the defaults CHAIN is the exporter's too: one carrier per directory from
// TreeScan.DefaultsCarriers, the selection its inheritance graph is built
// from. The golden-fixture tests pin the 16-char merged_hash against
// describe_tenant.py.
//
// Semantic rules enforced (MUST match describe_tenant.py + app/):
//   - _metadata is never inherited
//   - YAML null / ~ in override deletes an inherited RESERVED (`_`-prefixed)
//     key; on a threshold key it is a no-op, matching the emitting path
//     (see deepMerge for why the two differ — #1339)
//   - map × map → recursive deep merge
//   - array / scalar → override wins wholesale (no concat)
//   - hash = SHA-256(canonical_json(merged))[:16]
//   - canonical JSON = sort_keys + no-space + no-HTML-escape + no-trailing-newline
//   - defaults chain is L0→Ln (root first, leaf last)
//
// Limit of scope: this resolver is *read-only* and *stateless*. Each call
// runs one fresh ScanDirTree (no prior, so every file is read and hashed) —
// fine for an API endpoint that serves a handful of tenants per second. The exporter's ConfigManager still owns the hot-reload
// cache for Prometheus /metrics scrapes.

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
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

// ResolveEffective locates the tenant file that defines `tenantID` in ONE
// ScanDirTree walk of `configDir` (the exporter's own walker), collects the
// _defaults.yaml chain from root down to the tenant's directory, and returns
// the merged config plus dual hashes.
//
// Returns (nil, ErrTenantNotFound) when the tenant isn't present — callers
// should translate to 404. A tenant declared in two files returns its own
// *DuplicateTenantError; a duplicate that does not involve `tenantID` does
// not fail this call. All other errors (bad YAML, a tenant body that is not
// a mapping, missing root, etc.) are returned as-is for 500-class handling.
//
// The paths in DefaultsChain and SourceFile are relative to the RESOLVED
// root (ScanDirTree symlink-resolves `configDir`, so a symlinked
// `--config-dir` is served rather than 404'd) so the JSON response doesn't
// leak container paths like `/conf.d/...`.
//
// ⚠️ An unreadable file is logged-and-dropped by the walker (and the log is
// discarded here), so an unreadable tenant file reads as "not found" and an
// unreadable _defaults.yaml drops out of the chain. Before W2 (#1677) an
// unreadable defaults file was a hard error on this path.
//
// ⚠️ A COLD scan per call (prior nil): every file is read and, since #1957,
// every tenant file decoded in full — the unified parse that makes a tenant
// the exporter rejects 404 here too. Reusing a prior scan across requests
// would put this back on the mtime fast-path; that is #1977, not done here.
func ResolveEffective(configDir, tenantID string) (*EffectiveConfig, error) {
	scan, err := ScanDirTree(configDir, nil, nil, discardLogger)
	if err != nil {
		return nil, err
	}
	return newEffectiveResolver(scan).resolve(tenantID)
}

// discardLogger silences the walker for the library readers. ⛔ Deliberate:
// ResolveEffective runs once per tenant-api request and ScopeEffective once
// per da-guard run; the walker's WARN lines (unparseable file, unreadable
// entry) would repeat per request into the API's log. Both readers were
// silent before they consumed the walker, and they stay so. log.Logger is
// safe for concurrent use and io.Discard holds no state.
var discardLogger = log.New(io.Discard, "", 0)

// effectiveResolver answers per-tenant effective configs from ONE scan. It
// is built once per ResolveEffective call and once per ScopeEffective call
// (which is what makes a scope one walk instead of a re-walk per tenant).
type effectiveResolver struct {
	scan          *TreeScan
	defaultsByDir map[string]string // dir → the one carrier TreeScan.DefaultsCarriers picks there
}

// newEffectiveResolver reads the chain rule off the scan — the SAME
// selection the exporter's inheritance graph is built from (#1674). Until
// then this file kept its own case-folding rule (legacyDefaultsByDir) while
// the graph matched only the exact lower-case names, so `_DEFAULTS.YAML`
// entered /effective's chain and not the exporter's. The rule that function
// implemented is the one SelectDefaultsCarriers now carries for everyone.
func newEffectiveResolver(scan *TreeScan) *effectiveResolver {
	return &effectiveResolver{scan: scan, defaultsByDir: scan.DefaultsCarriers().ByDir}
}

// chain returns the defaults files from the scan root (L0) down to leafDir.
func (r *effectiveResolver) chain(leafDir string) []string {
	return chainFromCarriers(leafDir, r.scan.AbsRoot, r.defaultsByDir, nativePathOps)
}

// bytesOf returns the bytes the scan read for absPath. A prior-less scan
// caches every file it kept, so this is the snapshot the walk hashed — no
// second os.ReadFile, and the tenant file and its defaults come from one
// read of the tree.
func (r *effectiveResolver) bytesOf(absPath string) ([]byte, error) {
	rel, err := filepath.Rel(r.scan.AbsRoot, absPath)
	if err != nil {
		return nil, fmt.Errorf("%q is not under %q: %w", absPath, r.scan.AbsRoot, err)
	}
	f, ok := r.scan.Files[filepath.ToSlash(rel)]
	if !ok || f.Data == nil {
		// Unreachable for a prior-less scan; loud rather than an empty merge.
		return nil, fmt.Errorf("read %q: no bytes cached by the scan", absPath)
	}
	return f.Data, nil
}

// rel renders absPath relative to the resolved scan root, slash-separated.
func (r *effectiveResolver) rel(absPath string) string {
	if rp, err := filepath.Rel(r.scan.AbsRoot, absPath); err == nil {
		return filepath.ToSlash(rp)
	}
	return filepath.ToSlash(absPath)
}

func (r *effectiveResolver) resolve(tenantID string) (*EffectiveConfig, error) {
	tenantFile, err := r.scan.Locate(tenantID)
	if err != nil {
		return nil, err
	}
	tenantBytes, err := r.bytesOf(tenantFile)
	if err != nil {
		return nil, err
	}

	chain := r.chain(filepath.Dir(tenantFile))
	defaultsYAML := make([][]byte, 0, len(chain))
	for _, p := range chain {
		b, berr := r.bytesOf(p)
		if berr != nil {
			return nil, fmt.Errorf("read defaults %q: %w", p, berr)
		}
		defaultsYAML = append(defaultsYAML, b)
	}

	merged, mergedDefaults, tenantRaw, err := computeEffectiveConfigBytesDetailed(tenantBytes, tenantID, defaultsYAML)
	if err != nil {
		return nil, err
	}

	cjson, err := canonicalJSON(merged)
	if err != nil {
		return nil, err
	}
	mergedSum := sha256.Sum256(cjson)
	sourceSum := sha256.Sum256(tenantBytes)

	relChain := make([]string, len(chain))
	for i, p := range chain {
		relChain[i] = r.rel(p)
	}

	return &EffectiveConfig{
		TenantID:           tenantID,
		SourceFile:         r.rel(tenantFile),
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
//     by extractTenantRaw and never mutated past this point.
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
		block := extractDefaultsBlock(normalizeYAMLToJSON(raw))
		if block == nil {
			continue
		}
		merged = deepMerge(merged, block)
	}

	// Snapshot the merged-defaults state BEFORE the tenant override
	// is applied. deepCopyMap defends against shared sub-maps that
	// the subsequent deepMerge could mutate via aliasing.
	mergedDefaults = deepCopyMap(merged)

	var tenantDoc any
	if err := yaml.Unmarshal(tenantYAMLBytes, &tenantDoc); err != nil {
		return nil, nil, nil, fmt.Errorf("parse tenant: %w", err)
	}
	tenantRaw, err = extractTenantRaw(normalizeYAMLToJSON(tenantDoc), tenantID)
	if err != nil {
		return nil, nil, nil, err
	}
	merged = deepMerge(merged, tenantRaw)
	return merged, mergedDefaults, tenantRaw, nil
}

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
			// ADR-017's explicit-null opt-out applies to RESERVED
			// (`_`-prefixed) keys only.
			//
			// It must not apply to a threshold key, because the emitting
			// path does not honour it: collector.go →
			// ThresholdConfig.ResolveAtWithStats decodes a YAML null into
			// ScheduledValue.Default == "", logs `unknown value ""...
			// using default` and falls back to the PLATFORM DEFAULT. The
			// inherited value is still being emitted. A diagnostic path
			// that deleted the key here would tell the operator the
			// threshold is gone while /metrics still carries it — and
			// /effective is an endpoint customers call precisely when they
			// are asking "why is this alert still firing?" (#1339 P0).
			//
			// Threshold keys are exactly the non-`_` keys: in
			// tenant-config.schema.json the tenant body has `_`-prefixed
			// named properties, dimensional patternProperties, and
			// additionalProperties = thresholdScalar | scheduledValue.
			// So "not reserved" ⇒ "is a threshold".
			//
			// To stop alerting on a threshold key use "disable"; null is
			// indistinguishable from a half-typed line, which is why the
			// schema rejects it there.
			if strings.HasPrefix(k, "_") {
				delete(result, k)
			}
			continue
		}
		if overrideMap, ok := v.(map[string]any); ok {
			if baseMap, ok2 := result[k].(map[string]any); ok2 {
				result[k] = deepMerge(baseMap, overrideMap)
				continue
			}
		}
		result[k] = deepCopyValue(v)
	}
	return result
}

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
		return v
	}
}

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

func extractTenantRaw(doc any, tenantID string) (map[string]any, error) {
	m, ok := doc.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("tenant file has non-dict root")
	}
	tenantsBlock, ok := m["tenants"].(map[string]any)
	if !ok {
		return nil, fmt.Errorf("tenant file missing 'tenants' key")
	}
	val, present := tenantsBlock[tenantID]
	if present && val == nil {
		// `tenants:\n  t1:\n` — a tenant declared with a null (empty) body.
		// The walker counts it and the exporter's flat plane serves it with
		// the inherited defaults, so the merge must too: an empty override,
		// not "not in file" (#1677 F2). Before this, /effective 500'd on it,
		// da-guard failed the whole scope, and the exporter's own merged hash
		// for the tenant was skipped. describe_tenant.py maps the same body
		// to `{}` at ingest. A non-mapping, non-null body (`t1: 5`) still
		// errors below.
		return map[string]any{}, nil
	}
	raw, ok := val.(map[string]any)
	if !ok {
		return nil, fmt.Errorf("tenant %q not in file", tenantID)
	}
	return raw, nil
}

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

// ============================================================
// Public re-exports — v2.8.0 PR-4 collapsed app/config_inheritance.go
// into thin wrappers calling these. Same semantic contract as the
// private impls above; the wrappers keep `package main` callers from
// having to switch every call site to a config-qualified name.
// ============================================================

// DeepMerge implements ADR-017 inheritance semantics. See `deepMerge`
// for the rule list.
func DeepMerge(base, override map[string]any) map[string]any {
	return deepMerge(base, override)
}

// NormalizeYAMLToJSON walks a yaml.v3-decoded `any` tree and rewrites
// `map[any]any` (yaml.v3 default for mappings) into `map[string]any`
// so encoding/json can marshal it. Slice elements + scalar leaves are
// passed through untouched.
func NormalizeYAMLToJSON(v any) any { return normalizeYAMLToJSON(v) }

// ExtractDefaultsBlock returns the `defaults:` sub-tree from a parsed
// `_defaults.yaml`-shaped doc; nil for empty / unexpected shape.
func ExtractDefaultsBlock(doc any) map[string]any { return extractDefaultsBlock(doc) }

// ExtractTenantRaw returns `doc.tenants[tenantID]` from a parsed
// `<tenant>.yaml`-shaped doc, or an error if the document doesn't
// have the expected `tenants:` wrapper.
func ExtractTenantRaw(doc any, tenantID string) (map[string]any, error) {
	return extractTenantRaw(doc, tenantID)
}

// CanonicalJSON encodes data with sort_keys + no-space + no-HTML-escape
// + no-trailing-newline, matching describe_tenant.py's _canonical_hash.
// MUST stay byte-equal across Go and Python implementations — golden
// fixtures in app/config_golden_parity_test.go pin the contract.
func CanonicalJSON(data any) ([]byte, error) { return canonicalJSON(data) }

// ComputeEffectiveConfig is the byte-input version of
// computeEffectiveConfigBytes — public-facing alias for the simulate
// primitive (app/handler_simulate.go) and the inheritance.go wrappers.
func ComputeEffectiveConfig(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
) (map[string]any, error) {
	return computeEffectiveConfigBytes(tenantYAMLBytes, tenantID, defaultsChainYAML)
}

// ComputeMergedHash returns the 16-char `merged_hash` user-facing
// fingerprint. SHA-256 over CanonicalJSON of ComputeEffectiveConfig's
// output, truncated to 16 hex chars. Parity with describe_tenant.py
// pinned by the golden fixtures under tests/golden/.
func ComputeMergedHash(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
) (string, error) {
	merged, err := ComputeEffectiveConfig(tenantYAMLBytes, tenantID, defaultsChainYAML)
	if err != nil {
		return "", err
	}
	cjson, err := canonicalJSON(merged)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(cjson)
	return fmt.Sprintf("%x", sum)[:16], nil
}

// ComputeSourceHash reproduces describe_tenant.py `_file_hash`: SHA-256
// over raw file bytes, truncated to 16 hex chars.
func ComputeSourceHash(tenantYAMLBytes []byte) string {
	sum := sha256.Sum256(tenantYAMLBytes)
	return fmt.Sprintf("%x", sum)[:16]
}
