package guard

import (
	"encoding/json"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
)

// A report carries registered kinds only (findingkind's package comment says
// why that is the guarantee): newGuardReport gives every finding whose kind
// is not registered findingkind.Unregistered() and severity error before it
// sorts and summarises, so the summary, and an exit code taken from it,
// count those findings as written. MarshalJSON and Markdown seal again, for
// a report built as a literal or changed after it was built; on a report
// newGuardReport built that changes nothing and counts nothing.

// needsSeal: f's kind is not registered, and f is not already the
// sentinel at severity error.
func needsSeal(f Finding) bool {
	return !f.Kind.IsRegistered() && (f.Kind != findingkind.Unregistered() || f.Severity != SeverityError)
}

// sealed is r rebuilt by newGuardReport when some finding needs sealing,
// else r itself. r is not modified.
func (r GuardReport) sealed() GuardReport {
	for _, f := range r.Findings {
		if needsSeal(f) {
			return *newGuardReport(append([]Finding(nil), r.Findings...), r.Summary.TotalTenants)
		}
	}
	return r
}

// UnregisteredKinds is how many findings were written as
// findingkind.Unregistered() when the report was built.
func (r *GuardReport) UnregisteredKinds() int {
	if r == nil {
		return 0
	}
	return r.unregistered
}

// MarshalJSON writes the sealed report, in the shape the struct tags give.
func (r GuardReport) MarshalJSON() ([]byte, error) {
	type plain GuardReport // no MarshalJSON: no recursion
	return json.Marshal(plain(r.sealed()))
}
