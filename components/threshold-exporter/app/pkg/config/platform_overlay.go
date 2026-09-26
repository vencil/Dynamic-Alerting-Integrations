package config

// Walker-plane half of the platform per-tenant layer (#2019).
//
// A ROOT platform file (`_defaults.yaml`, `_profiles.yaml`, any root
// `_`-prefixed file) may carry a `tenants:` block: the platform's per-tenant
// DEFAULT. The flat plane (/metrics) has applied it since #1982
// (sortFlatMergeOrder / mergePartialConfigs); this file is what lets the
// merge core the walker plane shares — ResolveEffective (/effective),
// ScopeEffective (da-guard) and the exporter's merged_hash — apply the same
// layer instead of reading only the defaults chain and the tenant file.
//
// Semantics (the oracle is tests/shared/platform_tenant_overlay_matrix.json):
//
//   - WHICH FILES: rootPlatformKeys (flat_build.go) — the flat merge's own
//     predicates; a nested platform file's block is read by no plane (#1576)
//     and an unselected root carrier contributes nothing (#1674).
//   - WHICH TENANTS: only one that EXISTS, i.e. that a tenant file declares.
//     The walker plane only ever merges such a tenant (Locate / the scan's
//     Tenants), so a platform entry cannot create one here either.
//   - PRECEDENCE: the blocks apply after the defaults chain and before the
//     tenant file, per top-level key: a later platform file (sort order)
//     over an earlier one, the tenant file over all of them — exactly the flat
//     plane's map overwrite. A platform key is therefore merged over the
//     chain as if the tenant file had written it (see overlayTenant).
//   - WHICH BYTES COUNT: a file contributes its block iff the flat plane's
//     decode (ParseConfigFile, parsePartialConfig's verdict) accepts it — a
//     file /metrics drops whole is not overlaid here either.

