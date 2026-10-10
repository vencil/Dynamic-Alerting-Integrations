package handler

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/go-chi/chi/v5"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/credmask"
	"github.com/vencil/tenant-api/internal/gitops"
)

// DiffRequest is the body for POST /api/v1/tenants/{id}/diff.
type DiffRequest struct {
	Proposed string `json:"proposed"` // proposed YAML content
}

// DiffResponse is returned by POST /api/v1/tenants/{id}/diff.
type DiffResponse struct {
	TenantID string `json:"tenant_id"`
	Diff     string `json:"diff"`
	HasDiff  bool   `json:"has_diff"`
	// Masked is true when the caller may read the tenant but not write it
	// (#1560): the diff is then between the masked forms of the current file
	// and the proposal — re-encoded, without comments, every receiver
	// credential replaced by "<masked: write permission required>" — so
	// has_diff does not report a change to a credential value.
	Masked bool `json:"masked,omitempty"`
}

// DiffTenant handles POST /api/v1/tenants/{id}/diff
//
// Accepts JSON {"proposed": "<yaml>"} or raw YAML body (detected by Content-Type).
// Returns the unified diff between the current file and the proposed content.
//
// #1560: a caller who may read the tenant but not write it (canSeeCredentials)
// gets the diff of the two MASKED texts. ⛔ Both sides, and the equality test
// too: Writer.Diff compares the raw bytes first, so "no diff" for a guessed
// file would confirm a guessed credential. Any side that cannot be masked
// with certainty is a 422, never the raw diff.
//
// @Summary     Preview config diff
// @Description Returns unified diff between current file and proposed content.
// @Description The diff header names current/<id>.yaml and proposed/<id>.yaml.
// @Description A caller who may read the tenant but not write it gets the diff of the masked forms of both sides
// @Description (re-encoded, without comments, every receiver credential replaced by "<masked: write permission required>"),
// @Description with masked: true; a change to a credential value alone then shows has_diff false. Such a caller gets 413
// @Description for a proposal over the tenant-document size limit, and 422 MASKED_PREVIEW_UNAVAILABLE when either side is
// @Description not YAML, holds several documents, or uses an anchor, alias or merge key.
// @Tags        tenants
// @Accept      json
// @Produce     json
// @Param       id    path     string      true "Tenant ID"
// @Param       body  body     DiffRequest true "Proposed YAML"
// @Success     200   {object} DiffResponse
// @Failure     400   {object} ErrorResponse
// @Failure     409   {object} ErrorResponse
// @Failure     413   {object} ErrorResponse
// @Failure     422   {object} ErrorResponse
// @Failure     500   {object} ErrorResponse
// @Router      /api/v1/tenants/{id}/diff [post]
func DiffTenant(d *Deps) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			WriteJSONError(rw, r, http.StatusBadRequest, err.Error())
			return
		}
		masked := !canSeeCredentials(r, d, tenantID)

		// A body over the cap is a 413 for every caller (#2778); the masked
		// preview, which parses what it reads, relied on that first (R5).
		body, ok := readLimitedBody(rw, r, d)
		if !ok {
			return
		}

		// Determine format: JSON envelope or raw YAML
		proposed := string(body)
		ct := r.Header.Get("Content-Type")
		if strings.Contains(ct, "json") || (len(body) > 0 && body[0] == '{') {
			var req DiffRequest
			if err := json.Unmarshal(body, &req); err != nil {
				WriteJSONError(rw, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
				return
			}
			proposed = req.Proposed
		}

		if !masked {
			diff, err := d.Writer.Diff(tenantID, proposed)
			if err != nil {
				writeDiffError(rw, r, err)
				return
			}
			writeJSON(rw, http.StatusOK, DiffResponse{
				TenantID: tenantID,
				Diff:     diff,
				HasDiff:  diff != "",
			})
			return
		}

		// The masked preview parses both sides, which today's raw diff never
		// did, for any reader: bound it first (#1722's gate, as a 413).
		if errs := gitops.CheckTenantDocSize(proposed); len(errs) > 0 {
			WriteJSONError(rw, r, http.StatusRequestEntityTooLarge, errs[0])
			return
		}
		current, exists, err := d.Writer.PreviewCurrent(tenantID)
		if err != nil {
			writeDiffError(rw, r, err)
			return
		}
		var maskedCurrent []byte
		if exists {
			if len(gitops.CheckTenantDocSize(string(current))) > 0 {
				writeMaskedPreviewUnavailable(rw, r, "the current file")
				return
			}
			if maskedCurrent, err = credmask.MaskYAML(current); err != nil {
				writeMaskedPreviewUnavailable(rw, r, "the current file")
				return
			}
		}
		maskedProposed, err := credmask.MaskYAML([]byte(proposed))
		if err != nil {
			writeMaskedPreviewUnavailable(rw, r, "the proposal")
			return
		}
		diff, err := d.Writer.DiffTexts(tenantID, string(maskedCurrent), exists, string(maskedProposed))
		if err != nil {
			writeDiffError(rw, r, err)
			return
		}
		writeJSON(rw, http.StatusOK, DiffResponse{
			TenantID: tenantID,
			Diff:     diff,
			HasDiff:  diff != "",
			Masked:   true,
		})
	}
}

// writeDiffError answers a preview that could not be computed.
func writeDiffError(rw http.ResponseWriter, r *http.Request, err error) {
	// #1673: two files claim this tenant, so there is no single
	// "current file" to diff against. Server state, not a bad request.
	if errors.Is(err, confd.ErrAmbiguousTenantFile) {
		WriteJSONError(rw, r, http.StatusConflict, err.Error())
		return
	}
	// #2078: the PUT this previews would be refused (409) — say so
	// instead of showing a "new file" diff for a write that cannot land.
	if writeTenantPlacementError(rw, r, err) {
		return
	}
	WriteJSONError(rw, r, http.StatusInternalServerError, err.Error())
}

// writeMaskedPreviewUnavailable is the 422 for a masked preview one of whose
// sides cannot be masked with certainty. which names the side.
func writeMaskedPreviewUnavailable(rw http.ResponseWriter, r *http.Request, which string) {
	WriteJSONErrorWithCode(rw, r, http.StatusUnprocessableEntity, CodeMaskedPreviewUnavailable,
		"a caller without write permission on the tenant is shown a preview with credentials masked, and "+
			which+" cannot be masked with certainty (not YAML, several documents, or an anchor, alias or merge key)")
}
