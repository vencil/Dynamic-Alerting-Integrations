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
//   - between the chain and the tenant file: the ROOT platform files'
//     `tenants:` entries for the tenant (#2019, platform_overlay.go), per
//     top-level key, the tenant file winning — the /metrics plane's order
//   - under both: the profile the tenant's layer elects (`_profile`; #2117,
//     profile_overlay.go) fills in the keys neither writes — ApplyProfiles'
//     rule, so /effective and /metrics agree for a tenant on a profile; the
//     exporter's own merged_hash passes the same TenantLayers, so it does
//     too, and a profile edit is a reload of the tenants on that profile
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
	"math"
	"path/filepath"
	"sort"
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
	TenantID      string   `json:"tenant_id"`
	SourceFile    string   `json:"source_file"`
	SourceHash    string   `json:"source_hash"`
	MergedHash    string   `json:"merged_hash"`
	DefaultsChain []string `json:"defaults_chain"`
	// PlatformOverlay names the root platform files whose `tenants:` block
	// supplied values to EffectiveConfig (#2019), in merge order, each with
	// the top-level keys it supplied — only keys the tenant file does not
	// write and no later platform file overwrote. Omitted when that layer
	// contributes nothing. Same name and shape as describe_tenant.py.
	PlatformOverlay []PlatformOverlaySource `json:"platform_overlay,omitempty"`
	// ProfileOverlay names the profile the tenant is on (`_profile`, from
	// the tenant file or the platform overlay) and, per root platform file
	// that profile's filled-in values came from (merge order), the
	// top-level keys it supplied to EffectiveConfig — only keys neither the
	// tenant file nor the platform overlay writes (#2117). Omitted when the
	// profile contributes nothing (no `_profile`, an unknown profile, or
	// every key already set). Same name and shape as describe_tenant.py.
	ProfileOverlay  []ProfileOverlaySource `json:"profile_overlay,omitempty"`
	EffectiveConfig map[string]any         `json:"effective_config"`
	Warnings        []string               `json:"warnings,omitempty"`

	// TenantOverridesRaw is the tenant.yaml override block before any
	// defaults-chain merge. Populated by ResolveEffective so the C-12
	// redundant-override check can compare raw overrides to the
	// inherited defaults at the same path. Not serialized — guard-only.
	TenantOverridesRaw map[string]any `json:"-"`

	// MergedDefaults is the defaults chain merged together for this
	// tenant's directory, BEFORE the tenant override is applied — with the
	// root platform files' values for this tenant (#2019) merged over it,
	// because deleting such a key from the tenant file falls back to THAT
	// value, not to the chain's (a mapping platform value — the schedule
	// form — included, #2191). A key the tenant writes as a mapping is
	// replaced wholesale by the tenant's value, so its leaves fall back to
	// the chain and are left as the chain has them (platformInherited).
	// Under those, the elected profile's values (#2117, PlatformProfiles.inherited): a
	// key deleted from a tenant on a profile falls back to the PROFILE's
	// value on /metrics, and a chain-only view told the tenant to delete an
	// override whose removal changed what /metrics serves. This
	// is the "what the tenant inherits" view that the guard's
	// redundant-override check needs (different tenants under cascading
	// _defaults.yaml may inherit different merged defaults; see
	// guard.CheckInput.NewDefaultsByTenant). Not serialized — guard-only.
	MergedDefaults map[string]any `json:"-"`
}

// ResolveEffective locates the tenant file that defines `tenantID` in ONE
// ScanDirTree walk of `configDir` (the exporter's own walker), collects the
// _defaults.yaml chain from root down to the tenant's directory, and returns
// the merged config plus dual hashes. The root platform files' `tenants:`
// entries for the tenant are merged between the chain and the tenant file
// and reported in PlatformOverlay (#2019).
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

	// platform is every root platform file's `tenants:` block (#2019),
	// decoded on the first resolve and shared by every tenant after it.
	platform     []PlatformTenants
	platformDone bool

	// profiles is the root platform files' `profiles:` set (#2117),
	// decoded on the first resolve and shared by every tenant after it.
	profiles     *PlatformProfiles
	profilesDone bool

	// docs is each tenant file parsed once (ParseTenantDoc) and shared by
	// every tenant it declares that this resolver resolves (#2153): a
	// ScopeEffective over a file declaring T tenants parsed it T times.
	// Keyed by absolute path; the bytes are the scan's own (bytesOf), so
	// one key always names the same bytes. Lives and dies with the
	// resolver — one ResolveEffective / ScopeEffective call.
	docs map[string]*TenantDoc

	// onTenantParse is a test seam, nil in production: called once per
	// tenant-file parse. On this value (one call's resolver), not a
	// package global, so a parallel test observes only its own resolver.
	onTenantParse func(absPath string)
}

