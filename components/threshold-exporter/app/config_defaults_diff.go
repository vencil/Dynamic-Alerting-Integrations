package main

// ============================================================
// Defaults diff helpers — Issue #61 blast-radius effect=shadowed/cosmetic
// ============================================================
//
// These helpers support the three-way effect classification of the
// da_config_blast_radius_tenants_affected histogram:
//
//   effect = applied   → merged_hash actually moved.
//   effect = shadowed  → defaults change blocked by tenant override
//                        (every changed defaults key is overridden by
//                        this tenant's source YAML).
//   effect = cosmetic  → defaults file changed but no semantic key
//                        actually moved (comment/whitespace/reorder),
//                        so no tenant could be impacted.
//
// All helpers operate on the *parsed-and-normalized* form of YAML
// produced by extractDefaultsBlock(normalizeYAMLToJSON(...)), so the
// shape is always map[string]any with no map[any]any survivors. The
// integration site (config_debounce.go::diffAndReload) is responsible
// for invoking yaml.Unmarshal + that normalization pipe before calling
// changedDefaultsKeys/tenantOverridesAll.
//
// See Issue #61 (RFC) and ADR-018 (defaults inheritance + dual-hash).

import (
	"path/filepath"
	"reflect"
	"strings"

	"gopkg.in/yaml.v3"
)

// scopeRank lets us pick the *widest* (= shallowest, smallest rank)
// scope when multiple defaults files in a tenant's chain change in a
// single tick. RFC #61 §"Scope tie-breaker": widest wins, because a
// global change is operationally scarier than a per-env change and
// that's what blast-radius alerting wants to surface.
var scopeRank = map[string]int{
	"global":  0,
	"domain":  1,
	"region":  2,
	"env":     3,
	"unknown": 4,
}

// defaultsPathLevel classifies a _defaults.yaml absolute path by its
// directory depth relative to the conf.d scan root. The convention
// (matching docs/design/config-driven.md):
//
//	<root>/_defaults.yaml                              → "global"  (depth 0)
//	<root>/<domain>/_defaults.yaml                     → "domain"  (depth 1)
//	<root>/<domain>/<region>/_defaults.yaml            → "region"  (depth 2)
//	<root>/<domain>/<region>/<env>/_defaults.yaml      → "env"     (depth 3)
//	deeper or non-descendant of root                   → "unknown"
//
// Path *segment names* (e.g. literal "domain") are deliberately not
// inspected — production configs use real names like "finance" /
// "us-east" / "prod", and depth from root is the only reliable signal.
func defaultsPathLevel(path, root string) string {
	rel, err := filepath.Rel(root, filepath.Dir(path))
	if err != nil {
		return "unknown"
	}
	rel = filepath.ToSlash(rel)
	// filepath.Rel returns ".." prefixed when path is not a descendant
	// of root. Treat as unknown — should not happen in production but
	// guards against caller misuse.
	if rel == ".." || strings.HasPrefix(rel, "../") {
		return "unknown"
	}
	if rel == "." {
		return "global"
	}
	depth := strings.Count(rel, "/") + 1
	switch depth {
	case 1:
		return "domain"
	case 2:
		return "region"
	case 3:
		return "env"
	default:
		return "unknown"
	}
}

// widestChangedScope returns the widest (smallest scopeRank) level
// among defaults files in `chain` whose hash moved between prior and
// current scan. Returns "unknown" only if every changed file resolves
// to "unknown"; returns "" if no file in the chain actually changed
// (caller should not enter the defaults-effect branch in that case).
func widestChangedScope(chain []string, hashes, priorHashes map[string]string, root string) string {
	widest := ""
	widestRank := 999
	for _, p := range chain {
		if hashes[p] == priorHashes[p] {
			continue
		}
		lvl := defaultsPathLevel(p, root)
		if r, ok := scopeRank[lvl]; ok && r < widestRank {
			widestRank = r
			widest = lvl
		}
	}
	return widest
}

// parseDefaultsBytes turns raw _defaults.yaml bytes into the same
// shape that extractDefaultsBlock(normalizeYAMLToJSON(...)) produces
// elsewhere in the codebase. Returns nil with no error for an empty
// document (legacy flat configs may have empty/whitespace-only files).
//
// This duplicates the pipeline used in computeEffectiveConfig but
// stops before the merge step — we only need the parsed dict for
// key-level diffing.
func parseDefaultsBytes(b []byte) (map[string]any, error) {
	if len(strings.TrimSpace(string(b))) == 0 {
		return map[string]any{}, nil
	}
	var doc any
	if err := yaml.Unmarshal(b, &doc); err != nil {
		return nil, err
	}
	normalized := normalizeYAMLToJSON(doc)
	block := extractDefaultsBlock(normalized)
	if block == nil {
		return map[string]any{}, nil
	}
	return block, nil
}

// changedDefaultsKeys returns the dot-path keys whose values differ
// between prev and next, recursing into nested maps. Each leaf
// difference (scalar, array, or whole-subtree replacement) is reported
// as one entry. Examples:
//
//	prev: {a: 1, b: 2}            next: {a: 1, b: 3}
//	→ ["b"]
//
//	prev: {t: {cpu: 80, mem: 70}} next: {t: {cpu: 90, mem: 70}}
//	→ ["t.cpu"]
//
//	prev: {x: [1, 2]}             next: {x: [1, 2, 3]}
//	→ ["x"]                       (arrays replace whole)
//
//	prev: {}                      next: {a: 1}
//	→ ["a"]                       (added key)
//
//	prev: {a: 1}                  next: {}
//	→ ["a"]                       (removed key)
//
// Returns an empty slice when prev and next are deeply equal, which is
// the canonical "cosmetic edit" signal (comment-only, reordering,
// whitespace).
func changedDefaultsKeys(prev, next map[string]any) []string {
	var out []string
	collectDefaultsDiff("", prev, next, &out)
	return out
}

