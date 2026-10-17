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
	"github.com/vencil/tenant-api/internal/rbac"
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
// @Description The proposed side is what PUT /api/v1/tenants/{id} would write (#3062): the proposal's declared tenant sections
// @Description spliced into the current file, so a section the proposal leaves out shows as kept. A proposal PUT refuses as malformed,
// @Description or a current file whose tenants cannot be told apart (#2405: replaced whole), is shown as the proposal itself.
// @Description A caller who may read the tenant but not write it gets the diff of the masked forms of both sides
// @Description (re-encoded, without comments, every receiver credential replaced by "<masked: write permission required>"),
// @Description with masked: true; a change to a credential value alone then shows has_diff false. Such a caller gets 413
// @Description for a proposal over the tenant-document size limit, and 422 MASKED_PREVIEW_UNAVAILABLE when either side is
// @Description not YAML, holds several documents, or uses an anchor, alias or merge key.
// @Description Every caller gets 422 TENANT_FILE_WITHHELD when the current file declares a tenant the caller may not read
// @Description (#3050; a conf.d file can declare several), or its tenants cannot be listed with certainty (not YAML; an alias,
// @Description a merge key, a non-scalar key or a key tagged other than !!str at the top level or in tenants:) and the caller lacks read permission on all
// @Description tenants: the diff would carry that file's lines, and has_diff would confirm a guess of it.
// @Tags        tenants
// @Accept      json
// @Produce     json
// @Param       id    path     string      true "Tenant ID"
// @Param       body  body     DiffRequest true "Proposed YAML"
// @Success     200   {object} DiffResponse
// @Failure     400   {object} ErrorResponse
// @Failure     409   {object} ErrorResponse "Conflict: the PUT this previews would be refused — code TENANT_CONFIG_NOT_LOADABLE, config_error not_round_trippable, when the proposal's declared sections cannot be written into the file without changing content it does not declare (#3062; as PUT's 409), ambiguous tenant file, TENANT_DECLARED_ELSEWHERE, or code TENANT_TREE_UNREADABLE when conf.d holds an unresolvable symlink (dangling, looping, or with a non-directory in its path) other than the file this write replaces (#2939, ADR-036 §4: the tree after the write is judged; repair it in git; nothing written; unreadable_count always, the entries' names and reasons in the text and in unreadable only for a caller with write permission on all tenants). The tenant's own file being unreadable in any other way (a failed read, a FIFO) is TENANT_CONFIG_NOT_LOADABLE (config_error unreadable or not_regular_file) for every caller: it is never replaced"
// @Failure     413   {object} ErrorResponse
// @Failure     422   {object} ErrorResponse
// @Failure     500   {object} ErrorResponse "Internal error; or code TENANT_STORE_FAULT (#2939): tenant-api's own copy of conf.d holds an entry the server cannot read and git cannot hold — a FIFO, socket or device named like a config file, a file whose read fails, a sub-directory that cannot be listed, a failed stat that is not an unresolvable link — other than the written tenant file; it is fixed on the server or its volume (or by resetting the copy from git), not by a write; wins over TENANT_TREE_UNREADABLE; nothing written; entry names disclosed as for TENANT_TREE_UNREADABLE (the 500 the PUT this previews would get)"
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
			// Writer.Diff is PreviewCurrent + DiffTexts; split so the
			// preview is judged on the bytes it diffs (#3050).
			current, exists, err := d.Writer.PreviewCurrent(tenantID)
			if err != nil {
				writeDiffError(rw, r, d, err)
				return
			}
			if exists && !fileShown(r, d, current, tenantID) {
				writePreviewWithheld(rw, r)
				return
			}
			written, err := previewPut(r, d, tenantID, current, proposed)
			if err != nil {
				writeDiffError(rw, r, d, err)
				return
			}
			diff, err := d.Writer.DiffTexts(tenantID, string(current), exists, written)
			if err != nil {
				writeDiffError(rw, r, d, err)
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
			writeDiffError(rw, r, d, err)
			return
		}
		if exists && !fileShown(r, d, current, tenantID) {
			writePreviewWithheld(rw, r)
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
		written, err := previewPut(r, d, tenantID, current, proposed)
		if err != nil {
			writeDiffError(rw, r, d, err)
			return
		}
		maskedProposed, err := credmask.MaskYAML([]byte(written))
		if err != nil {
			writeMaskedPreviewUnavailable(rw, r, "the proposal")
			return
		}
		diff, err := d.Writer.DiffTexts(tenantID, string(maskedCurrent), exists, string(maskedProposed))
		if err != nil {
			writeDiffError(rw, r, d, err)
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

// previewPut is what PUT would write over current (nil when the file does
// not exist) for proposed (#3062): the proposal's declared tenant sections
// spliced into the current file, by the very splice PUT runs, so a section
// the proposal leaves out shows as kept, not deleted. Two outcomes PUT does
// not write are previewed as the proposal itself, as before: a proposal PUT
// refuses as malformed (no tenants mapping, a merge key or a key that is
// not one tenant id there: 400), and a current file the splice cannot read
// (PUT replaces it whole for a caller with write permission on all tenants
// and refuses anyone else, #2405). A splice PUT refuses because it would
// change what the proposal does not declare is the *putSpliceError PUT
// answers with (writeDiffError: the same 409).
func previewPut(r *http.Request, d *Deps, tenantID string, current []byte, proposed string) (string, error) {
	names := tenantNamesFor(d.RBAC, d.TenantOrg, rbac.RequestPrincipal(r))
	written, err := spliceDeclaredSections(current, proposed, tenantID, names, putFileShown(r, d, tenantID))
	if errors.Is(err, gitops.ErrSpliceBaseUnparseable) || errors.Is(err, gitops.ErrValidation) {
		return proposed, nil
	}
	return written, err
}

// writeDiffError answers a preview that could not be computed.
func writeDiffError(rw http.ResponseWriter, r *http.Request, d *Deps, err error) {
	// #3062: the PUT this previews would be refused: its declared sections
	// cannot be written into the file without changing the rest.
	if writePutSpliceError(rw, r, err) {
		return
	}
	// #1673: two files claim this tenant, so there is no single
	// "current file" to diff against. Server state, not a bad request.
	if errors.Is(err, confd.ErrAmbiguousTenantFile) {
		WriteJSONError(rw, r, http.StatusConflict, err.Error())
		return
	}
	// #2078: the PUT this previews would be refused (409) — say so
	// instead of showing a "new file" diff for a write that cannot land.
	if writeTenantPlacementError(rw, r, d, err) {
		return
	}
	WriteJSONError(rw, r, http.StatusInternalServerError, err.Error())
}

// writePreviewWithheld is the 422 for a preview whose current file declares
// a tenant the caller may not read (#3050, fileShown): the diff carries the
// file's lines around every change (and all of them against a short
// proposal), and has_diff false would confirm a guess of the whole file. The
// text names no tenant.
func writePreviewWithheld(rw http.ResponseWriter, r *http.Request) {
	WriteJSONErrorWithCode(rw, r, http.StatusUnprocessableEntity, CodeTenantFileWithheld,
		"the current tenant file also declares a tenant you may not read (or its tenants cannot be listed "+
			"with certainty), so no preview of it is shown")
}

// writeMaskedPreviewUnavailable is the 422 for a masked preview one of whose
// sides cannot be masked with certainty. which names the side.
func writeMaskedPreviewUnavailable(rw http.ResponseWriter, r *http.Request, which string) {
	WriteJSONErrorWithCode(rw, r, http.StatusUnprocessableEntity, CodeMaskedPreviewUnavailable,
		"a caller without write permission on the tenant is shown a preview with credentials masked, and "+
			which+" cannot be masked with certainty (not YAML, several documents, or an anchor, alias or merge key)")
}
