package main

import (
	"fmt"
	"log"
	"path/filepath"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// profileRoutingKeysOf lists, sorted, the routing-layer keys
// (config.IsRoutingLayerKey) the threshold profiles of the config being
// committed write (#2914). The profile expansion does not apply them
// (profileFill); this is what the ERROR and the gauge name.
//
// Directory mode reads every file the commit was merged from
// (flatScanState.configs, cached partials included, so an unchanged file
// counts on a reload exactly as on the cold load), like metadataStringFormOf:
// a tenant file's `profiles:` is already stripped there, and a nested `_`
// file is not in it. Single-file mode reads cfg, that file's own decode,
// named by its base name.
func profileRoutingKeysOf(cfg *ThresholdConfig, flatScan *flatScanState, path string) []config.ProfileRoutingKey {
	if flatScan == nil {
		return config.ProfileRoutingKeysOfConfig(filepath.Base(path), cfg)
	}
	return config.ProfileRoutingKeys(flatScan.configs)
}

// maxLoggedProfileRoutingKeys caps the entries one ERROR names; the gauge
// carries the full count.
const maxLoggedProfileRoutingKeys = 20

// publishProfileRoutingKeys sets da_config_profile_routing_keys and, when it
// is not 0, logs one ERROR for this commit naming file, profile and key.
func publishProfileRoutingKeys(metrics *configMetrics, logger *log.Logger, keys []config.ProfileRoutingKey) {
	metrics.SetProfileRoutingKeys(len(keys))
	if len(keys) == 0 {
		return
	}
	shown := keys
	more := ""
	if len(shown) > maxLoggedProfileRoutingKeys {
		shown = shown[:maxLoggedProfileRoutingKeys]
		more = fmt.Sprintf(" … and %d more", len(keys)-maxLoggedProfileRoutingKeys)
	}
	names := make([]string, 0, len(shown))
	for _, k := range shown {
		names = append(names, fmt.Sprintf("%s: profile %q key %s", k.File, k.Profile, k.Key))
	}
	logger.Printf("ERROR: %d routing-layer keys in threshold profiles are not applied (#2914): %s%s — "+
		"a threshold profile may not carry `_severity_dedup` or any `_routing*` key, because the route generator "+
		"never reads them from a profile; move `_routing` to `_routing_profiles.yaml` (elected by the tenant's "+
		"`_routing_profile`) or to the tenant's own `_routing`, and `_severity_dedup` to the tenant's own entry, "+
		"then delete them from the profile",
		len(keys), strings.Join(names, ", "), more)
}
