package main

// Subtree defaults on the OUTPUT plane (#1521, second half) moved to
// pkg/config/subtree_defaults.go (#1988), together with its design notes, so
// the one build of the collector's ThresholdConfig can be shared. These are
// the package-main names the reload paths and tests call; every one is
// `return config.X(args...)`, no extra logic.

import "github.com/vencil/threshold-exporter/pkg/config"

func applySubtreeDefaults(
	cfg *ThresholdConfig,
	root string,
	tenantDefaults map[string][]string,
	parsed map[string]map[string]any,
) (int, map[string][]string) {
	return config.ApplySubtreeDefaults(cfg, root, tenantDefaults, parsed)
}

func scheduledValueFromRaw(raw any) (ScheduledValue, bool) {
	return config.ScheduledValueFromRaw(raw)
}
