package main

import "time"

// configShape is the pair of whole-tree size maxima published as
// da_config_max_tenants_per_file and da_config_max_mapping_keys (#2153).
//
// Why these two numbers: load and reload time are dominated by YAML
// decoding, and yaml.v3 checks a mapping's keys for duplicates pairwise, so
// one mapping with K keys costs O(K²) to decode. The mapping that grows in
// practice is a file's `tenants:` block (a shared flat file declaring
// thousands of tenants) or one tenant's override map. Neither number is
// visible from the existing scan/reload histograms, which only show that a
// load was slow, not which shape made it so.
type configShape struct {
	maxTenantsPerFile int
	maxMappingKeys    int
}

// configShapeOf measures ONE file's decoded config.
//
// ⛔ LINEAR AND PARSE-FREE, BY CONSTRUCTION. It reads len() of the maps the
// decode already built — O(1) each — so its cost is O(tenants + profiles)
// map-header reads per file, with no allocation and no second decode. A
// yaml.Node walk would count mappings this misses (below), but it would be
// one more parse of every changed file on the reload path this metric
// exists to diagnose.
//
// ⚠️ It counts what the exporter DECODED, not every mapping in the bytes:
// unknown top-level keys, mappings inside a value (a schedule-form value,
// a routing block), nested `_`-prefixed files (never in the flat cache),
// and sections BuildFlatConfig's placement rules dropped are not counted.
// The large mappings the issue measured — `tenants:` and one tenant's
// overrides — are always counted.
func configShapeOf(cfg *ThresholdConfig) configShape {
	s := configShape{maxTenantsPerFile: len(cfg.Tenants)}
	s.maxMappingKeys = max(len(cfg.Defaults), len(cfg.StateFilters), len(cfg.Tenants), len(cfg.Profiles))
	for _, overrides := range cfg.Tenants {
		s.maxMappingKeys = max(s.maxMappingKeys, len(overrides))
	}
	for _, profile := range cfg.Profiles {
		s.maxMappingKeys = max(s.maxMappingKeys, len(profile))
	}
	return s
}

// configShapeOfFiles is configShapeOf's maximum over every file of a
// directory-mode commit (flatScanState.configs: every file the committed
// config was merged from, cached partials included, so an unchanged file
// counts on a reload exactly as on the cold load).
func configShapeOfFiles(configs map[string]ThresholdConfig) configShape {
	var out configShape
	for name := range configs {
		cfg := configs[name]
		s := configShapeOf(&cfg)
		out.maxTenantsPerFile = max(out.maxTenantsPerFile, s.maxTenantsPerFile)
		out.maxMappingKeys = max(out.maxMappingKeys, s.maxMappingKeys)
	}
	return out
}

// LoadInitial is the startup load: Load, timed into
// da_config_initial_load_duration_seconds (#2153).
//
// ⛔ A SEPARATE ENTRY, NOT A TIMER INSIDE Load. Load is also the watch
// loop's single-file reload (tickOnce, diffAndReload), so timing it there
// would overwrite the startup figure with every later reload. main calls
// this once; every other caller keeps calling Load.
func (m *ConfigManager) LoadInitial() error {
	t0 := time.Now()
	if err := m.Load(); err != nil {
		return err
	}
	m.getMetrics().SetInitialLoadDuration(time.Since(t0))
	return nil
}
