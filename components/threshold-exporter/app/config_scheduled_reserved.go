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
// hierarchy's parsedDefaults), judged by the same function and split by
// what the overlay does with each (config.SubtreeScheduledReserved, the
// verdict applySubtreeDefaults acts on), as "<file>: defaults.<key>":
// applied as its `default` alone (defaultOnly, beside the tenant entries),
// or not applied at all (notApplied).
func scheduledReservedOf(cfg *ThresholdConfig, flatScan *flatScanState, root string, subtree map[string]map[string]any) (defaultOnly, notApplied []string) {
	if flatScan == nil {
		return append([]string(nil), cfg.ScheduledReservedEntries()...), nil
	}
	defaultOnly, notApplied = config.SubtreeScheduledReserved(root, subtree, cfg)
	for name := range flatScan.configs {
		c := flatScan.configs[name]
		for _, e := range c.ScheduledReservedEntries() {
			defaultOnly = append(defaultOnly, name+": "+e)
		}
	}
	sort.Strings(defaultOnly)
	return defaultOnly, notApplied
}

// maxLoggedScheduledReserved caps the entries one WARN names; the count in
// it is whole.
const maxLoggedScheduledReserved = 20

// scheduledReservedRemedy ends both WARN lines.
const scheduledReservedRemedy = "Remove them (tenant-api refuses to write this form; for a recurring maintenance " +
	"window use `_state_maintenance` with `recurring:`)"

// warnScheduledReserved WARNs once for this commit per effect
// (scheduledReservedOf), each line when it names any entry: the entries
// whose `default` alone is used — what is served did not change, the
// readers read the `default` alone before #3269 as after — and the subtree
// entries the overlay does not apply at all, schedule or not.
func warnScheduledReserved(logger *log.Logger, defaultOnly, notApplied []string) {
	if len(defaultOnly) > 0 {
		logger.Printf("WARN: %d config entries write a reserved key with a time window or expires "+
			"(`overrides:` / `expires:` / `reason:`, #3269): %s — a reserved key does not support them: "+
			"only each value's `default` is used and the rest is ignored. %s",
			len(defaultOnly), cappedList(defaultOnly), scheduledReservedRemedy)
	}
	if len(notApplied) > 0 {
		logger.Printf("WARN: %d subtree defaults entries write a reserved key with a time window or expires "+
			"(`overrides:` / `expires:` / `reason:`, #3269): %s — not applied at all: a subtree `_defaults.yaml` "+
			"does not hand this value down to its tenants, `default` included. %s",
			len(notApplied), cappedList(notApplied), scheduledReservedRemedy)
	}
}

// cappedList is entries joined, at most maxLoggedScheduledReserved of them.
func cappedList(entries []string) string {
	if len(entries) > maxLoggedScheduledReserved {
		return strings.Join(entries[:maxLoggedScheduledReserved], ", ") + " …"
	}
	return strings.Join(entries, ", ")
}