func collectDefaultsDiff(prefix string, a, b any, out *[]string) {
	am, aIsMap := a.(map[string]any)
	bm, bIsMap := b.(map[string]any)
	if !aIsMap || !bIsMap {
		// At least one side is not a map — treat as a leaf and compare
		// directly. Covers scalar↔scalar, scalar↔array, scalar↔map,
		// array↔array (we don't try to diff array elements; replace).
		if !reflect.DeepEqual(a, b) {
			*out = append(*out, prefix)
		}
		return
	}
	// Both maps — walk the union of keys so additions and deletions
	// are both surfaced.
	seen := make(map[string]struct{}, len(am)+len(bm))
	for k := range am {
		seen[k] = struct{}{}
	}
	for k := range bm {
		seen[k] = struct{}{}
	}
	for k := range seen {
		path := k
		if prefix != "" {
			path = prefix + "." + k
		}
		collectDefaultsDiff(path, am[k], bm[k], out)
	}
}

// tenantOverridesAll returns true iff every dot-path in `dotPaths` is
// overridden by `tenantSrc`. A path is considered overridden if the
// tenant has set the value at that path OR at any prefix of it (a
// scalar/array at "thresholds" overrides "thresholds.cpu" because the
// whole subtree is replaced by deepMerge semantics).
//
// An empty dotPaths slice returns true vacuously — callers in the
// shadow-detection path only invoke this with non-empty changed-key
// sets, so the vacuous case is unreachable in production but kept
// well-defined for tests.
//
// "Set" means present and not nil; YAML null is treated as "not
// overridden" because deepMerge treats null as "delete key" (ADR-018
// semantic trap #6) which leaves the defaults value visible.
func tenantOverridesAll(tenantSrc map[string]any, dotPaths []string) bool {
	if len(dotPaths) == 0 {
		return true
	}
	for _, p := range dotPaths {
		if !pathOverriddenIn(tenantSrc, strings.Split(p, ".")) {
			return false
		}
	}
	return true
}

func pathOverriddenIn(node any, segs []string) bool {
	if node == nil {
		return false
	}
	if len(segs) == 0 {
		// Reached the leaf: presence (non-nil) counts as override.
		return true
	}
	m, ok := node.(map[string]any)
	if !ok {
		// Non-map node at intermediate depth: tenant has replaced the
		// whole subtree with a scalar/array → all descendants are
		// overridden.
		return true
	}
	val, exists := m[segs[0]]
	if !exists {
		return false
	}
	return pathOverriddenIn(val, segs[1:])
}

// classifyDefaultsNoOpEffect distinguishes "shadowed" from "cosmetic"
// for a tenant whose merged_hash did NOT move despite a defaults-chain
// hash change. Called from diffAndReload's noOp branch to set the
// effect label of the blast-radius observation. See Issue #61.
//
// Logic:
//
//  1. Aggregate dot-path keys that actually changed across every
//     defaults file in the tenant's chain whose file hash moved.
//  2. If no key actually changed → cosmetic (comment-only / reorder /
//     whitespace edit; common during operator formatter runs).
//  3. Else parse the tenant's source YAML overrides; if every changed
//     key is covered by an override (or a parent-subtree replacement)
//     → shadowed.
//  4. Else fall back to cosmetic (defensive — logically unreachable
//     because merged_hash *would* have moved, but parse failures or
//     edge cases land here rather than in a bogus "applied" bucket).
//
// All disk-I/O is deliberately scoped to this rare path (tenants in
// the noOp set are by definition the "quiet defaults edit" minority).
// On parse failure of the tenant file we return "cosmetic" with a
// log line — same policy as logMergeSkip elsewhere in the package.
func classifyDefaultsNoOpEffect(
	tenantBytes []byte,
	tid string,
	defaultsChain []string,
	priorParsed, newParsed map[string]map[string]any,
	hashes, priorHashes map[string]string,
) string {
	var allChanged []string
	for _, dp := range defaultsChain {
		if hashes[dp] == priorHashes[dp] {
			continue
		}
		prev := priorParsed[dp]
		next := newParsed[dp]
		// If either side is unparseable (cache miss / parse error),
		// fall back to "cosmetic" — we can't claim shadow without
		// evidence, and the alternative ("applied") would be wrong
		// because merged_hash didn't move.
		if prev == nil || next == nil {
			continue
		}
		allChanged = append(allChanged, changedDefaultsKeys(prev, next)...)
	}
	if len(allChanged) == 0 {
		return "cosmetic"
	}
	var doc any
	if err := yaml.Unmarshal(tenantBytes, &doc); err != nil {
		return "cosmetic"
	}
	overrides, err := extractTenantRaw(normalizeYAMLToJSON(doc), tid)
	if err != nil {
		return "cosmetic"
	}
	if tenantOverridesAll(overrides, allChanged) {
		return "shadowed"
	}
	return "cosmetic"
}
