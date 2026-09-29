package config

import "math"

// NonFiniteAsText returns m with every NaN / ±Inf float64 replaced by the
// token Python's json.dumps writes for it — "NaN", "Infinity", "-Infinity" —
// as a JSON string. It descends into the containers a decoded YAML tree holds
// (map[string]any, []any); every other value is returned as is.
//
// Why: a YAML `.inf` / `-.inf` / `.nan` anywhere in a tenant reaches the
// effective config as a non-finite float64, and encoding/json refuses the whole
// document on one ("json: unsupported value: +Inf"). The HTTP responses that
// carry the effective config (tenant-api /effective, the exporter's /simulate)
// call this before encoding so such a tenant is still readable. The token is a
// JSON *string* because JSON has no number for it; describe_tenant.py writes
// the same token bare (invalid JSON), and canonicalJSON writes it bare too, so
// merged_hash — computed from the original tree, never from this copy — still
// matches Python.
//
// m is never modified: a container that holds a non-finite float somewhere
// below it is copied, one that does not is returned shared (m itself when
// nothing is replaced).
func NonFiniteAsText(m map[string]any) map[string]any {
	out, changed := nonFiniteAsText(m)
	if !changed {
		return m
	}
	return out.(map[string]any)
}

// nonFiniteAsText reports whether anything was replaced, so an unchanged
// container is returned shared instead of copied.
func nonFiniteAsText(v any) (any, bool) {
	switch x := v.(type) {
	case float64:
		switch {
		case math.IsNaN(x):
			return "NaN", true
		case math.IsInf(x, 1):
			return "Infinity", true
		case math.IsInf(x, -1):
			return "-Infinity", true
		}
	case map[string]any:
		var out map[string]any
		for k, e := range x {
			r, changed := nonFiniteAsText(e)
			if !changed {
				continue
			}
			if out == nil {
				out = make(map[string]any, len(x))
				for kk, ee := range x {
					out[kk] = ee
				}
			}
			out[k] = r
		}
		if out != nil {
			return out, true
		}
	case []any:
		var out []any
		for i, e := range x {
			r, changed := nonFiniteAsText(e)
			if !changed {
				continue
			}
			if out == nil {
				out = append([]any(nil), x...)
			}
			out[i] = r
		}
		if out != nil {
			return out, true
		}
	}
	return v, false
}
