package handler

import (
	"fmt"
	"io"
	"math"
	"net/http"
)

// maxBodyKnob is the setting that raises d.MaxBody(); a 413 from
// readLimitedBody names it so the operator has something to act on.
const maxBodyKnob = "TA_MAX_BODY_BYTES"

// readLimitedBody reads r.Body bounded by d.MaxBody(). A body over that cap
// is answered with 413 (code PAYLOAD_TOO_LARGE, naming TA_MAX_BODY_BYTES); a
// read error with the canonical 400 "failed to read request body". Either way
// it returns ok=false so the caller can early-return; on success it returns
// the whole body and ok=true.
//
// ⛔ #2778: this used to be io.ReadAll(io.LimitReader(r.Body, d.MaxBody())),
// which stops at the cap WITHOUT an error. A YAML body whose first MaxBody
// bytes still parse was then written as if it were the whole request — the
// tail the client sent vanished from conf.d behind a 200. Do not go back to a
// bare LimitReader here: every caller parses what this returns.
func readLimitedBody(w http.ResponseWriter, r *http.Request, d *Deps) ([]byte, bool) {
	limit := d.MaxBody()
	return readBodyCapped(w, r, limit, func(got int) string {
		return fmt.Sprintf(
			"request body is at least %d bytes, over the %d-byte limit for this "+
				"endpoint; send a smaller body or raise %s",
			got, limit, maxBodyKnob)
	})
}

// readBodyWithin reads r.Body under an EXPLICIT byte limit and reports going
// over it with a 413 that explains the batch endpoints' cost. It is the batch
// endpoints' reader; readLimitedBody is every other write handler's. Both
// refuse an oversize body instead of truncating it (see readBodyCapped).
//
// 413 rather than 400 deliberately: the body is not malformed, it is too big,
// and the client's remedy (send less / ask for a bigger cap) differs.
func readBodyWithin(w http.ResponseWriter, r *http.Request, limit int64, knob string) ([]byte, bool) {
	return readBodyCapped(w, r, limit, func(got int) string {
		return fmt.Sprintf(
			"request body is at least %d bytes, over the %d-byte limit for this "+
				"endpoint — every operation in a batch is validated twice while the "+
				"single write lock is held, so an oversize batch delays every other "+
				"tenant's write; send fewer operations or raise %s",
			got, limit, knob)
	})
}

// readBodyCapped reads r.Body up to limit and answers 413 with tooLarge's
// message when the body is longer.
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