// newEffectiveResolver reads the chain rule off the scan — the SAME
// selection the exporter's inheritance graph is built from (#1674). Until
// then this file kept its own case-folding rule (legacyDefaultsByDir) while
// the graph matched only the exact lower-case names, so `_DEFAULTS.YAML`
// entered /effective's chain and not the exporter's. The rule that function
// implemented is the one SelectDefaultsCarriers now carries for everyone.
func newEffectiveResolver(scan *TreeScan) *effectiveResolver {
	return &effectiveResolver{scan: scan, defaultsByDir: scan.DefaultsCarriers().ByDir, docs: make(map[string]*TenantDoc)}
}

// tenantDoc is absPath's bytes parsed once per resolver (#2153).
func (r *effectiveResolver) tenantDoc(absPath string, b []byte) *TenantDoc {
	if d, ok := r.docs[absPath]; ok {
		return d
	}
	d := ParseTenantDoc(b)
	if r.onTenantParse != nil {
		r.onTenantParse(absPath)
	}
	r.docs[absPath] = d
	return d
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

// platformTenants decodes the root platform files' `tenants:` blocks once
// per resolver, from the scan's own bytes (LoadRootPlatformTenants).
func (r *effectiveResolver) platformTenants() []PlatformTenants {
	if !r.platformDone {
		r.platform = LoadRootPlatformTenants(r.scan, nil, func(f *TreeFile) ([]byte, error) {
			return r.bytesOf(f.AbsPath)
		})
		r.platformDone = true
	}
	return r.platform
}

// platformProfiles decodes the root platform files' `profiles:` blocks once
// per resolver, from the scan's own bytes (LoadRootPlatformProfiles).
func (r *effectiveResolver) platformProfiles() *PlatformProfiles {
	if !r.profilesDone {
		r.profiles = LoadRootPlatformProfiles(r.scan, func(f *TreeFile) ([]byte, error) {
			return r.bytesOf(f.AbsPath)
		})
		r.profilesDone = true
	}
	return r.profiles
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

	overlay := PlatformOverlayFor(r.platformTenants(), tenantID)
	parts, err := computeEffectiveConfigDocDetailed(r.tenantDoc(tenantFile, tenantBytes), tenantID, defaultsYAML, overlay, r.platformProfiles())
	if err != nil {
		// #2123: name the file whose bytes the decode rejected, so a caller
		// can tell "this file is broken" from any other resolve failure
		// (errors.As *DecodeError). The text is unchanged.
		var cpe *chainParseError
		var tpe *tenantParseError
		switch {
		case errors.As(err, &cpe) && cpe.index < len(chain):
			return nil, &DecodeError{Path: r.rel(chain[cpe.index]), Err: err}
		case errors.As(err, &tpe):
			return nil, &DecodeError{Path: r.rel(tenantFile), Err: err}
		}
		return nil, err
	}

	cjson, err := canonicalJSON(parts.merged)
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
		PlatformOverlay:    parts.platformSources,
		ProfileOverlay:     parts.profileSources,
		EffectiveConfig:    parts.merged,
		TenantOverridesRaw: parts.tenantRaw,
		MergedDefaults:     parts.mergedDefaults,
	}, nil
}

// ============================================================
// Internals — the only Go implementation. app/config_inheritance.go just
// forwards to these (every wrapper is `return config.X(...)`), so there is
// no second Go copy to keep in step. The drift that can happen is Go vs the
// Python implementation (describe_tenant.py): app/config_golden_parity_test.go
// pins both to the same hashes on its fixtures.
// ============================================================

func computeEffectiveConfigBytes(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
	overlay []PlatformBlock,
) (map[string]any, error) {
	parts, err := computeEffectiveConfigBytesDetailed(tenantYAMLBytes, tenantID, defaultsChainYAML, overlay, nil)
	return parts.merged, err
}

// effectiveParts is computeEffectiveConfigBytesDetailed's result.
type effectiveParts struct {
	merged, mergedDefaults, tenantRaw map[string]any
	platformSources                   []PlatformOverlaySource
	profileSources                    []ProfileOverlaySource
}

