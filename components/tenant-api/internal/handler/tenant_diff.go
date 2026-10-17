package handler

import (
	"encoding/json"
	"errors"
	"net/http"
	"strings"

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
// Returns the unified diff between the current file and what PUT would write.
//
// #3062: the proposal is judged by PUT's own refusal sequence, in PUT's
// order (planPut and gitops.PreviewPut, tenant_put_plan.go): a proposal PUT
// refuses is refused here with PUT's status and body.
//
// #1560: a caller who may read the tenant but not write it (canSeeCredentials,
// PUT's write gate on {id}) gets the diff of the two MASKED texts instead of
// PUT's 403. ⛔ Both sides, and the equality test too: "no diff" for a
// guessed file would confirm a guessed credential. Any side that cannot be
// masked with certainty is a 422, never the raw diff.
//
// @Summary     Preview config diff
// @Description Returns unified diff between current file and proposed content.
// @Description The diff header names current/<id>.yaml and proposed/<id>.yaml.
// @Description The proposed side is what PUT /api/v1/tenants/{id} would write (#3062): the proposal's declared tenant sections
// @Description spliced into the current file, so a section the proposal leaves out shows as kept (a current file whose tenants
// @Description cannot be told apart is replaced whole, #2405, for a caller PUT lets replace it).
// @Description A proposal PUT would refuse is refused here exactly as PUT refuses it — the same status and body, judged in PUT's
// @Description order (#3062): the write scope on {id} as the file holds it and as the proposal proposes it, every other tenant
// @Description the proposal declares (a fixed 403), a masked-credential placeholder, the splice into the current file, a declared
// @Description tenant another conf.d file also declares, validation. In PR write mode the current file is this server's copy,
// @Description which may lag the base branch PUT judges on.
// @Description A caller who may read the tenant but not write it gets, instead of PUT's 403, the diff of the masked forms of
// @Description both sides (re-encoded, without comments, every receiver credential replaced by "<masked: write permission required>"),
// @Description with masked: true; a change to a credential value alone then shows has_diff false. Such a caller gets 422
// @Description MASKED_PREVIEW_UNAVAILABLE when either side is not YAML, holds several documents, or uses an anchor, alias or merge key.
// @Description When such a caller may not read every tenant of the current file, a refusal about how the file is written (409
// @Description not_round_trippable, the anchor rules, #2405's 400) is held until the duplicate-declaration check and validation have run,
// @Description and if neither refuses the answer is 422 TENANT_FILE_WITHHELD, as for a proposal PUT would accept: every file shape
// @Description gets the same answer (#3062).
// @Description For a proposal PUT would accept, every caller gets 422 TENANT_FILE_WITHHELD when the current file declares a tenant
// @Description the caller may not read (#3050; a conf.d file can declare several), or its tenants cannot be listed with certainty
// @Description (not YAML; an alias, a merge key, a non-scalar key or a key tagged other than !!str at the top level or in tenants:)
// @Description and the caller lacks read permission on all tenants: the diff would carry that file's lines, and has_diff would
// @Description confirm a guess of it.
// @Tags        tenants
// @Accept      json
// @Produce     json
// @Param       id    path     string      true "Tenant ID"
// @Param       body  body     DiffRequest true "Proposed YAML"
// @Success     200   {object} DiffResponse
// @Failure     400   {object} ErrorResponse "Bad request: the PUT this previews would be refused with this 400 (#3062: the same body)"
// @Failure     403   {object} ErrorResponse "Forbidden: the PUT this previews would be refused with this 403 (#3062: the same body) — the proposal moves {id} outside the caller's write scope, or declares a tenant besides {id} the caller may not write (a fixed text naming no tenant)"
// @Failure     409   {object} ErrorResponse "Conflict: the PUT this previews would be refused — code TENANT_CONFIG_NOT_LOADABLE, config_error not_round_trippable, when the proposal's declared sections cannot be written into the file without changing content it does not declare (#3062; as PUT's 409), ambiguous tenant file, TENANT_DECLARED_ELSEWHERE, or code TENANT_TREE_UNREADABLE when conf.d holds an unresolvable symlink (dangling, looping, or with a non-directory in its path) other than the file this write replaces (#2939, ADR-036 §4: the tree after the write is judged; repair it in git; nothing written; unreadable_count always, the entries' names and reasons in the text and in unreadable only for a caller with write permission on all tenants). The tenant's own file being unreadable in any other way (a failed read, a FIFO) is TENANT_CONFIG_NOT_LOADABLE (config_error unreadable or not_regular_file) for every caller: it is never replaced"
// @Failure     413   {object} ErrorResponse
// @Failure     422   {object} ErrorResponse
// @Failure     500   {object} ErrorResponse "Internal error; or code TENANT_STORE_FAULT (#2939): tenant-api's own copy of conf.d holds an entry the server cannot read and git cannot hold — a FIFO, socket or device named like a config file, a file whose read fails, a sub-directory that cannot be listed, a failed stat that is not an unresolvable link — other than the written tenant file; it is fixed on the server or its volume (or by resetting the copy from git), not by a write; wins over TENANT_TREE_UNREADABLE; nothing written; entry names disclosed as for TENANT_TREE_UNREADABLE (the 500 the PUT this previews would get)"
// @Router      /api/v1/tenants/{id}/diff [post]
func DiffTenant(d *Deps) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		// A body over the cap is a 413 for every caller (#2778).
		// Format: JSON envelope or raw YAML.
		readBody := func(rw http.ResponseWriter, r *http.Request) (string, bool) {
			body, ok := readLimitedBody(rw, r, d)
			if !ok {
				return "", false
			}
			ct := r.Header.Get("Content-Type")
			if strings.Contains(ct, "json") || (len(body) > 0 && body[0] == '{') {
				var req DiffRequest
				if err := json.Unmarshal(body, &req); err != nil {
					WriteJSONError(rw, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
					return "", false
				}
				return req.Proposed, true
			}
			return string(body), true
		}
		// #3062: PUT's refusal sequence, PUT's answers (tenant_put_plan.go):
		// the preview is refused exactly when, and as, the write would be.
		plan, ok := planPut(rw, rw, r, d, endpointDiff, readBody)
		if !ok {
			return
		}
		tenantID := plan.tenantID
		res, errs, err := plan.preview(d)
		if err == nil && len(errs) > 0 {
			err = validationError(errs)
		}
		if errors.Is(err, gitops.ErrSpliceShapeWithheld) {
			// A masked preview of a file this caller may not read whole: the
			// answer it gets for a write PUT accepts, whatever the file's
			// shape (tenant_put_plan.go).
			writePreviewWithheld(rw, r)
			return
		}
		if err != nil {
			writePutDirectError(rw, plan.r, d, tenantID, err)
			return
		}

		// What the preview may SHOW, decided after the verdict (see the
		// file comment of tenant_put_plan.go): a file the caller may not
		// read every tenant of is not shown (#3050).
		if res.Exists && !fileShown(r, d, res.Existing, tenantID) {
			writePreviewWithheld(rw, r)
			return
		}
		if !plan.masked {
			diff, err := previewWriter(d).DiffTexts(tenantID, string(res.Existing), res.Exists, res.Content)
			if err != nil {
				WriteJSONError(rw, r, http.StatusInternalServerError, err.Error())
				return
			}
			writeJSON(rw, http.StatusOK, DiffResponse{
				TenantID: tenantID,
				Diff:     diff,
				HasDiff:  diff != "",
			})
			return
		}

		// #1560: the masked preview parses both sides, for a caller who may
		// not write the tenant: bound the proposal first (#1722's gate, as
		// a 413), and mask both sides or show nothing.
		if errs := gitops.CheckTenantDocSize(plan.body); len(errs) > 0 {
			WriteJSONError(rw, r, http.StatusRequestEntityTooLarge, errs[0])
			return
		}
		var maskedCurrent []byte
		if res.Exists {
			if len(gitops.CheckTenantDocSize(string(res.Existing))) > 0 {
				writeMaskedPreviewUnavailable(rw, r, "the current file")
				return
			}
			if maskedCurrent, err = credmask.MaskYAML(res.Existing); err != nil {
				writeMaskedPreviewUnavailable(rw, r, "the current file")
				return
			}
		}
		maskedProposed, err := credmask.MaskYAML([]byte(res.Content))
		if err != nil {
			writeMaskedPreviewUnavailable(rw, r, "the proposal")
			return
		}
		diff, err := previewWriter(d).DiffTexts(tenantID, string(maskedCurrent), res.Exists, string(maskedProposed))
		if err != nil {
			WriteJSONError(rw, r, http.StatusInternalServerError, err.Error())
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
