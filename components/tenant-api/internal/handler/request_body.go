package handler

import (
	"io"
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