// computeEffectiveConfigBytesDetailed extends the legacy helper with
// the two intermediate maps the C-12 PR-5 redundant-override check
// needs:
//
//   - mergedDefaults: the defaults chain merged together, BEFORE the
//     tenant override is applied — with the platform values a deleted
//     tenant key falls back to merged over it (#2019, platformInherited:
//     every platform value except a key the tenant writes as a mapping,
//     #2191). Captured as a copy so
//     subsequent merging into `merged` doesn't mutate it.
//   - tenantRaw:      the tenant.yaml override block, raw. Returned
//     by extractTenantRaw and never mutated past this point.
//
// Caller-friendly contract: these two maps are *also* what the guard
// library treats as the tuple (NewDefaults, TenantOverrides) per
// tenant. We deliberately return them separately rather than letting
// downstream callers re-derive: re-derivation requires re-running the
// merge engine and would invite drift. `platformSources` /
// `profileSources` are the platform overlay's and the profile's
// attribution (EffectiveConfig.PlatformOverlay / ProfileOverlay); nil
// without one. `profiles` nil = no profile expansion.
func computeEffectiveConfigBytesDetailed(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
	overlay []PlatformBlock,
	profiles *PlatformProfiles,
) (effectiveParts, error) {
	return computeEffectiveConfigDocDetailed(ParseTenantDoc(tenantYAMLBytes), tenantID, defaultsChainYAML, overlay, profiles)
}

// computeEffectiveConfigDocDetailed is computeEffectiveConfigBytesDetailed
// over a tenant file already parsed by ParseTenantDoc (#2153) — the one
// implementation: the byte form above only parses and calls it.
func computeEffectiveConfigDocDetailed(
	tenantDoc *TenantDoc,
	tenantID string,
	defaultsChainYAML [][]byte,
	overlay []PlatformBlock,
	profiles *PlatformProfiles,
) (effectiveParts, error) {
	// Parse-and-fold one file at a time (no []ChainDefaults: this is the
	// debounced path's per-tenant call, and a slice per call was +1 alloc
	// per re-merged tenant). A file after a broken one is never parsed.
	var err error
	merged := make(map[string]any)
	for i, defBytes := range defaultsChainYAML {
		if merged, err = foldDefaults(merged, i, ParseChainDefaults(defBytes)); err != nil {
			return effectiveParts{}, err
		}
	}

	chain := merged
	p, err := mergeTenantOver(chain, tenantDoc, tenantID, overlay, profiles)
	if err != nil {
		return effectiveParts{}, err
	}

	// The merged-defaults state BEFORE the tenant override: `chain` is
	// untouched by the merge above (deepMerge copies its base), and is
	// copied here so a caller mutating MergedDefaults cannot alias the
	// effective config. Over it, what deleting a tenant key really falls
	// back to: the profile's values (#2117, PlatformProfiles.inherited),
	// then the platform overlay's (platformInherited) — not the whole
	// platform union (a key the tenant writes as a mapping is replaced
	// wholesale, so the platform's value for it is not a fallback).
	pr := profiles.inherited(p.own, p.tenantRaw, overlay)
	pi := platformInherited(overlay, p.tenantRaw)
	//
	// Each layer goes on per THRESHOLD, not per spelling (#2368,
	// mergeOverSpellings): the platform's `mysql_threads_running` is what
	// deleting the tenant's `mysql_cpu` falls back to on /metrics, so the
	// chain's `mysql_cpu` must not stay beside it for the guard to compare
	// against. And before that, each layer on its own takes resolve's
	// canonical-wins dedup (dropShadowedSpellings): a layer writing BOTH
	// spellings serves only its canonical one on /metrics, so the losing
	// spelling is no fallback either.
	//
	// ⚠️ On `pr` (the profile layer) the dedup is defensive and unreachable
	// today: profileFor hands inherited a canonicalView of the profile, so
	// `pr` never holds both spellings. Measured (#2368 round 3): passing `pr`
	// through undeduped leaves every da-guard alias test — the 16128-cell
	// product included — unchanged. Kept so this call site does not depend
	// on that upstream detail.
	chainD := dropShadowedSpellings(chain)
	switch {
	case pr == nil && pi == nil:
		p.mergedDefaults = deepCopyMap(chainD)
	case pr == nil:
		p.mergedDefaults = mergeOverSpellings(chainD, dropShadowedSpellings(pi))
	case pi == nil:
		p.mergedDefaults = mergeOverSpellings(chainD, dropShadowedSpellings(pr))
	default:
		p.mergedDefaults = mergeOverSpellings(
			mergeOverSpellings(chainD, dropShadowedSpellings(pr)), dropShadowedSpellings(pi))
	}
	return p.effectiveParts, nil
}

