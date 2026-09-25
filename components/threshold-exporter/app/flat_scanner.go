package main

// Single-file loader, and the package-main names of the flat-plane helpers.
//
// The parse helpers, the multi-file merge and the build of the collector's
// ThresholdConfig moved to pkg/config/flat_build.go in #1988 (the directory
// walk had already moved to pkg/config/tree_scan.go in #1941). The functions
// below keep the names the reload paths (config.go) and the tests call; every
// forwarder is `return config.X(args...)`, no extra logic.
//
//   loadFile(path)           — single YAML file → ThresholdConfig + hash.
//                              Directory mode has no separate eager loader:
//                              Load delegates to fullDirLoad (config.go) so
//                              the initial load and the watch loop share one
//                              composite-hash construction + per-file cache.

import (
	"crypto/sha256"
	"fmt"
	"log"
	"os"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// loadFile reads a single YAML config file and returns the parsed config + content hash.
func loadFile(path string) (ThresholdConfig, string, error) {
	var cfg ThresholdConfig

	data, err := os.ReadFile(path)
	if err != nil {
		return cfg, "", fmt.Errorf("read config %s: %w", path, err)
	}

	hash := fmt.Sprintf("%x", sha256.Sum256(data))

	cfg, err = config.ParseConfigFile(data)
	if err != nil {
		return cfg, "", fmt.Errorf("parse config %s: %w", path, err)
	}

	return cfg, hash, nil
}

func scanKeyBase(key string) string { return config.ScanKeyBase(key) }

func isNestedPlatformFile(key string) bool { return config.IsNestedPlatformFile(key) }

func reportUnparseableNestedPlatformFile(fullPath string, data []byte, metrics *configMetrics, logger *log.Logger) (any, bool) {
	return config.ReportUnparseableNestedPlatformFile(fullPath, data, scanObserverFor(metrics), logger)
}

func reportNestedPlatformTenants(name string, probe any, logger *log.Logger) {
	config.ReportNestedPlatformTenants(name, probe, logger)
}

func parsePartialConfig(name, path string, data []byte, metrics *configMetrics, logger *log.Logger) (ThresholdConfig, bool) {
	return config.ParsePartialConfig(name, path, data, scanObserverFor(metrics), logger)
}

func applyBoundaryRules(name string, partial *ThresholdConfig, logger *log.Logger) {
	config.ApplyBoundaryRules(name, partial, logger)
}

func mergePartialConfigs(configs map[string]ThresholdConfig, exists map[string]struct{}) ThresholdConfig {
	return config.MergePartialConfigs(configs, exists)
}

func isPlatformKey(key string) bool { return config.IsPlatformKey(key) }

func sortFlatMergeOrder(names []string) { config.SortFlatMergeOrder(names) }

func tenantExistenceFor(configs map[string]ThresholdConfig, scan *treeScan) map[string]struct{} {
	return config.TenantExistenceFor(configs, scan)
}

func reportPlatformOrphans(configs map[string]ThresholdConfig, exists map[string]struct{}, logger *log.Logger) {
	config.ReportPlatformOrphans(configs, exists, logger)
}

func mergePartialInto(merged *ThresholdConfig, partial ThresholdConfig) {
	config.MergePartialInto(merged, partial)
}

func anyRootCarrierKey(groups ...[]string) bool { return config.AnyRootCarrierKey(groups...) }
