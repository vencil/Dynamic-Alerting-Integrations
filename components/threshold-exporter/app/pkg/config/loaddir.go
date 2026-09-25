package config

import (
	"fmt"
	"log"
)

// RejectDuplicateTenant is the issue-#127 hard reject shared by every full
// load: a tenant declared in two files is a misconfiguration, and the
// caller propagates the typed error without committing any state. The
// walker records the conflict on the scan instead of failing the walk, so
// the flat products of a rejected tree are still available for diagnosis;
// this is the one place that turns the record back into the error callers
// unwrap with errors.As(&DuplicateTenantError{}).
//
// Historically the hierarchical scan was a SECOND walk that could also
// fail on its own (its non-duplicate errors were logged as
// "WARN: hierarchical scan during <loader> failed" and ignored, because
// hierarchical mode is opt-in). With one walk there is no second failure
// to tolerate: a tree the flat plane cannot read is a hard error for the
// load, as it always was.
func RejectDuplicateTenant(scan *TreeScan) error {
	if scan.Conflict != nil {
		return fmt.Errorf("config rejected (mixed-mode duplicate tenant): %w", scan.Conflict)
	}
	return nil
}

// LoadDir is a cold, stateless load of a conf.d directory: the ThresholdConfig
// the exporter's ConfigManager.Load() commits for the same tree (#1988). It
// runs the exporter's own steps in the exporter's order — one ScanDirTree with
// no prior, RejectDuplicateTenant, the empty-tree refusal, the subtree defaults
// chain and ParseDefaultsFiles the hierarchy plane derives from that scan, then
// BuildFlatConfig — so a reader outside package main gets the collector's
// config instead of a re-derivation of it.
//
// Directory mode only: the exporter's single-file mode (a path to one YAML
// file) is not a conf.d tree and is not served here.
//
// logger receives the walker's and the build's WARN/ERROR lines; nil discards
// them, because a library reader called per request would otherwise repeat
// them into its caller's log (see discardLogger). Nothing is counted: there is
// no ScanObserver.
//
// ⚠️ Cold means every file is read and decoded on every call. A caller serving
// requests should cache the result.
func LoadDir(dir string, logger *log.Logger) (*ThresholdConfig, error) {
	if logger == nil {
		logger = discardLogger
	}
	scan, err := ScanDirTree(dir, nil, nil, logger)
	if err != nil {
		return nil, err
	}
	if err := RejectDuplicateTenant(scan); err != nil {
		return nil, err
	}
	if len(scan.Files) == 0 {
		return nil, fmt.Errorf("no .yaml files found in %s", dir)
	}

	// Mirrors populateHierarchyStateFrom: a tree with neither a defaults file
	// nor a tenant installs no hierarchy state, so no subtree chain applies.
	var tenantDefaults map[string][]string
	var parsedDefaults map[string]map[string]any
	if len(scan.Defaults) > 0 || len(scan.Tenants) > 0 {
		tenantDefaults = scan.InheritanceGraph().TenantDefaults
		parsedDefaults = ParseDefaultsFiles(scan.Defaults, logger)
	}

	built, err := BuildFlatConfig(scan, FlatBuildInput{
		Root:           dir,
		TenantDefaults: tenantDefaults,
		ParsedDefaults: parsedDefaults,
		Logger:         logger,
	})
	if err != nil {
		return nil, err
	}
	return &built.Config, nil
}