// dropShadowedSpellings is one layer after resolve's canonical-wins dedup
// (canonicalizeDefaults / canonicalView): a deprecated spelling whose
// canonical spelling the same map also sets is dropped. Unlike canonicalView
// it does NOT rename a lone deprecated spelling — da-guard compares by
// literal path, and renaming would hide a legitimate same-spelling hint.
// Returns m itself when nothing is dropped (the steady state).
//
// ⛔ #2368 round 2, measured: without it a platform entry writing
// `mysql_threads_running` at 70 and `mysql_cpu` at 71 left 71 in
// MergedDefaults; a tenant writing `mysql_cpu` at 71 was called redundant,
// and deleting it moved /metrics from 71 to 70. A chain writing both
// spellings is the same shape.
func dropShadowedSpellings(m map[string]any) map[string]any {
	var drop []string
	for k := range m {
		if canon, isAlias := canonicalKeyFor(k); isAlias {
			if _, both := m[canon]; both {
				drop = append(drop, k)
			}
		}
	}
	if len(drop) == 0 {
		return m
	}
	out := make(map[string]any, len(m))
	for k, v := range m {
		out[k] = v
	}
	for _, k := range drop {
		delete(out, k)
	}
	return out
}

// mergeOverSpellings is deepMerge(base, over) where a key means the
// THRESHOLD, not its spelling (#2368): a key `over` sets drops every other
// spelling of it from base. `over` never carries both spellings of one
// threshold — the caller passes it through dropShadowedSpellings first.
//
// ⛔ It builds MergedDefaults — da-guard's "what deleting this override
// falls back to" — and nothing else. Measured without it: chain
// `mysql_cpu` at 30, platform entry `mysql_threads_running` at 70, tenant
// `mysql_cpu` at 30 → MergedDefaults held both spellings, the guard compared
// the tenant's `mysql_cpu` with the chain's 30 and advised deleting it, and
// deleting it moved /metrics from 30 to 70. The effective config is NOT
// built with it: that map keeps each layer's own spelling (see
// docs/design/config-driven.md), and changing it would move every merged_hash.
//
// A null in `over` drops nothing: on a threshold key deepMerge ignores it,
// so the base value is still the fallback.
func mergeOverSpellings(base, over map[string]any) map[string]any {
	var drop []string
	var buf [2]string
	for k, v := range over {
		if v == nil {
			continue
		}
		for _, s := range otherSpellings(k, &buf) {
			if _, in := base[s]; in {
				drop = append(drop, s)
			}
		}
	}
	if len(drop) == 0 {
		return deepMerge(base, over)
	}
	trimmed := make(map[string]any, len(base))
	for k, v := range base {
		trimmed[k] = v
	}
	for _, s := range drop {
		delete(trimmed, s)
	}
	return deepMerge(trimmed, over) // deepMerge deep-copies trimmed's values
}

// ChainDefaults is one defaults file taken through the FIRST half of the
// merge pipeline — yaml.Unmarshal, normalizeYAMLToJSON, extractDefaultsBlock
// — exactly as computeEffectiveConfigBytesDetailed takes every chain entry
// (that function is built on it, so the two cannot drift). It exists so a
// caller merging MANY tenants over one tree can parse each defaults file
// once instead of once per tenant whose chain contains it (#1978: a
// 1000-tenant, four-level cold load parsed the root `_defaults.yaml` 1000
// times). The zero value is an empty file.
//
// ⛔ Immutable once built: deepMerge deep-copies every override value it
// takes, so merging never writes into the parsed block, and one ChainDefaults
// may be shared by every tenant's chain. The block stays unexported so no
// caller outside this package can break that promise; inside it, the one
// hand-off (defaultsDictOf → the parsedDefaults cache) is read-only.
type ChainDefaults struct {
	block map[string]any // nil: the document has no defaults mapping (skipped by the merge)
	err   error          // the yaml.Unmarshal error, unwrapped
}

// ParseChainDefaults parses one defaults file's bytes for
// ComputeMergedHashFromChain. A syntax error is kept, not returned: it becomes
// the tenant's merge error (`parse defaults[i]: …`, i being the file's index
// in THAT tenant's chain) only when a chain containing the file is merged.
func ParseChainDefaults(b []byte) ChainDefaults {
	var raw any
	if err := yaml.Unmarshal(b, &raw); err != nil {
		return ChainDefaults{err: err}
	}
	return ChainDefaults{block: extractDefaultsBlock(normalizeYAMLToJSON(raw))}
}

// mergeDefaultsChain folds the chain L0→Ln. The first entry that failed to
// parse ends it with `parse defaults[%d]: %w` — the error text
// emitParseFailureSignal (app) maps back to the file by that index.
func mergeDefaultsChain(chain []ChainDefaults) (map[string]any, error) {
	merged := make(map[string]any)
	var err error
	for i, pd := range chain {
		if merged, err = foldDefaults(merged, i, pd); err != nil {
			return nil, err
		}
	}
	return merged, nil
}

