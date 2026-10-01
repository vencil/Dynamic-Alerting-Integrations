package config

// Per-key attribution of an effective config (#2564): for every top-level
// key of EffectiveConfig.EffectiveConfig, the layer and the file its value
// came from. `da-guard effective` prints it so the Python readers stop
// re-deriving "where does this value come from" with their own walker.
//
// ⛔ NOT A SECOND MERGE. The value of every key is the resolver's own
// (computeEffectiveConfigDocDetailed); this file only names, per key of that
// result, the highest-precedence layer that wrote it, reading each layer as
// the merge read it:
//
//   - the defaults chain: each level's block as ParseChainDefaults parsed it
//     (the parse the merge folds), deepest level writing the key non-null;
//   - the profile layer: the merge's own attribution (ProfileOverlay);
//   - the platform layer: the merge's own attribution (PlatformOverlay),
//     which names the CANONICAL spelling — mapped back to the spelling the
//     platform block wrote, since the effective config keeps each layer's
//     own spelling;
//   - the tenant file: its raw block (TenantOverridesRaw), key written
//     non-null.
//
// Precedence is the merge's (tenant file > platform `tenants:` > profile >
// chain, deepest level last). A null never attributes: on a threshold key
// the merge ignores it (the value is inherited), and on a reserved key it
// deletes the value, so the key is not in the effective config at all.
//
// ⚠️ Granularity is the TOP-LEVEL key, like PlatformOverlay / ProfileOverlay.
// A mapping value is merged leaf by leaf across layers; its source names the
// highest layer that wrote any part of it, and lower layers may have
// supplied other leaves.

import (
	"fmt"
	"sort"
)

// Layer names of KeySource.Layer.
const (
	KeyLayerDefaults = "defaults"
	KeyLayerPlatform = "platform"
	KeyLayerProfile  = "profile"
	KeyLayerTenant   = "tenant"
)

// KeySource is where one top-level key of an effective config came from.
type KeySource struct {
	// Layer is one of KeyLayerDefaults, KeyLayerPlatform, KeyLayerProfile,
	// KeyLayerTenant.
	Layer string `json:"layer"`
	// File is the file that wrote the value, relative to the scan root,
	// slash-separated (the spelling of DefaultsChain / SourceFile).
	File string `json:"file"`
	// Level is the file's index in DefaultsChain (0 = the root) for the
	// defaults layer; absent for the other layers.
	Level *int `json:"level,omitempty"`
}

// keySources attributes every key of merged. chainFiles[i] is the rel path
// of chain level i and chainBlocks[i] its parsed defaults block (nil: the
// level has no defaults mapping). Every key of merged must be attributed:
// a key no layer writes is an internal error, not a guess.
func keySources(
	merged, tenantRaw map[string]any,
	tenantFile string,
	chainFiles []string,
	chainBlocks []map[string]any,
	overlay []PlatformBlock,
	platformSources []PlatformOverlaySource,
	profileSources []ProfileOverlaySource,
) (map[string]KeySource, error) {
	out := make(map[string]KeySource, len(merged))

	// Lowest precedence first; each later layer overwrites.
	for i, block := range chainBlocks {
		for k, v := range block {
			if v == nil {
				continue
			}
			if _, in := merged[k]; in {
				level := i
				out[k] = KeySource{Layer: KeyLayerDefaults, File: chainFiles[i], Level: &level}
			}
		}
	}
	for _, ps := range profileSources {
		for _, k := range ps.Keys {
			if _, in := merged[k]; in {
				out[k] = KeySource{Layer: KeyLayerProfile, File: ps.File}
			}
		}
	}
	for _, ps := range platformSources {
		block := platformBlockOf(overlay, ps.File)
		for _, canon := range ps.Keys {
			for k, v := range block {
				if v == nil {
					continue
				}
				if c, _ := canonicalKeyFor(k); c != canon {
					continue
				}
				if _, in := merged[k]; in {
					out[k] = KeySource{Layer: KeyLayerPlatform, File: ps.File}
				}
			}
		}
	}
	for k, v := range tenantRaw {
		if v == nil {
			continue
		}
		if _, in := merged[k]; in {
			out[k] = KeySource{Layer: KeyLayerTenant, File: tenantFile}
		}
	}

	var missing []string
	for k := range merged {
		if _, ok := out[k]; !ok {
			missing = append(missing, k)
		}
	}
	if len(missing) > 0 {
		sort.Strings(missing)
		return nil, fmt.Errorf("internal: no layer writes effective key(s) %q", missing)
	}
	return out, nil
}

// platformBlockOf is the block of the overlay entry for file; nil if none.
func platformBlockOf(overlay []PlatformBlock, file string) map[string]any {
	for _, pb := range overlay {
		if pb.File == file {
			return pb.Block
		}
	}
	return nil
}
