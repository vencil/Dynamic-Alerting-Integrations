package handler

import (
	"errors"
	"net/http"
)

// parseAccessReportProjection parses the strict projection query parameters
// shared by the two access-report endpoints — GET .../access-report and POST
// .../access-report/dry-run:
//
//   - include=org_values — opt-in expansion of DERIVED org identifiers.
//   - view=full|redacted (default full) — the report projection.
//
// Both are STRICT: an unrecognized value is an error (never silently ignored),
// so a typo'd ?view=redcated cannot fail open to the FULL report. The returned
// error carries the verbatim client-facing message; callers render it as a 400
// via WriteJSONError. On error the returned bools are unset and must not be
// used.
func parseAccessReportProjection(r *http.Request) (includeOrgValues bool, redacted bool, err error) {
	switch r.URL.Query().Get("include") {
	case "":
	case "org_values":
		includeOrgValues = true
	default:
		return false, false, errors.New("unsupported include value: only org_values is recognized")
	}
	switch r.URL.Query().Get("view") {
	case "", "full":
	case "redacted":
		redacted = true
	default:
		return false, false, errors.New("unsupported view value: full or redacted")
	}
	return includeOrgValues, redacted, nil
}
