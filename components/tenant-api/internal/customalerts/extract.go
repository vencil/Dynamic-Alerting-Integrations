package customalerts

import "gopkg.in/yaml.v3"

// Extract returns the tenant's `_custom_alerts` list as a slice of generic
// maps (ADR-024 §S6b-2). The portal RecipeBuilder modal reads this JSON
// directly instead of parsing the (subset-lossy) YAML client-side — the
// backend owns the YAML round-trip on BOTH the read (here) and the write
// (MergeCustomAlerts), so the client only ever handles JSON.
//
// Returns an empty slice (not nil) when the tenant has no `_custom_alerts`.
// A YAML parse error is returned; a tenant simply absent from the doc yields
// an empty slice, not an error (GET surfaces presence via the 404 path).
func Extract(rawYAML, tenantID string) ([]map[string]any, error) {
	var doc struct {
		Tenants map[string]map[string]any `yaml:"tenants"`
	}
	if err := yaml.Unmarshal([]byte(rawYAML), &doc); err != nil {
		return nil, err
	}
	out := []map[string]any{}
	tenant, ok := doc.Tenants[tenantID]
	if !ok {
		return out, nil
	}
	raw, ok := tenant["_custom_alerts"]
	if !ok {
		return out, nil
	}
	list, ok := raw.([]any)
	if !ok {
		return out, nil
	}
	for _, item := range list {
		// yaml.v3 decodes nested maps as map[string]any already.
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out, nil
}
