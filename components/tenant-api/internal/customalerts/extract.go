package customalerts

import (
	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/confedit"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// Extract returns the tenant's `_custom_alerts` list as a slice of generic
// maps (ADR-024 §S6b-2). The portal RecipeBuilder modal reads this JSON
// directly instead of parsing the (subset-lossy) YAML client-side — the
// backend owns the YAML round-trip on BOTH the read (here) and the write
// (MergeCustomAlerts), so the client only ever handles JSON.
//
// Returns an empty slice (not nil) when the tenant has no `_custom_alerts`.
// A YAML parse error is returned; a tenant simply absent from the doc yields
// an empty slice, not an error (GET surfaces presence via the 404 path).
//
// #3370: each `threshold:` is the exporter's reading of the text the file
// wrote (config.CustomAlertThresholdTexts) — a bare `017` is "017", not
// YAML's 15 — so what the portal shows, and writes back on a save, is what
// the exporter serves.
func Extract(rawYAML, tenantID string) ([]map[string]any, error) {
	var doc struct {
		Tenants map[string]map[string]yaml.Node `yaml:"tenants"`
	}
	if err := yaml.Unmarshal([]byte(rawYAML), &doc); err != nil {
		return nil, err
	}
	out := []map[string]any{}
	tenant, ok := doc.Tenants[tenantID]
	if !ok {
		return out, nil
	}
	node, ok := tenant[confedit.CustomAlertsKey]
	if !ok {
		return out, nil
	}
	var raw any
	if err := node.Decode(&raw); err != nil {
		return nil, err
	}
	list, ok := raw.([]any)
	if !ok {
		return out, nil
	}
	seq := &node
	for seq.Kind == yaml.AliasNode && seq.Alias != nil {
		seq = seq.Alias
	}
	config.CustomAlertThresholdTexts(list, seq)
	for _, item := range list {
		// yaml.v3 decodes nested maps as map[string]any already.
		if m, ok := item.(map[string]any); ok {
			out = append(out, m)
		}
	}
	return out, nil
}
