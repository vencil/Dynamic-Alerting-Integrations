package handler

import (
	"fmt"
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

// ReadLimitedBody is readLimitedBody for handler sub-packages (federation):
// the same 413-on-over-cap read, so a body is never silently truncated.
//
// ⛔ The federation handlers read through a bare io.LimitReader until #2778's
// follow-up, so an over-cap body there was a 400 "invalid JSON" naming neither
// the size nor the cap. forbidigo now rejects reading r.Body, and naming
// io.LimitReader / io.ReadAll, anywhere in internal/handler except
// read_body_capped.go (.golangci.yml, rules tagged #2778).
func ReadLimitedBody(w http.ResponseWriter, r *http.Request, d *Deps) ([]byte, bool) {
	return readLimitedBody(w, r, d)
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
