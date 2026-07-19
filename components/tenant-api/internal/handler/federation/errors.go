package federation

import (
	"errors"
	"net/http"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/handler"
)

// writeFederationGitError maps a gitops write-path error to the HTTP
// response shared by every federation registry write (token account-id
// allocation, fleet backfill): an overloaded write plane or degraded forge
// → 503 (retryable), a commit conflict → 409, anything else → 500 prefixed
// with msg500. It always writes exactly one response, so callers invoke it
// inside `if err != nil { ...; return }`.
func writeFederationGitError(w http.ResponseWriter, r *http.Request, err error, msg500 string) {
	if errors.Is(err, gitops.ErrWriteOverloaded) || errors.Is(err, gitops.ErrForgeDegraded) {
		handler.WriteOverloaded(w, r)
		return
	}
	if errors.Is(err, gitops.ErrConflict) {
		handler.WriteJSONError(w, r, http.StatusConflict, err.Error())
		return
	}
	handler.WriteJSONError(w, r, http.StatusInternalServerError, msg500+err.Error())
}
