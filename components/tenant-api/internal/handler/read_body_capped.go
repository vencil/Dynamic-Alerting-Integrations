package handler

import (
	"io"
	"math"
	"net/http"
)

// readBodyCapped reads r.Body up to limit and answers 413 with tooLarge's
// message when the body is longer. readLimitedBody and readBodyWithin are its
// two named callers; an endpoint with a cap of its own (recipe preview's
// previewMaxBody) calls it directly.
//
// ⛔ It sits alone in this file because this file is the ONE place in
// internal/handler that forbidigo lets read r.Body or name io.LimitReader /
// io.ReadAll (.golangci.yml, rules tagged #2778). Put nothing else here: the
// exemption covers the whole file, and what is kept out stays under the rule.
//
// ⛔ THE POINT IS DETECTING THE OVERFLOW. A bare io.LimitReader returns a short
// read at the cap with no error — the caller then either fails somewhere
// downstream ("invalid JSON: unexpected EOF") naming neither the size nor the
// limit, or, for a format whose prefix still parses (YAML), goes on with the
// truncated body as if it were whole (#2778). This reads one byte past the
// limit so exceeding it is DETECTABLE.
func readBodyCapped(w http.ResponseWriter, r *http.Request, limit int64, tooLarge func(got int) string) ([]byte, bool) {
	// ⛔ limit+1 OVERFLOWS AT MaxInt64, and the failure is silent in the worst
	// possible direction: io.LimitReader on a negative n returns EOF at once, so
	// len(body)==0 is NOT greater than limit and the empty read is ACCEPTED —
	// every request then dies downstream as "invalid JSON: unexpected end of
	// JSON input", the exact unactionable message this function exists to
	// replace. An operator reaching for MaxInt64 is asking to disable the cap,
	// so honour that reading rather than rejecting it.
	probe := limit
	if probe < math.MaxInt64 {
		probe++
	}
	body, err := io.ReadAll(io.LimitReader(r.Body, probe))
	if err != nil {
		WriteJSONError(w, r, http.StatusBadRequest, "failed to read request body: "+err.Error())
		return nil, false
	}
	if int64(len(body)) > limit {
		// ⚠️ "at least", not the exact size: this reads only limit+1 bytes, so
		// the real length is unknowable here by construction. Saying a precise
		// number would be a lie, and reading the whole body to learn it would
		// reinstate the memory cost the cap exists to prevent.
		WriteJSONError(w, r, http.StatusRequestEntityTooLarge, tooLarge(len(body)))
		return nil, false
	}
	return body, true
}
