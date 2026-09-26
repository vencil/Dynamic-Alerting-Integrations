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
// See Issue #61 (RFC) and ADR-017 (defaults inheritance + dual-hash).

import (
	"path/filepath"
	"reflect"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/config"
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
//
// PR #69 follow-up (Issue #61): K8s ConfigMap mounts surface symlink
// shims like `..data` and `..2026_04_25_...` at the mount root. Under
// git-sync (the production scanner path) these never appear on the
// scanned path, but we filter `..`-prefixed segments defensively so a
// CM-mounted scan doesn't downgrade a real `global` defaults change to
// `domain` just because Walk saw the symlink dir. Production-named
// dirs never start with `..`.
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
	// Drop K8s ConfigMap symlink artifacts (`..data` / `..2026_*`) before
	// counting depth. A segment starting with `..` is never a legitimate
	// hierarchy directory in our schema.
	depth := 0
	for _, seg := range strings.Split(rel, "/") {
		if seg == "" || strings.HasPrefix(seg, "..") {
			continue
		}
		depth++
	}
	switch depth {
	case 0:
		return "global"
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
	return widestPathScope(hashChangedChainPaths(chain, hashes, priorHashes), root)
}

// hashChangedChainPaths returns the entries of `chain` whose hash moved
// between the prior and current scan, in chain order.
func hashChangedChainPaths(chain []string, hashes, priorHashes map[string]string) []string {
	var changed []string
	for _, p := range chain {
		if hashes[p] != priorHashes[p] {
			changed = append(changed, p)
		}
	}
	return changed
}

// chainMembershipDelta returns the paths that left (in prev, not next) and
// joined (in next, not prev) a tenant's defaults chain between two scans
// (#1964), each in chain order.
func chainMembershipDelta(prev, next []string) (removed, added []string) {
	inPrev := make(map[string]bool, len(prev))
	for _, p := range prev {
		inPrev[p] = true
	}
	inNext := make(map[string]bool, len(next))
	for _, p := range next {
		inNext[p] = true
	}
	for _, p := range prev {
		if !inNext[p] {
			removed = append(removed, p)
		}
	}
	for _, p := range next {
		if !inPrev[p] {
			added = append(added, p)
		}
	}
	return removed, added
}

// widestPathScope returns the widest (smallest scopeRank) level among the
// given defaults paths, or "" when paths is empty.
func widestPathScope(paths []string, root string) string {
	widest := ""
	widestRank := 999
	for _, p := range paths {
		lvl := defaultsPathLevel(p, root)
		if r, ok := scopeRank[lvl]; ok && r < widestRank {
			widestRank = r
			widest = lvl
		}
	}
	return widest
}

// parseDefaultsBytes forwards to config.ParseDefaultsBytes
// (pkg/config/defaults_parse.go, moved there in #1988).
func parseDefaultsBytes(b []byte) (map[string]any, error) { return config.ParseDefaultsBytes(b) }

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
// overridden", which leaves the defaults value visible — so a defaults
// change at that path is NOT shadowed.
//
// The old comment justified this with "deepMerge treats null as delete
// key (ADR-017 trap #6)", which was backwards: deleting would have
// removed the value, not left the default visible. #1339 narrowed the
// delete to reserved (`_`-prefixed) keys, so on a threshold key null
// really is "no override" and this behaviour is now correct for the
// stated reason rather than in spite of it.
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
//     defaults file in the tenant's chain whose file hash moved, plus
//     (#1964) every key set by a file in `removed` (left the chain) or
//     `added` (joined it); a same-directory removal+addition (carrier
//     switch) is diffed pairwise instead. nil removed/added = no
//     membership change.
//  2. If no key actually changed → cosmetic (comment-only / reorder /
//     whitespace edit; common during operator formatter runs).
//  3. Else parse the tenant's source YAML overrides; if every changed
//     key is covered by an override (or a parent-subtree replacement)
//     → shadowed.
//  4. Else fall back to cosmetic (defensive — logically unreachable
//     because merged_hash *would* have moved, but parse failures or
//     edge cases land here rather than in a bogus "applied" bucket).
//
// `overlayKeys` (#2019) are the top-level keys whose value in the
// tenant's root-platform-file entries (platformOverlayDelta) changed this
// tick. They are TOP-LEVEL keys, not dot paths — a tenant key replaces a
// platform key wholesale (config.overlayTenant) — so each is shadowed iff
// the tenant file writes that key at all; they join step 2's "anything
// changed" test and step 3's "every change is shadowed" test. `overlay`
// is the tenant's current platform entries: a CHAIN key they set shadows
// the chain change just as a tenant-file key does.
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
	removed, added []string,
	overlayKeys []string,
	overlay []config.PlatformBlock,
) string {
	var allChanged []string
	// #1964: a file that joined the chain contributes its whole content
	// (diffed against an empty map below), not its diff against whatever
	// the same path held before it was selected — skip it here.
	joined := make(map[string]bool, len(added))
	for _, dp := range added {
		joined[dp] = true
	}
	for _, dp := range defaultsChain {
		if joined[dp] || hashes[dp] == priorHashes[dp] {
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
	// #1964: chain-membership changes. A removal and an addition in the
	// same directory are a carrier switch (a co-located `.yaml`/`.yml`
	// pair; the chain holds at most one carrier per directory, so the
	// pairing is one-to-one) and are diffed against each other — an
	// identical-content switch changes no key. An unpaired file that left
	// the chain withdraws every key it set (prior parse vs empty); an
	// unpaired file that joined applies every key it sets (empty vs new
	// parse). A missing parse on either side is skipped, the same cosmetic
	// fallback as above.
	addedByDir := make(map[string]string, len(added))
	for _, dp := range added {
		addedByDir[filepath.Dir(dp)] = dp
	}
	empty := map[string]any{}
	for _, dp := range removed {
		prev := priorParsed[dp]
		if partner, ok := addedByDir[filepath.Dir(dp)]; ok {
			delete(addedByDir, filepath.Dir(dp))
			if next := newParsed[partner]; prev != nil && next != nil {
				allChanged = append(allChanged, changedDefaultsKeys(prev, next)...)
			}
			continue
		}
		if prev != nil {
			allChanged = append(allChanged, changedDefaultsKeys(prev, empty)...)
		}
	}
	for _, dp := range added {
		if _, unpaired := addedByDir[filepath.Dir(dp)]; !unpaired {
			continue
		}
		if next := newParsed[dp]; next != nil {
			allChanged = append(allChanged, changedDefaultsKeys(empty, next)...)
		}
	}
	if len(allChanged) == 0 && len(overlayKeys) == 0 {
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
	// #2019: a chain change is shadowed by whatever the tenant's own layer
	// sets over the chain — its file AND its root platform entries
	// (`overlay`, merged exactly as the merge does). A platform change
	// (overlayKeys) can only be shadowed by the tenant FILE, below.
	if !tenantOverridesAll(config.ApplyPlatformOverlay(overrides, overlay), allChanged) {
		return "cosmetic"
	}
	for _, k := range overlayKeys {
		if _, written := overrides[k]; !written {
			return "cosmetic"
		}
	}
	return "shadowed"
}
