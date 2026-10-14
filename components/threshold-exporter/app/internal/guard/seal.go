package guard

import "github.com/vencil/threshold-exporter/internal/guard/findingkind"

// SealUnregisteredKinds replaces the kind of every finding whose kind is not
// a registered one — the zero Kind, which a check that chose no kind leaves,
// or a Kind no register call made — with findingkind.Unregistered(), makes
// that finding an error, and recomputes the order and the summary. It
// returns how many findings it changed.
//
// It is for the CLI's output boundary (#2904): findingkind.Kind's
// MarshalJSON refuses such a kind, which used to fail the whole JSON report
// (exit 2, nothing written to --output) and leave an empty kind column in
// Markdown. A finding whose kind is unknown is still a finding the run
// cannot vouch for, so it blocks (exit 1) instead of disappearing.
func (r *GuardReport) SealUnregisteredKinds() int {
	if r == nil {
		return 0
	}
	n := 0
	for i := range r.Findings {
		if r.Findings[i].Kind.IsRegistered() {
			continue
		}
		r.Findings[i].Kind = findingkind.Unregistered()
		r.Findings[i].Severity = SeverityError
		n++
	}
	if n > 0 {
		*r = *newGuardReport(r.Findings, r.Summary.TotalTenants)
	}
	return n
}
