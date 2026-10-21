package federation

import (
	"net/http"

	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/handler"
)

// writeFederationGitError maps the error of a federation registry write
// (token account-id allocation, fleet backfill) to its HTTP response. Those
// writes go through the account allocator and the state store
// (account.Allocator over statestore.Store), never the git Writer, so the
// errors are the state store's — 409 / 503 + Retry-After / 507, the error
// kind table's answers (handler.WriteKindError) — and anything else is a 500
// prefixed with msg500 (its text goes to the log only —
// handler.WriteErrorEnvelope, #1700). It always writes exactly one response,
// so callers invoke it inside `if err != nil { ...; return }`.
func writeFederationGitError(w http.ResponseWriter, r *http.Request, err error, msg500 string) {
	handler.WriteKindError(w, r, err, msg500,
		errkind.StateConflict, errkind.StateUnavailable, errkind.StateFull)
}

// writeIssueError answers a federation token mint that Issue / IssueLogs
// refused: the tenant's token limit (409 TOKEN_LIMIT_REACHED), the mint rate limit
// (429 RATE_LIMITED + Retry-After: when the next mint is admitted, #3226),
// else a 500 whose text goes to the log only.
func writeIssueError(w http.ResponseWriter, r *http.Request, err error) {
	handler.WriteKindError(w, r, err, "issue federation token: ",
		errkind.FederationTokenLimit, errkind.FederationMintRateLimited)
}