import (
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// PlatformTenants is ONE root platform file's `tenants:` block, decoded once
// and shared read-only by every tenant it names (the same immutability
// promise as ChainDefaults: deepMerge copies every value it takes).
type PlatformTenants struct {
	File    string // scan key (root-relative slash path) — what platform_overlay reports
	AbsPath string // TreeFile.AbsPath — the reload's scope attribution
	Hash    string // TreeFile.Hash the block was decoded from — the reload's reuse key
	tenants map[string]map[string]any
}

// Block returns the file's entry for tenantID (read-only), if any.
func (pt PlatformTenants) Block(tenantID string) (map[string]any, bool) {
	b, ok := pt.tenants[tenantID]
	return b, ok
}

// PlatformBlock is one root platform file's entry for ONE tenant — the
// optional overlay input of ComputeEffectiveConfig / ComputeMergedHash /
// ComputeMergedHashFromChain, in merge order.
type PlatformBlock struct {
	File  string
	Block map[string]any
}

// PlatformOverlaySource is one entry of EffectiveConfig.PlatformOverlay: a
// root platform file and the top-level keys whose value in the effective
// config it supplied (the tenant file does not write them and no later
// platform file overwrote them). Field names match describe_tenant.py.
type PlatformOverlaySource struct {
	File string   `json:"file"`
	Keys []string `json:"keys"`
}

// parsePlatformTenants decodes one root platform file's `tenants:` block.
// A file the flat plane's decode rejects contributes nothing (see the file
// comment); so does a tenant entry whose body is not a mapping (a null body
// has no keys; any other shape is already a ParseConfigFile rejection).
//
// ⚠️ The typed decode runs only when an untyped one found a non-empty
// `tenants:` mapping, so a tree without per-tenant platform values pays one
// untyped decode per root platform file and nothing more.
func parsePlatformTenants(key string, f *TreeFile, data []byte) PlatformTenants {
	pt := PlatformTenants{File: key, AbsPath: f.AbsPath, Hash: f.Hash}
	var doc struct {
		Tenants map[string]any `yaml:"tenants"`
	}
	if yaml.Unmarshal(data, &doc) != nil || len(doc.Tenants) == 0 {
		return pt
	}
	if _, err := ParseConfigFile(data); err != nil {
		return pt
	}
	for tid, body := range doc.Tenants {
		m, ok := normalizeYAMLToJSON(body).(map[string]any)
		if !ok || len(m) == 0 {
			continue
		}
		if pt.tenants == nil {
			pt.tenants = make(map[string]map[string]any, len(doc.Tenants))
		}
		pt.tenants[tid] = m
	}
	return pt
}

// LoadRootPlatformTenants decodes the `tenants:` block of every root platform
// file of `scan` (rootPlatformKeys, merge order). An entry of `prior` with
// the same file and hash is reused instead of decoded again — the reload
// path's cache; the one-shot readers pass nil. `bytesOf` supplies a file's
// bytes when it must be decoded; a file it cannot supply contributes nothing
// (the walker already logged an unreadable file).
func LoadRootPlatformTenants(scan *TreeScan, prior []PlatformTenants, bytesOf func(*TreeFile) ([]byte, error)) []PlatformTenants {
	keys := rootPlatformKeys(scan)
	if len(keys) == 0 {
		return nil
	}
	out := make([]PlatformTenants, 0, len(keys))
	for _, k := range keys {
		f := scan.Files[k]
		if reused, ok := findPlatformTenants(prior, k, f.Hash); ok {
			out = append(out, reused)
			continue
		}
		data, err := bytesOf(f)
		if err != nil {
			continue
		}
		out = append(out, parsePlatformTenants(k, f, data))
	}
	return out
}

func findPlatformTenants(files []PlatformTenants, key, hash string) (PlatformTenants, bool) {
	for _, pt := range files {
		if pt.File == key && pt.Hash == hash {
			return pt, true
		}
	}
	return PlatformTenants{}, false
}

// PlatformOverlayFor returns tenantID's entries across `files`, in merge
// order; nil when no root platform file names the tenant. The caller asks
// only for a tenant that exists (a tenant file declares it).
func PlatformOverlayFor(files []PlatformTenants, tenantID string) []PlatformBlock {
	var out []PlatformBlock
	for _, pt := range files {
		if b, ok := pt.tenants[tenantID]; ok {
			out = append(out, PlatformBlock{File: pt.File, Block: b})
		}
	}
	return out
}

// overlayTenant returns the override the merge applies over the defaults
// chain — the tenant file's block with every top-level key it does NOT
// write taken from the platform blocks (a later block over an earlier one)
// — and the attribution of those keys.
//
// ⛔ SHALLOW, PER TOP-LEVEL KEY, like the flat plane: a tenant key replaces
// the platform's value for that key wholesale (its `_routing` map, its
// scheduled value) rather than deep-merging into it; each surviving
// platform value then deep-merges into the chain exactly as a tenant value
// would. With no overlay the tenant block is returned as is (no copy).
//
// Attribution: a key is reported when the platform value is what the
// effective config carries for it — never `_metadata` (not inherited), and
// never a null on a threshold key (deepMerge ignores it). A null on a
// RESERVED (`_`-prefixed) key deletes the inherited value, so it is
// reported exactly when `chain` (the merged defaults chain) has that key
// to delete.
func overlayTenant(tenantRaw map[string]any, overlay []PlatformBlock, chain map[string]any) (map[string]any, []PlatformOverlaySource) {
	if len(overlay) == 0 {
		return tenantRaw, nil
	}
	combined := make(map[string]any, len(tenantRaw)+len(overlay[0].Block))
	owner := make(map[string]int)
	for i, pb := range overlay {
		for k, v := range pb.Block {
			combined[k] = v
			owner[k] = i
		}
	}
	for k, v := range tenantRaw {
		combined[k] = v
		delete(owner, k)
	}
	byFile := make([][]string, len(overlay))
	for k, i := range owner {
		if k == "_metadata" {
			continue
		}
		if combined[k] == nil {
			if _, inherited := chain[k]; !strings.HasPrefix(k, "_") || !inherited {
				continue
			}
		}
		byFile[i] = append(byFile[i], k)
	}
	var sources []PlatformOverlaySource
	for i, keys := range byFile {
		if len(keys) == 0 {
			continue
		}
		sort.Strings(keys)
		sources = append(sources, PlatformOverlaySource{File: overlay[i].File, Keys: keys})
	}
	return combined, sources
}

// platformInherited is the part of the platform layer a tenant key falls
// back to when it is deleted from the tenant file — what
// EffectiveConfig.MergedDefaults (the guard's "inherited value") merges
// over the chain. nil when nothing qualifies.
//
// ⛔ NOT THE WHOLE PLATFORM UNION. The layer replaces per TOP-LEVEL key
// (overlayTenant), so a key whose platform value is a mapping (`_routing`,
// a scheduled `{default: …}`) or which the tenant file writes as a mapping
// is replaced wholesale by the tenant's value: a leaf removed from the
// tenant's mapping falls back to the CHAIN, not to the platform's mapping.
// Merging the platform mapping in made the guard call a leaf redundant
// whose removal changes the effective config. Only a non-mapping platform
// value over a non-mapping (or absent) tenant value is what deleting that
// tenant key falls back to.
func platformInherited(overlay []PlatformBlock, tenantRaw map[string]any) map[string]any {
	var out map[string]any
	for _, pb := range overlay {
		for k, v := range pb.Block {
			if out == nil {
				out = make(map[string]any)
			}
			out[k] = v
		}
	}
	for k, v := range out {
		_, platformMap := v.(map[string]any)
		_, tenantMap := tenantRaw[k].(map[string]any)
		if platformMap || tenantMap {
			delete(out, k)
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// ApplyPlatformOverlay is overlayTenant's merged override without the
// attribution: the tenant block with the platform keys it does not write
// filled in. For package main's reload classifier, which asks which keys
// the tenant's own layer (file + platform entries) sets.
func ApplyPlatformOverlay(tenantRaw map[string]any, overlay []PlatformBlock) map[string]any {
	combined, _ := overlayTenant(tenantRaw, overlay, nil)
	return combined
}
