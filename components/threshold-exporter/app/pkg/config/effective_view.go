package config

import "strings"

// The effective config the walker REPORTS (#2115 F1 / F4): /effective and
// `da-guard effective` print EffectiveConfig.EffectiveConfig, /simulate
// returns the same for its request tree (SimulateEffective), and since
// #2414 / #2433 the walker's answer is meant to be what /metrics serves.
// The merge that feeds merged_hash (deepMerge over every layer) is not that
// in two shapes, both measured on main fc5438c4:
//
//   - F1, one threshold under two #1231 spellings across layers. Root
//     `mysql_threads_running` at 30, subtree `_defaults.yaml` the retired
//     spelling at 40: the merge kept both keys (40 and 30) while /metrics
//     serves 40 — applySubtreeDefaults hands the deeper level down per
//     threshold, and the tenant side beats the chain per threshold too
//     (tenantAuthoredThreshold). A tenant writing the retired spelling at
//     "60" over a root 80 was the same: both keys, /metrics 60.
//   - F4, a schedule merged leaf by leaf. Subtree `{default: "150",
//     overrides: [22:00-06:00 → 300]}`, tenant `{default: "160"}`: the merge
//     gave {default: "160", overrides: [...]}, while /metrics takes the
//     tenant's map whole (scheduledValueFromRaw of the one value its layer
//     wrote) and serves 160 at 23:00. A deeper subtree level writing a
//     schedule over a shallower one is the same shape (/metrics:
//     `overrides[key] = value`).
//
// effectiveView is that answer: each layer — chain level by level (root
// first), then the tenant side (tenant file + platform `tenants:` + profile,
// already one map: effectiveParts.override) — first deduplicated the way
// /metrics dedups one layer (both spellings written → the canonical one
// stays, canonicalizeDefaults / canonicalView), then laid over the layers
// below PER THRESHOLD: a threshold key (a key not starting with `_`) whose
// value is a scalar, a list or a schedule replaces whatever the lower layers
// hold under ANY of its spellings, whole. The winning layer's own spelling
// is kept — the dropShallowerSpellings / mergeOverSpellings convention — so
// key_sources still names a key that layer wrote. Reserved (`_`) keys, and a
// mapping that is not a schedule (mappingNotSchedule), go through deepMerge
// exactly as before.
//
// ⛔ merged_hash is NOT computed from this view: it stays the deepMerge
// result's hash, the exporter's own merged_hash (ComputeMergedHash*) and
// describe_tenant.py's, pinned by the golden fixtures. Moving it would
// re-hash every tenant of these shapes on the exporter for no change in
// what /metrics serves. The same split as the non-finite rendering
// (NonFiniteAsText): effective_config is what is served, merged_hash the
// reload identity. da-guard's main gate (schema / required fields,
// cardinality) also keeps judging the merge (EffectiveConfig.MergedConfig):
// resolvePath reads no alias, so the view would report a required canonical
// key missing where the subtree writes the retired spelling.
//
// A threshold written as null, or as a schedule that writes nothing
// (`{default: null}`, nullThreshold), writes nothing here either, as in
// deepMerge: the value below shows through.
func effectiveView(chainBlocks []map[string]any, override map[string]any) map[string]any {
	view := make(map[string]any)
	for _, block := range chainBlocks {
		view = layerPerThreshold(view, block)
	}
	return layerPerThreshold(view, override)
}

// layerPerThreshold is one effectiveView step: over deduplicated, then laid
// on base per threshold (see effectiveView). base is the view built so far,
// owned by the caller and written in place for threshold keys.
func layerPerThreshold(base, over map[string]any) map[string]any {
	if len(over) == 0 {
		return base
	}
	var leafMerged map[string]any
	var buf [2]string
	for k, v := range over {
		if strings.HasPrefix(k, "_") || mappingNotSchedule(v) {
			if leafMerged == nil {
				leafMerged = make(map[string]any)
			}
			leafMerged[k] = v
			continue
		}
		if !writesThreshold(k, v) || shadowedInLayer(over, k) {
			continue
		}
		for _, s := range otherSpellings(k, &buf) {
			delete(base, s)
		}
		base[k] = deepCopyValue(v)
	}
	if leafMerged == nil {
		return base
	}
	return deepMerge(base, leafMerged)
}

// mappingNotSchedule reports whether v is a mapping /metrics does not take as
// one threshold value (scheduledValueFromRaw + isThresholdShaped, the test
// applySubtreeDefaults hands a level's value down by) — the golden fixtures'
// `threshold: {cpu: 85}` shape, or an `overrides:`-only mapping. No layer
// serves such a value whole, so it keeps deepMerge's leaf-by-leaf merge,
// unchanged by #2115. A schedule mapping is laid whole.
func mappingNotSchedule(v any) bool {
	if _, isMap := v.(map[string]any); !isMap {
		return false
	}
	sv, ok := scheduledValueFromRaw(v)
	return !ok || !isThresholdShaped(sv)
}

// writesThreshold reports whether a layer's value for threshold key k is a
// write — not null, not a schedule that writes nothing (#2518, #2708).
func writesThreshold(k string, v any) bool {
	return v != nil && !nullThreshold(k, v)
}

// shadowedInLayer reports whether k is a retired spelling whose canonical
// spelling the same layer also writes: /metrics serves only the canonical
// one (canonical wins inside one layer). A canonical spelling written as
// null shadows nothing — /metrics serves the retired spelling's value then
// (#2418), as levelWritesSpelling rules for the chain.
func shadowedInLayer(layer map[string]any, k string) bool {
	canon, isAlias := canonicalKeyFor(k)
	if !isAlias {
		return false
	}
	v, in := layer[canon]
	return in && writesThreshold(canon, v)
}
