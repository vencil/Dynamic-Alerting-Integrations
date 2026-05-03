package main

// Lowercase wrappers around `pkg/config` inheritance helpers + app-only
// `logMergeSkip`.
//
// v2.8.0 PR-4 collapsed the parallel `app/config_inheritance.go` ↔
// `pkg/config/hierarchy.go` trees. The canonical impls live in
// `pkg/config` (with capital exports for library consumers like
// `cmd/da-guard` and `tenant-api`).
//
// These wrappers exist so `package main` files (config.go,
// config_debounce.go, config_simulate.go, config_defaults_diff.go)
// keep compiling without renaming every `deepMerge(...)` /
// `computeEffectiveConfig(...)` call site. Behavior pin: every wrapper
// is `return config.X(args...)`, no extra logic.
//
// The 8 semantic traps from §8.11.2 are enforced inside `pkg/config`
// (deepMerge / computeEffectiveConfig / computeMergedHash). Golden
// fixtures in config_golden_parity_test.go pin the 16-char merged_hash
// output across Go and Python implementations.

import (
	"log"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// deepMerge implements ADR-018 inheritance semantics — see
// pkg/config.DeepMerge for the rule list.
func deepMerge(base, override map[string]any) map[string]any {
	return config.DeepMerge(base, override)
}

// deepCopyMap clones a map and all nested maps/slices.
func deepCopyMap(m map[string]any) map[string]any {
	return config.DeepCopyMap(m)
}

// normalizeYAMLToJSON rewrites yaml.v3's map[any]any into
// map[string]any so encoding/json can marshal it.
func normalizeYAMLToJSON(v any) any {
	return config.NormalizeYAMLToJSON(v)
}

// extractDefaultsBlock returns the `defaults:` sub-tree, or nil.
func extractDefaultsBlock(doc any) map[string]any {
	return config.ExtractDefaultsBlock(doc)
}

// extractTenantRaw returns `doc.tenants[tenantID]`, or an error.
func extractTenantRaw(doc any, tenantID string) (map[string]any, error) {
	return config.ExtractTenantRaw(doc, tenantID)
}

// canonicalJSON: sort_keys + no-space + no-HTML-escape +
// no-trailing-newline. Parity-pinned by golden fixtures.
func canonicalJSON(data any) ([]byte, error) {
	return config.CanonicalJSON(data)
}

// computeEffectiveConfig builds the merged dict over a defaults chain
// + tenant override. Used by simulate + recomputeMergedHash.
func computeEffectiveConfig(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
) (map[string]any, error) {
	return config.ComputeEffectiveConfig(tenantYAMLBytes, tenantID, defaultsChainYAML)
}

// computeMergedHash returns the 16-char tenant-config fingerprint.
func computeMergedHash(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
) (string, error) {
	return config.ComputeMergedHash(tenantYAMLBytes, tenantID, defaultsChainYAML)
}

// computeSourceHash returns the 16-char source-file fingerprint.
func computeSourceHash(tenantYAMLBytes []byte) string {
	return config.ComputeSourceHash(tenantYAMLBytes)
}

// logMergeSkip — APP-ONLY. Standardizes the skip-with-context log line
// used when one tenant's merge fails while others succeed. Stays in
// `package main` because the logging convention is exporter-specific
// (the log.Printf format is read by ops dashboards).
func logMergeSkip(tenantID, reason string, err error) {
	log.Printf("WARN: skipping merged_hash for tenant=%s (%s): %v", tenantID, reason, err)
}