// foldDefaults merges chain entry i into merged — the one step both the
// byte-input merge and mergeDefaultsChain take, so their errors and results
// cannot differ.
func foldDefaults(merged map[string]any, i int, pd ChainDefaults) (map[string]any, error) {
	if pd.err != nil {
		return nil, &chainParseError{index: i, err: pd.err}
	}
	if pd.block == nil {
		return merged, nil
	}
	return deepMerge(merged, pd.block), nil
}

// TenantDoc is one tenant file taken through the first half of the tenant
// merge — yaml.Unmarshal and normalizeYAMLToJSON — once, so a caller merging
// every tenant a file declares parses the file once instead of once per
// tenant (#2153: a file declaring T tenants was parsed T times per cold load
// and per reload, each parse also paying yaml.v3's duplicate-key check over
// the whole `tenants:` mapping — quadratic in T). The byte-input merge
// (computeEffectiveConfigBytesDetailed) is built on it, so the two cannot
// drift: same merged result, same errors.
//
// ⛔ Isolation between the tenants sharing one TenantDoc — chosen: COPY, not
// a read-only promise. tenantRaw hands each merge a deep copy of its own
// `tenants.<id>` subtree; the shared document is written by nothing after
// ParseTenantDoc returns. A read-only promise would have to hold for every
// reader of effectiveParts.tenantRaw (EffectiveConfig.TenantOverridesRaw is
// exported, and the guard reads it), which this package cannot enforce;
// the copy costs one walk of that tenant's own block, not of the file.
//
// A syntax error is kept, not returned: every tenant merged from the file
// gets it as its own `parse tenant: …` (*tenantParseError), at the same
// point of its merge (after the defaults chain) as a per-tenant parse gave.
// Safe to share across goroutines: built complete by ParseTenantDoc and only
// read afterwards.
type TenantDoc struct {
	doc any   // normalizeYAMLToJSON of the document; nil when err != nil
	err error // the yaml.Unmarshal error, unwrapped
}

// ParseTenantDoc parses one tenant file's bytes for the *Doc merge entry
// points (ComputeMergedHashDoc, ComputeMergedHashFromChainDoc).
func ParseTenantDoc(b []byte) *TenantDoc {
	doc, err := decodeTenantFile(b)
	if err != nil {
		return &TenantDoc{err: err}
	}
	return &TenantDoc{doc: doc}
}

// TenantRaw is tenantID's override block from the parsed file — a fresh
// deep copy — or an error (the file did not parse, has no `tenants:`
// mapping, or does not declare tenantID). For the exporter's reload-side
// readers that need one tenant's own block (#2118): reading it through here
// keys the tenant the way the merge does.
func (d *TenantDoc) TenantRaw(tenantID string) (map[string]any, error) {
	return d.tenantRaw(tenantID)
}

// decodeTenantFile is yaml.Unmarshal into `any` + normalizeYAMLToJSON — the
// same parse, the same errors — except that the keys of the top-level
// `tenants:` mapping are the tenant ids the flat plane serves (#2118).
//
// The flat plane decodes `tenants:` into a map[string] (ParseConfigFile, and
// simulate's tenant enumeration in source.go), so a key is its scalar TEXT
// as yaml.v3 decodes it into a string: bare `010` is tenant "010". The
// generic decode types the same key as int 8, and normalizeYAMLToJSON can
// only re-spell the decoded value ("8"), so the walker looked "010" up and
// answered `not in file` — /effective 404, da-guard failing the whole scope,
// the exporter skipping the tenant's merged_hash — for a tenant /metrics
// serves. Likewise `0x1`, `007`, `1.0`, `2024-01-01`, `+1`, `.inf`.
//
// ⛔ Only the `tenants:` keys are re-keyed; normalizeYAMLToJSON's general
// rule for every other mapping (a key inside a tenant's block, `defaults:`)
// is unchanged. A file whose `tenants:` keys are all strings — every file
// but the ones this fixes — pays one walk over the keys and nothing more;
// a file with a non-string key re-decodes each tenant's body from its own
// node, so two keys the generic decode collapses (`010` and `8` are both
// int 8) keep their own bodies, as they do on the flat plane.
func decodeTenantFile(b []byte) (any, error) {
	var root yaml.Node
	if err := yaml.Unmarshal(b, &root); err != nil {
		return nil, err
	}
	if root.Kind == 0 {
		// Empty input: yaml.Unmarshal leaves `any` nil, no error.
		return nil, nil
	}
	var raw any
	if err := root.Decode(&raw); err != nil {
		return nil, err
	}
	doc := normalizeYAMLToJSON(raw)
	m, ok := doc.(map[string]any)
	if !ok {
		return doc, nil
	}
	if block, ok := tenantsBlockByKeyText(&root); ok {
		m["tenants"] = block
	}
	// A scalar `_profile` is its text, as on the flat plane (#2433): bare
	// `010` names profile "010", not int 8.
	if block, ok := m["tenants"].(map[string]any); ok && tenantsWriteProfile(block) {
		texts := profileTexts(root.Decode)
		for tid, body := range block {
			if b, ok := body.(map[string]any); ok {
				withProfileText(b, texts, tid)
			}
		}
	}
	return doc, nil
}

