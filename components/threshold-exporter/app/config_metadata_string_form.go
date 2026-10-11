package main

import (
	"log"
	"sort"
	"strings"
)

// metadataStringFormOf lists, sorted, the entries of the config being
// committed that write `_metadata` as a string (#2859), as
// "<file>: tenants.<id>" (directory mode) or "tenants.<id>" (single file).
//
// Directory mode reads every file the commit was merged from
// (flatScanState.configs, cached partials included, so an unchanged file
// counts on a reload exactly as on the cold load), like configShapeOfFiles.
// Single-file mode reads cfg, which is that file's own decode.
//
// The exporter keeps reading the string form: it parses the text as YAML,
// as it always has. Refusing the file at load would drop every tenant it
// declares; the write side refuses the form instead.
func metadataStringFormOf(cfg *ThresholdConfig, flatScan *flatScanState) []string {
	if flatScan == nil {
		return append([]string(nil), cfg.StringMetadataEntries()...)
	}
	var out []string
	for name := range flatScan.configs {
		c := flatScan.configs[name]
		for _, e := range c.StringMetadataEntries() {
			out = append(out, name+": "+e)
		}
	}
	sort.Strings(out)
	return out
}

// maxLoggedStringMetadata caps the entries one WARN names; the gauge
// carries the full count.
const maxLoggedStringMetadata = 20

// publishMetadataStringForm sets da_config_metadata_string_form and, when it
// is not 0, WARNs once for this commit naming the entries.
func publishMetadataStringForm(metrics *configMetrics, logger *log.Logger, entries []string) {
	metrics.SetMetadataStringForm(len(entries))
	if len(entries) == 0 {
		return
	}
	shown := entries
	more := ""
	if len(shown) > maxLoggedStringMetadata {
		shown = shown[:maxLoggedStringMetadata]
		more = " …"
	}
	logger.Printf("WARN: %d config entries write _metadata as a string, not a mapping (#2859): %s%s — still read, but tenant-api refuses to write this form and the conf.d schema lint fails on it; rewrite each as a mapping (environment: …, owner: …)",
		len(entries), strings.Join(shown, ", "), more)
}
