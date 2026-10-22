package main

import (
	"log"
	"sort"
	"strings"
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
// ⚠️ A subtree `_defaults.yaml` is not a partial: a schedule it writes on
// `_silent_mode` / `_severity_dedup` / a `_state_` key is dropped by the
// subtree overlay the same way (servedValue) without a line here — da-guard
// already names every reserved key in a subtree defaults file
// (subtree_default_reserved_key).
func scheduledReservedOf(cfg *ThresholdConfig, flatScan *flatScanState) []string {
	if flatScan == nil {
		return append([]string(nil), cfg.ScheduledReservedEntries()...)
	}
	var out []string
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
		"only each value's `default` is read and the rest is ignored. Remove them (tenant-api refuses "+
		"to write this form; for a recurring maintenance window use `_state_maintenance` with `recurring:`)",
		len(entries), strings.Join(shown, ", "), more)
}