// tenantsBlockByKeyText rebuilds the top-level `tenants:` mapping of the
// parsed document keyed by each key's string decode (see decodeTenantFile).
// ok=false — keep the generic decode — when there is no such mapping, every
// key already is a string, or a key is not a plain scalar (a merge key
// `<<`, an alias): shapes the flat plane's typed decode does not key by
// text either, left exactly as before.
func tenantsBlockByKeyText(root *yaml.Node) (map[string]any, bool) {
	n := root
	if n.Kind == yaml.DocumentNode && len(n.Content) == 1 {
		n = n.Content[0]
	}
	if n.Kind != yaml.MappingNode {
		return nil, false
	}
	var tn *yaml.Node
	for i := 0; i+1 < len(n.Content); i += 2 {
		if k := n.Content[i]; k.Kind == yaml.ScalarNode && k.ShortTag() == "!!str" && k.Value == "tenants" {
			tn = n.Content[i+1]
		}
	}
	if tn != nil && tn.Kind == yaml.AliasNode {
		tn = tn.Alias
	}
	if tn == nil || tn.Kind != yaml.MappingNode {
		return nil, false
	}
	retype := false
	for i := 0; i+1 < len(tn.Content); i += 2 {
		k := tn.Content[i]
		if k.Kind != yaml.ScalarNode || k.ShortTag() == "!!merge" {
			return nil, false
		}
		if k.ShortTag() != "!!str" {
			retype = true
		}
	}
	if !retype {
		return nil, false
	}
	block := make(map[string]any, len(tn.Content)/2)
	for i := 0; i+1 < len(tn.Content); i += 2 {
		var id string
		if err := tn.Content[i].Decode(&id); err != nil {
			return nil, false
		}
		var body any
		if err := tn.Content[i+1].Decode(&body); err != nil {
			return nil, false
		}
		block[id] = normalizeYAMLToJSON(body)
	}
	return block, true
}

// tenantRaw is tenantID's override block — a fresh deep copy, see TenantDoc —
// or the error the per-tenant parse + extractTenantRaw gave: a fresh
// *tenantParseError per call (errors.As / DecodeError attribution unchanged).
func (d *TenantDoc) tenantRaw(tenantID string) (map[string]any, error) {
	if d.err != nil {
		return nil, &tenantParseError{err: d.err}
	}
	raw, err := extractTenantRaw(d.doc, tenantID)
	if err != nil {
		return nil, err
	}
	return deepCopyMap(raw), nil
}

// tenantMerge is mergeTenantOver's result: the effective parts it fills
// (not mergedDefaults) and `own`, the tenant block with the platform
// overlay applied (before profile expansion) — the layer whose `_profile`
// elects the profile.
type tenantMerge struct {
	effectiveParts
	own map[string]any
}

