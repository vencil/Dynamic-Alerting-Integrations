package handler

import (
	"fmt"
	"io"
	"math"
	"net/http"
)

// readLimitedBody reads r.Body bounded by d.MaxBody(). On a read error it
// writes the canonical 400 "failed to read request body" JSON envelope to w
// and returns ok=false so the caller can early-return; on success it returns
// the read bytes and ok=true.
//
// Extracted from seven in-package handlers that shared a byte-identical
// read-and-400 block (PR-1 Wave C6, behavior-preserving).
func readLimitedBody(w http.ResponseWriter, r *http.Request, d *Deps) ([]byte, bool) {
	body, err := io.ReadAll(io.LimitReader(r.Body, d.MaxBody()))
	if err != nil {
		WriteJSONError(w, r, http.StatusBadRequest, "failed to read request body: "+err.Error())
		return nil, false
	}
	return body, true
}

// readBodyWithin reads r.Body under an EXPLICIT byte limit and, unlike
// readLimitedBody, reports going over it instead of truncating.
//
// ⛔ THE DIFFERENCE IS THE WHOLE POINT. readLimitedBody wraps io.LimitReader,
// which returns a short read at the cap with no error — the caller then fails
// somewhere downstream ("invalid JSON: unexpected EOF") naming neither the size
// nor the limit, and an operator hitting a body cap has nothing to act on. This
// reads one byte past the limit so exceeding it is DETECTABLE, and answers with
// 413 + the two numbers that make it self-diagnosing.
//
// 413 rather than 400 deliberately: the body is not malformed, it is too big,
// and the client's remedy (send less / ask for a bigger cap) differs.
func readBodyWithin(w http.ResponseWriter, r *http.Request, limit int64, knob string) ([]byte, bool) {
	// ⛔ limit+1 OVERFLOWS AT MaxInt64, and the failure is silent in the worst
	// possible direction: io.LimitReader on a negative n returns EOF at once, so
	// len(body)==0 is NOT greater than limit and the empty read is ACCEPTED —
	// every batch request then dies downstream as "invalid JSON: unexpected end
	// of JSON input", the exact unactionable message this function exists to
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
		WriteJSONError(w, r, http.StatusRequestEntityTooLarge, fmt.Sprintf(
			"request body is at least %d bytes, over the %d-byte limit for this "+
				"endpoint — every operation in a batch is validated twice while the "+
				"single write lock is held, so an oversize batch delays every other "+
				"tenant's write; send fewer operations or raise %s",
			len(body), limit, knob))
		return nil, false
	}
	return body, true
}
