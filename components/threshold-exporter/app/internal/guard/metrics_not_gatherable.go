package guard

// A tree whose /metrics cannot be gathered (#2031).
//
// When two keys of the config give one series — `X: "70:critical"` beside
// `X_critical`, a regex label `q=~` beside an exact label `q_re=`, or any
// other shape the decode does not fold into one key — client_golang refuses
// the whole Gather and the exporter's /metrics answers HTTP 500 for every
// tenant. The main gate let such a tree through (rc 0).
//
// ⛔ NO VERDICT OF ITS OWN: it is a Gather of the exporter's own collectors
// over the exporter's build of the WHOLE tree, whatever
// --scope says (one such pair fails the scrape for every tenant), handed in
// as CheckInput.MetricsNotGatherable.

// FindingMetricsNotGatherable (error; #2031): the exporter's /metrics cannot
// be gathered for the tree.
const FindingMetricsNotGatherable FindingKind = "metrics_not_gatherable"

// checkMetricsNotGatherable reports one finding, naming no tenant, when
// input.MetricsNotGatherable is set.
func checkMetricsNotGatherable(input CheckInput) []Finding {
	if input.MetricsNotGatherable == "" {
		return nil
	}
	return []Finding{{
		Severity: SeverityError,
		Kind:     FindingMetricsNotGatherable,
		Message:  input.MetricsNotGatherable,
	}}
}
