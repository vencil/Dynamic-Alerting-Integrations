package main

import (
	"fmt"
	"math"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// capChangeNotices reports a per-tenant cap that this change RELAXES (#2043
// review follow-up).
//
// Since #2043 the cardinality check predicts against the cap the tree itself
// sets, so a change that raises the root max_metrics_per_tenant — or disables
// it with a negative value — is judged against the very cap it introduces.
// That is correct as a prediction (the exporter will enforce the new cap), but
// it means the guard can no longer be what catches the raise. The notice puts
// the raise at the top of the report so a reviewer sees it; whether it needs
// sign-off is a policy question, so it never changes the exit code.
//
// Only a relaxation is reported: a lowered cap makes the check stricter and
// shows up as ordinary findings. The comparison uses the same reading as the
// exporter (config.RootMaxMetricsPerTenant) on both sides, and ignores an
// explicit --cardinality-limit — the question is what the tree changes at
// runtime, not what this run checked against.
func capChangeNotices(f *flags) []string {
	if f.baselineConfigDir == "" {
		return nil
	}
	head, _, herr := config.RootMaxMetricsPerTenant(f.configDir)
	base, _, berr := config.RootMaxMetricsPerTenant(f.baselineConfigDir)
	if herr != nil || berr != nil {
		// Never silent: "could not compare" must not read as "not raised".
		return []string{fmt.Sprintf(
			"Could not compare the per-tenant cardinality cap with the baseline: %v",
			firstErr(herr, berr))}
	}
	if capRank(head) <= capRank(base) {
		return nil
	}
	return []string{fmt.Sprintf(
		"**Per-tenant cardinality cap relaxed by this change: %s → %s** "+
			"(root `_defaults.yaml` `max_metrics_per_tenant`). The cardinality check below uses the new cap, "+
			"as the exporter will; it cannot flag this raise itself, so confirm the raise is intended.",
		capLabel(base), capLabel(head))}
}

// capRank orders caps by how much they allow: no limit (≤ 0) is the loosest.
func capRank(limit int) int {
	if limit <= 0 {
		return math.MaxInt
	}
	return limit
}

func capLabel(limit int) string {
	if limit <= 0 {
		return "no limit"
	}
	return fmt.Sprintf("%d", limit)
}

func firstErr(errs ...error) error {
	for _, err := range errs {
		if err != nil {
			return err
		}
	}
	return nil
}
