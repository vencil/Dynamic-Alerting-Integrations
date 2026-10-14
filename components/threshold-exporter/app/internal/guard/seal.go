package guard

import (
	"encoding/json"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
)

// sealed is r with every finding whose kind is not registered given
// findingkind.Unregistered() and severity error, re-sorted and re-summarised;
// n is how many findings changed. r itself is not modified.
//
// Every output of a report goes through it — MarshalJSON and Markdown — so
// what a report writes carries registered kinds only (findingkind's package
// comment says why that is the guarantee).
func (r GuardReport) sealed() (GuardReport, int) {
	n := 0
	for _, f := range r.Findings {
		if needsSeal(f) {
			n++
		}
	}
	if n == 0 {
		return r, 0
	}
	findings := make([]Finding, len(r.Findings))
	copy(findings, r.Findings)
	for i := range findings {
		if needsSeal(findings[i]) {
			findings[i].Kind = findingkind.Unregistered()
			findings[i].Severity = SeverityError
		}
	}
	return *newGuardReport(findings, r.Summary.TotalTenants), n
}

// needsSeal: f's kind is not registered, and f is not already the
// sentinel at severity error.
func needsSeal(f Finding) bool {
	return !f.Kind.IsRegistered() && (f.Kind != findingkind.Unregistered() || f.Severity != SeverityError)
}

// SealUnregisteredKinds applies sealed to r in place and returns how many
// findings changed, so a caller can name them and decide its exit code from
// the summary the output will show. The output is sealed whether or not it
// is called.
func (r *GuardReport) SealUnregisteredKinds() int {
	if r == nil {
		return 0
	}
	s, n := r.sealed()
	*r = s
	return n
}

// MarshalJSON writes the sealed report, in the shape the struct tags give.
func (r GuardReport) MarshalJSON() ([]byte, error) {
	type plain GuardReport // no MarshalJSON: no recursion
	s, _ := r.sealed()
	return json.Marshal(plain(s))
}
