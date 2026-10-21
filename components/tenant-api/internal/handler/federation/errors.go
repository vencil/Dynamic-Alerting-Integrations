package federation

import (
	"net/http"

	"github.com/vencil/tenant-api/internal/handler"
)

// writeFederationGitError maps the error of a federation registry write
// (token account-id allocation, fleet backfill) to its HTTP response. Those
// writes go through the account allocator and the state store
// (account.Allocator over statestore.Store), never the git Writer, so the
// errors are the state store's — 409 / 503 + Retry-After / 507 via
// handler.WriteStateError — and anything else is a 500 prefixed with msg500
// (its text goes to the log only — handler.WriteErrorEnvelope, #1700). It
// always writes exactly one response, so callers invoke it inside
// `if err != nil { ...; return }`.
func writeFederationGitError(w http.ResponseWriter, r *http.Request, err error, msg500 string) {
	if handler.WriteStateError(w, r, err) {
		return
	}
	handler.WriteJSONError(w, r, http.StatusInternalServerError, msg500+err.Error())
}