// mergeTenantOver applies tenantID's override block from the parsed tenant
// file on top of the merged defaults — with the platform overlay's keys
// the tenant file does not write filled in first (overlayTenant; #2019),
// then the elected profile's keys neither of those writes
// (PlatformProfiles.expand; #2117). `profiles` nil = no profile expansion.
// The block is the tenant's own copy (TenantDoc.tenantRaw), so nothing done
// to it here or by a caller holding effectiveParts.tenantRaw reaches the
// shared document.
func mergeTenantOver(merged map[string]any, tenantDoc *TenantDoc, tenantID string, overlay []PlatformBlock, profiles *PlatformProfiles) (tenantMerge, error) {
	tenantRaw, err := tenantDoc.tenantRaw(tenantID)
	if err != nil {
		return tenantMerge{}, err
	}
	own, platformSources := overlayTenant(tenantRaw, overlay, merged)
	override, profileSources := profiles.expand(own, merged)
	return tenantMerge{
		effectiveParts: effectiveParts{
			merged:          deepMerge(merged, override),
			tenantRaw:       tenantRaw,
			platformSources: platformSources,
			profileSources:  profileSources,
		},
		own: own,
	}, nil
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
	out, err := encodeCanonical(data)
	if err == nil {
		return out, nil
	}
	// encoding/json has no token for a non-finite float and refuses the
	// whole document; Python's json.dumps (allow_nan=True, the default in
	// describe_tenant.py's _canonical_hash) writes NaN / Infinity /
	// -Infinity. A YAML `.inf` / `.nan` anywhere in a tenant (a routing
	// override, a threshold) made every Go reader of the merged hash —
	// da-guard, the exporter, tenant-api — fail the tenant while the Python
	// tools read the same tree fine. Only that error takes the slow path,
	// so every document encoding/json accepts stays byte-identical.
	var unsupported *json.UnsupportedValueError
	if !errors.As(err, &unsupported) {
		return nil, fmt.Errorf("canonicalJSON encode: %w", err)
	}
	var buf bytes.Buffer
	if err := writeCanonicalNonFinite(&buf, data); err != nil {
		return nil, fmt.Errorf("canonicalJSON encode: %w", err)
	}
	return buf.Bytes(), nil
}

// encodeCanonical is encoding/json with no HTML escaping and no trailing
// newline — the canonical form of every value encoding/json accepts.
func encodeCanonical(v any) ([]byte, error) {
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(v); err != nil {
		return nil, err
	}
	out := buf.Bytes()
	if n := len(out); n > 0 && out[n-1] == '\n' {
		out = out[:n-1]
	}
	return out, nil
}

