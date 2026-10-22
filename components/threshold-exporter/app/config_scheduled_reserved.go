package main

import (
	"log"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// scheduledReservedOf lists, sorted, the entries of the config being
// committed that write a reserved key with a schedule (#3269), as
// "<file>: tenants.<id>.<key>" (directory mode) or "tenants.<id>.<key>"
// (single file) — the entries whose schedule the decode dropped, keeping
// only the `default` every reader of the key reads
// (config.ThresholdConfig.ScheduledReservedEntries).
//
// Read like metadataStringFormOf: directory mode reads every file the
// commit was merged from (flatScanState.configs, cached partials included,
// so an unchanged file is named on a reload as on the cold load); single
// file mode reads cfg, which is that file's own decode.
//
// A subtree `_defaults.yaml` is not a partial: its entries come from the
// defaults-chain cache the overlay reads (subtree, the committed
// hierarchy's parsedDefaults), judged by the same function
// (config.SubtreeScheduledReserved), as "<file>: defaults.<key>".
func scheduledReservedOf(cfg *ThresholdConfig, flatScan *flatScanState, root string, subtree map[string]map[string]any) []string {
	if flatScan == nil {
		return append([]string(nil), cfg.ScheduledReservedEntries()...)
	}
	out := config.SubtreeScheduledReserved(root, subtree)
	for name := range flatScan.configs {
		c := flatScan.configs[name]
		for _, e := range c.ScheduledReservedEntries() {
			out = append(out, name+": "+e)
		}
	}
	sort.Strings(out)
	return out
}

// maxLoggedScheduledReserved caps the entries one WARN names; the count in
// it is whole.
const maxLoggedScheduledReserved = 20

// warnScheduledReserved WARNs once for this commit naming the entries
// (scheduledReservedOf), when there are any. What is served does not change:
// the readers read the `default` alone, before #3269 as after.
func warnScheduledReserved(logger *log.Logger, entries []string) {
	if len(entries) == 0 {
		return
	}
	shown, more := entries, ""
	if len(shown) > maxLoggedScheduledReserved {
		shown, more = shown[:maxLoggedScheduledReserved], " …"
	}
	logger.Printf("WARN: %d config entries write a reserved key with a time window or expires "+
		"(`overrides:` / `expires:` / `reason:`, #3269): %s%s — a reserved key does not support them: "+
		"only each value's `default` is ever read and the rest is ignored. Remove them (tenant-api refuses "+
		"to write this form; for a recurring maintenance window use `_state_maintenance` with `recurring:`)",
		len(entries), strings.Join(shown, ", "), more)
}
