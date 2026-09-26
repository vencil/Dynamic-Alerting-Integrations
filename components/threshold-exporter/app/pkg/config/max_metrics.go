package config

import "fmt"

// EffectiveMaxMetricsPerTenant maps a configured max_metrics_per_tenant to
// the cap the exporter enforces: 0 (unset) → DefaultMaxMetricsPerTenant;
// any other value as given. A result ≤ 0 means no truncation — resolve.go
// only truncates when the limit is > 0.
//
// ⛔ ONE READING. ResolveAtWithStats (the runtime truncation) and da-guard's
// PR-time prediction (#2043) both go through here; before #2043 da-guard was
// handed a hard-coded 500 by its CI workflow, which silently disagreed with
// the runtime as soon as #2028 made the configured value take effect.
func EffectiveMaxMetricsPerTenant(configured int) int {
	if configured == 0 {
		return DefaultMaxMetricsPerTenant
	}
	return configured
}

// RootMaxMetricsPerTenant returns the per-tenant cap the exporter enforces
// for the conf.d tree at configDir, and the path of the file it came from
// ("" when the root has no defaults carrier).
//
// It reads the ROOT defaults carrier the exporter's chain selects
// (rootDefaultsCarrier — same walker, same carrier choice) and decodes it
// with ParseConfigFile, the exporter's one decode. Only the root counts: in
// directory mode a max_metrics_per_tenant anywhere else is ignored (#2028).
//
// A root carrier that fails to decode is an error rather than the built-in
// default: the exporter would drop that whole file, so every prediction made
// from the tree would be wrong, not just the cap.
func RootMaxMetricsPerTenant(configDir string) (limit int, source string, err error) {
	path, data, ok := rootDefaultsCarrier(configDir)
	if !ok {
		return EffectiveMaxMetricsPerTenant(0), "", nil
	}
	cfg, err := ParseConfigFile(data)
	if err != nil {
		return 0, path, fmt.Errorf("parse root defaults %s: %w", path, err)
	}
	return EffectiveMaxMetricsPerTenant(cfg.MaxMetricsPerTenant), path, nil
}