// writeCanonicalNonFinite writes v the way json.dumps(sort_keys=True,
// separators=(",", ":")) does, descending into the containers a decoded
// YAML tree holds (map[string]any, []any) so a non-finite float at any
// depth becomes Python's token. Every other value goes through
// encodeCanonical, so it renders exactly as on the fast path.
func writeCanonicalNonFinite(buf *bytes.Buffer, v any) error {
	switch x := v.(type) {
	case float64:
		switch {
		case math.IsNaN(x):
			buf.WriteString("NaN")
			return nil
		case math.IsInf(x, 1):
			buf.WriteString("Infinity")
			return nil
		case math.IsInf(x, -1):
			buf.WriteString("-Infinity")
			return nil
		}
	case map[string]any:
		keys := make([]string, 0, len(x))
		for k := range x {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		buf.WriteByte('{')
		for i, k := range keys {
			if i > 0 {
				buf.WriteByte(',')
			}
			kb, err := encodeCanonical(k)
			if err != nil {
				return err
			}
			buf.Write(kb)
			buf.WriteByte(':')
			if err := writeCanonicalNonFinite(buf, x[k]); err != nil {
				return err
			}
		}
		buf.WriteByte('}')
		return nil
	case []any:
		buf.WriteByte('[')
		for i, e := range x {
			if i > 0 {
				buf.WriteByte(',')
			}
			if err := writeCanonicalNonFinite(buf, e); err != nil {
				return err
			}
		}
		buf.WriteByte(']')
		return nil
	}
	b, err := encodeCanonical(v)
	if err != nil {
		return err
	}
	buf.Write(b)
	return nil
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
//
// ⚠️ Over a doc from yaml.Unmarshal + NormalizeYAMLToJSON, a bare
// non-string key (`010:`) is re-spelled ("8") and the flat plane's id
// ("010") is not found (#2118). To read a tenant FILE by the flat plane's
// id, use ParseTenantDoc(b).TenantRaw(id).
func ExtractTenantRaw(doc any, tenantID string) (map[string]any, error) {
	return extractTenantRaw(doc, tenantID)
}

// CanonicalJSON encodes data with sort_keys + no-space + no-HTML-escape
// + no-trailing-newline, matching describe_tenant.py's _canonical_hash.
// MUST stay byte-equal across Go and Python implementations — golden
// fixtures in app/config_golden_parity_test.go pin the contract.
func CanonicalJSON(data any) ([]byte, error) { return canonicalJSON(data) }

// TenantLayers is what the merge applies between the defaults chain and the
// tenant file, besides the tenant file itself: the root platform files'
// entries for the tenant (PlatformOverlayFor; #2019) — applied key by key
// with the tenant file winning — and the root platform files' profiles
// (LoadRootPlatformProfiles; #2117), whose elected profile fills in the
// keys neither of those writes. The zero value applies neither.
type TenantLayers struct {
	Overlay  []PlatformBlock
	Profiles *PlatformProfiles
}

// oneLayers is the optional TenantLayers argument: the zero value when none
// is passed. ⛔ More than one is a programming error (which would win?), so
// it panics rather than silently merging a subset.
func oneLayers(layers []TenantLayers) TenantLayers {
	switch len(layers) {
	case 0:
		return TenantLayers{}
	case 1:
		return layers[0]
	}
	panic(fmt.Sprintf("config: %d TenantLayers passed, want at most one", len(layers)))
}

// ComputeEffectiveConfig is the byte-input version of
// computeEffectiveConfigBytes — public-facing alias for the simulate
// primitive (app/handler_simulate.go) and the inheritance.go wrappers.
//
// `layers` (at most one) is the tenant's platform overlay and the tree's
// profiles (TenantLayers). ⛔ Variadic ON PURPOSE: a caller that passes
// none gets exactly the chain + tenant-file merge, and every call site
// keeps using ONE merge function, not a *WithOverlay / *WithProfiles twin
// that could drift from it. The exporter's merged_hash, ConfigManager.Resolve
// and /effective pass the same layers, so the three agree (#2019, #2117).
func ComputeEffectiveConfig(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
	layers ...TenantLayers,
) (map[string]any, error) {
	l := oneLayers(layers)
	parts, err := computeEffectiveConfigBytesDetailed(tenantYAMLBytes, tenantID, defaultsChainYAML, l.Overlay, l.Profiles)
	return parts.merged, err
}

// ComputeMergedHash returns the 16-char `merged_hash` user-facing
// fingerprint. SHA-256 over CanonicalJSON of ComputeEffectiveConfig's
// output, truncated to 16 hex chars. Parity with describe_tenant.py
// pinned by the golden fixtures under tests/golden/. `layers` as for
// ComputeEffectiveConfig.
func ComputeMergedHash(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChainYAML [][]byte,
	layers ...TenantLayers,
) (string, error) {
	return ComputeMergedHashDoc(ParseTenantDoc(tenantYAMLBytes), tenantID, defaultsChainYAML, layers...)
}

// ComputeMergedHashDoc is ComputeMergedHash over a tenant file already
// parsed by ParseTenantDoc (#2153) — ComputeMergedHash is this plus the
// parse. For a caller hashing many tenants of one file: parse it once and
// pass the same TenantDoc for each.
func ComputeMergedHashDoc(
	tenantDoc *TenantDoc,
	tenantID string,
	defaultsChainYAML [][]byte,
	layers ...TenantLayers,
) (string, error) {
	l := oneLayers(layers)
	parts, err := computeEffectiveConfigDocDetailed(tenantDoc, tenantID, defaultsChainYAML, l.Overlay, l.Profiles)
	if err != nil {
		return "", err
	}
	return mergedHashOf(parts.merged)
}

// ComputeMergedHashFromChain is ComputeMergedHash over a defaults chain whose
// files were already parsed by ParseChainDefaults (#1978). Same pipeline,
// same errors, same 16-char value for the same bytes — the byte-input
// function above shares both halves with it — ParseChainDefaults for the
// parse and foldDefaults for the per-file fold (mergeDefaultsChain here,
// computeEffectiveConfigBytesDetailed there) — so this is not a second merge. It only skips the
// merged-defaults snapshot ComputeMergedHash computes and discards.
// `layers` as for ComputeEffectiveConfig.
func ComputeMergedHashFromChain(
	tenantYAMLBytes []byte,
	tenantID string,
	defaultsChain []ChainDefaults,
	layers ...TenantLayers,
) (string, error) {
	return ComputeMergedHashFromChainDoc(ParseTenantDoc(tenantYAMLBytes), tenantID, defaultsChain, layers...)
}

// ComputeMergedHashFromChainDoc is ComputeMergedHashFromChain over a tenant
// file already parsed by ParseTenantDoc (#2153): the cold load's form, with
// both the defaults chain and the tenant file parsed once per load.
func ComputeMergedHashFromChainDoc(
	tenantDoc *TenantDoc,
	tenantID string,
	defaultsChain []ChainDefaults,
	layers ...TenantLayers,
) (string, error) {
	merged, err := mergeDefaultsChain(defaultsChain)
	if err != nil {
		return "", err
	}
	l := oneLayers(layers)
	tm, err := mergeTenantOver(merged, tenantDoc, tenantID, l.Overlay, l.Profiles)
	if err != nil {
		return "", err
	}
	return mergedHashOf(tm.merged)
}

// mergedHashOf is SHA-256 over CanonicalJSON(merged), truncated to 16 hex.
func mergedHashOf(merged map[string]any) (string, error) {
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
