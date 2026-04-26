package guard

// Dotted-path helpers shared by the Schema and Redundant-override
// checks.
//
// Both checks need to resolve a path like `thresholds.cpu_threshold`
// against a `map[string]any` that may have nested maps at any depth.
// Path semantics — same as the future PR-4 CLI flag spelling so a
// reviewer reading a finding can paste the path back into the CLI:
//
//   - `.` separates levels.
//   - Leaf values can be any type (scalar / slice / nil).
//   - A path that traverses a non-map mid-walk resolves to "not
//     found" (e.g. `a.b.c` against `{"a": {"b": "scalar"}}` → not
//     found at `.c` because `b` isn't a map).
//
// PR-1 doesn't need fancier addressing (array indexing, wildcards).
// If PR-2 routing checks need it, add a separate Path type then —
// keeping path.go single-purpose makes test coverage tractable.

import "strings"

// resolvePath walks `m` along the dotted `path` and returns the
// terminal value plus a "found" flag.
//
//   - `path == ""` → returns (m, true) — the whole map.
//   - Path that doesn't exist → returns (nil, false).
//   - Path that exists but resolves to YAML null / Go nil →
//     returns (nil, true). Callers that treat "missing" and
//     "explicit null" the same should check for nil after the bool.
func resolvePath(m map[string]any, path string) (any, bool) {
	if path == "" {
		return m, true
	}
	parts := strings.Split(path, ".")
	var current any = m
	for _, p := range parts {
		nested, ok := current.(map[string]any)
		if !ok {
			return nil, false
		}
		v, exists := nested[p]
		if !exists {
			return nil, false
		}
		current = v
	}
	return current, true
}

// flattenLeaves walks `m` recursively and emits every leaf value
// keyed by its dotted path. Used by the redundant-override check
// to enumerate every tenant override field.
//
//   - Nested map[string]any values recurse; their entries are
//     emitted with the full dotted path.
//   - Slices, scalars, and nil are leaves.
//   - An empty input map returns an empty (non-nil) result map so
//     callers can range-over without a nil check.
//
// Iteration order of nested maps follows Go's map iteration order,
// i.e. unspecified. The caller (run.go) sorts findings before
// returning them, so internal iteration order doesn't leak into
// the report.
func flattenLeaves(m map[string]any) map[string]any {
	out := make(map[string]any)
	flattenInto(m, "", out)
	return out
}

func flattenInto(node map[string]any, prefix string, out map[string]any) {
	for k, v := range node {
		path := k
		if prefix != "" {
			path = prefix + "." + k
		}
		if nested, ok := v.(map[string]any); ok {
			// Empty submaps are skipped: PR-1's redundant-override
			// check rejects all map values via the structured-value
			// gate, so emitting an empty-map leaf would never produce
			// a finding anyway. If PR-2's routing checks need to
			// distinguish "explicitly set to {}" from "absent", add
			// a separate emit_empty option then.
			flattenInto(nested, path, out)
			continue
		}
		out[path] = v
	}
}
