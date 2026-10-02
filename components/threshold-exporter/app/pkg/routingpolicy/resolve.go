package routingpolicy

import (
	"fmt"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// Provenance names, per top-level key of a resolved routing, the layer the
// value came from: SourceTenant, SourceDefaults, or "profile:<name>"
// (ProfileSource).
type Provenance map[string]string

// Layer names used in Provenance.
const (
	SourceTenant   = "tenant"
	SourceDefaults = "_routing_defaults"
)

// ProfileSource is the Provenance value of a key a routing profile supplied.
func ProfileSource(name string) string { return "profile:" + name }

// Describe renders a Provenance value for an operator message.
func Describe(source string) string {
	switch {
	case source == SourceTenant:
		return "the tenant's _routing"
	case source == SourceDefaults:
		return "_routing_defaults"
	case strings.HasPrefix(source, "profile:"):
		return fmt.Sprintf("routing profile '%s'", strings.TrimPrefix(source, "profile:"))
	}
	return "an unknown layer"
}

// IsDisabled reports whether v is a string that turns a feature off
// (pkg/config.IsDisabled after lower-casing and trimming — the set
// _lib_validation.is_disabled uses too).
func IsDisabled(v any) bool {
	s, ok := v.(string)
	return ok && config.IsDisabled(strings.ToLower(strings.TrimSpace(s)))
}

// Resolve merges one tenant's routing from the three layers, as
// _grar_parse._merge_tenant_routing does:
//
//  1. base = `_routing_defaults`;
//  2. the referenced profile's top-level keys, each replacing base's;
//  3. the tenant's `_routing` top-level keys, each replacing again;
//  4. `{{tenant}}` replaced in every string value.
//
// The merge is SHALLOW: `receiver`, `overrides`, `routes`, `group_by` are
// replaced whole, so a tenant's `routes: []` drops the profile's routes.
//
// block is the tenant's config block (its `_routing` and `_routing_profile`
// keys are read). ok=false: the tenant has no routing — `_routing` is a
// disabling string, is written but is no mapping (RoutingNotMapping, #2341),
// or no layer supplies anything. unknownProfile is the
// referenced profile name, trimmed, when no profile has that name (nil
// otherwise); it is reported even when ok=false, like the Python reader's
// WARN. A reference is any non-empty string, so `"   "` is a reference to the
// profile named "" — unknown, as in Python (`rp_ref.strip()`).
//
// The result shares nothing with block or l.
func Resolve(tenantID string, block map[string]any, l Layers) (resolved map[string]any, ok bool, prov Provenance, unknownProfile *string) {
	// "Is there a reference" (a non-empty string, Python truthiness) and
	// "what does it name" (trimmed) are two questions: a whitespace-only
	// value is a reference that names "".
	raw, isStr := block["_routing_profile"].(string)
	hasRef := isStr && raw != ""
	ref := strings.TrimSpace(raw)
	var profile map[string]any
	if hasRef {
		p, known := l.Profiles[ref]
		if !known {
			unknownProfile = &ref
		}
		profile = p
	}

	routing, written := block["_routing"]
	if IsDisabled(routing) {
		return nil, false, nil, unknownProfile
	}
	// #2341 R5: a `_routing` that is neither a mapping nor a disabling
	// string renders nothing — not the defaults route (RoutingNotMapping).
	if written && RoutingNotMapping(routing) {
		return nil, false, nil, unknownProfile
	}

	merged := map[string]any{}
	prov = Provenance{}
	for k, v := range l.Defaults {
		merged[k] = v
		prov[k] = SourceDefaults
	}
	for k, v := range profile {
		merged[k] = v
		prov[k] = ProfileSource(ref)
	}
	if tr, isMap := asStringMap(routing); isMap {
		for k, v := range tr {
			merged[k] = v
			prov[k] = SourceTenant
		}
	}
	if len(merged) == 0 {
		return nil, false, nil, unknownProfile
	}
	return substituteTenant(merged, tenantID).(map[string]any), true, prov, unknownProfile
}

// substituteTenant returns a deep copy of v with `{{tenant}}` replaced in
// every string value (keys are left alone), as _grar_merge._substitute_tenant.
func substituteTenant(v any, tenantID string) any {
	switch t := v.(type) {
	case string:
		return strings.ReplaceAll(t, "{{tenant}}", tenantID)
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, e := range t {
			out[k] = substituteTenant(e, tenantID)
		}
		return out
	case map[any]any:
		out := make(map[any]any, len(t))
		for k, e := range t {
			out[k] = substituteTenant(e, tenantID)
		}
		return out
	case []any:
		out := make([]any, len(t))
		for i, e := range t {
			out[i] = substituteTenant(e, tenantID)
		}
		return out
	}
	return v
}

// asStringMap returns v as a map[string]any when it is a YAML mapping whose
// keys are all strings (a copy for map[any]any). Nothing else is a mapping
// here.
func asStringMap(v any) (map[string]any, bool) {
	switch t := v.(type) {
	case map[string]any:
		out := make(map[string]any, len(t))
		for k, e := range t {
			out[k] = e
		}
		return out, true
	case map[any]any:
		out := make(map[string]any, len(t))
		for k, e := range t {
			s, ok := k.(string)
			if !ok {
				return nil, false
			}
			out[s] = e
		}
		return out, true
	}
	return nil, false
}
