package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
)

// GroupBatchRequest is the body for POST /api/v1/groups/{id}/batch.
// Applies a patch to all members of the specified group.
type GroupBatchRequest struct {
	// key → value to set on every member (e.g., "_silent_mode": "warning");
	// at most 1000 entries. May be empty only when unset names a key (else 400).
	Patch map[string]string `json:"patch" validate:"max=1000"`
	// keys to remove from every member's config block; at most 1000 entries. Only "_routing" is
	// accepted: removing it resets the member to `_routing_defaults` and its routing profile,
	// which turns routing back on for a member a disabling `_routing` turned off (the change is
	// judged by domain policy like a `_routing` patch). A key the member does not carry, or a
	// member that does not exist, makes it a no-op: with no patch nothing is written and no
	// tenant is created. A key must not appear twice, nor in both patch and unset.
	Unset []string `json:"unset,omitempty" validate:"max=1000"`
}

// GroupBatchResponse is the response for POST /api/v1/groups/{id}/batch.
// The PR-mode fields mirror BatchResponse: the group is expanded into one op
// per member and runs the POST /tenants/batch pipeline (#2339).
type GroupBatchResponse struct {
	Status   string        `json:"status"` // "completed" | "pending_review" (PR mode)
	TaskID   string        `json:"task_id"`
	GroupID  string        `json:"group_id"`
	PRURL    string        `json:"pr_url,omitempty"`    // PR/MR URL in PR mode
	PRNumber int           `json:"pr_number,omitempty"` // PR/MR number in PR mode
	Results  []BatchResult `json:"results"`
	Summary  string        `json:"summary"`           // e.g., "5 succeeded, 1 failed"
	Message  string        `json:"message,omitempty"` // human-readable message (PR mode)
	// Warnings is the batch-level advisory list for PR mode, as in
	// BatchResponse.Warnings; each notice names its tenant.
	Warnings []string `json:"warnings,omitempty"`
}

// GroupBatch handles POST /api/v1/groups/{id}/batch
//
// Applies a patch operation to all members of a group. The group is expanded
// into one BatchOperation per member and runs the POST /tenants/batch
// pipeline (#2339): the same patch-value check, per-member RBAC, domain
// policy, and — in PR write-back mode — one PR/MR for the whole group.
// Supports async mode via ?async=true query parameter (direct mode only; PR
// mode is synchronous, like /tenants/batch).
//
// Query Parameters:
//
//	?async=true  — Enable async mode; returns 202 with task_id for polling
//	(default)    — Sync mode; returns 200 with completed results
//
// @Summary     Batch operation on group members
// @Description Apply a patch to all tenants in a group, through the same pipeline as POST /api/v1/tenants/batch
// @Description (one operation per member): patch values are range-checked (400), and each member is checked for write permission and domain policy.
// @Description unset removes keys from every member (only "_routing"): unset ["_routing"] turns routing back on for members a disabling _routing turned off, judged by domain policy.
// @Description PR write-back mode: the whole group becomes one PR/MR (status pending_review, pr_url, pr_number); nothing is committed to the base branch.
// @Description PR write-back mode ignores ?async=true and answers 200 synchronously, as POST /api/v1/tenants/batch does.
// @Description Direct mode: a member whose config file cannot be loaded as a tenant config (config_error malformed_yaml | invalid_config)
// @Description is not patched: its result carries status error and code TENANT_CONFIG_NOT_LOADABLE; repair the tenant file itself first.
// @Tags        groups
// @Accept      json
// @Produce     json
// @Param       id    path     string            true "Group ID"
// @Param       body  body     GroupBatchRequest true "Patch to apply"
// @Param       async query   string            false "Enable async mode (true/false)"
// @Success     200   {object} GroupBatchResponse
// @Success     202   {object} map[string]interface{}
// @Failure     400   {object} ErrorResponse
// @Failure     403   {object} ErrorResponse "PR write-back mode: the forge token lacks write scope to open the PR/MR"
// @Failure     404   {object} ErrorResponse
// @Failure     409   {object} ErrorResponse "PR write-back mode: a member is already declared by another conf.d file (code TENANT_DECLARED_ELSEWHERE), or its config file cannot be loaded as a tenant config (code TENANT_CONFIG_NOT_LOADABLE, with tenant_id and config_error; repair the tenant file itself first); nothing written. Direct mode reports these per member in results[].code instead."
// @Failure     413   {object} ErrorResponse
// @Failure     500   {object} ErrorResponse
// @Failure     503   {object} ErrorResponse
// @Router      /api/v1/groups/{id}/batch [post]
func GroupBatch(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)
		// Capture the verified principal VALUE for the async path below —
		// same discipline as email: the closure must never reach back into
		// the request context (it outlives the HTTP request), so it captures
		// the immutable principal snapshot, not r / r.Context().
		p := rbac.RequestPrincipal(r)

		g, ok := d.Groups.GetGroup(groupID)
		if !ok {
			WriteJSONError(w, r, http.StatusNotFound, "group not found: "+groupID)
			return
		}

		// #1722: same budget, same reason as POST /tenants/batch — and this
		// endpoint needed it MORE, not less. It reaches the identical write
		// path (executeBatchOps → applyPatch → WriteMerged → readMergeValidate
		// → validate) and applies ONE patch to EVERY member, so the in-lock
		// cost is multiplied by the group size rather than paid once.
		//
		// ⛔ The read happens before the per-member permission gate, so an
		// oversize body is materialised for a caller who may hold no write
		// permission anywhere. That is why the cap sits here, at the read,
		// rather than after authorization.
		batchLimit, batchKnob := d.BatchBodyLimit()
		body, ok := readBodyWithin(w, r, batchLimit, batchKnob)
		if !ok {
			return
		}
		var req GroupBatchRequest
		if err := json.NewDecoder(bytes.NewReader(body)).Decode(&req); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		// B2: an unset-only request is allowed; one with neither keeps the
		// pre-B2 refusal, code and message unchanged.
		if len(req.Patch) == 0 && len(req.Unset) == 0 {
			WriteJSONError(w, r, http.StatusBadRequest, "patch must not be empty")
			return
		}
		// #1722: the byte cap above does NOT bound the key count, and this is the
		// endpoint where that matters most — mergePatchYAML is quadratic in
		// len(patch), runs inside the single-writer token, and here it runs once
		// PER MEMBER. A body of ~18k short legal keys sits comfortably inside the
		// 256 KiB budget. The `max` lives on the struct tag so the bound is one
		// value, not two; it is enforced HERE because the per-member
		// BatchOperations are built in Go and never pass through JSON-decode
		// validation. #2339: the patch values get the same range check as
		// /tenants/batch. B2: so do the unset keys.
		violations := ValidateStructTags(&req)
		violations = append(violations, validateBatchEdit(req.Patch, req.Unset, "")...)
		if len(violations) > 0 {
			WriteValidationErrors(w, r, violations)
			return
		}

		if len(g.Members) == 0 {
			WriteJSONError(w, r, http.StatusBadRequest, "group has no members")
			return
		}

		// #2339: one op per member, run through the /tenants/batch pipeline.
		// BatchRequest's struct tags (operations max=1000, tenant_id bounds)
		// are deliberately NOT applied to the expansion: the group, not the
		// caller, sets its size, and each member's id is checked per op.
		// A member listed twice yields two ops on one tenant, exactly as two
		// such ops in a /tenants/batch body would.
		ops := make([]BatchOperation, len(g.Members))
		for i, member := range g.Members {
			ops[i] = BatchOperation{TenantID: member, Patch: req.Patch, Unset: req.Unset}
		}

		taskID := fmt.Sprintf("group-batch-%s-%s",
			groupID, time.Now().UTC().Format("20060102-150405"))

		if d.prWritePath() {
			meta := batchPRMeta{
				title: func(n int) string {
					return fmt.Sprintf("[tenant-api] Group %s batch update %d tenants", groupID, n)
				},
				source: "tenant-manager UI (group batch: " + groupID + ")",
				labels: []string{"tenant-api", "auto-generated", "batch", "group"},
			}
			resp, ok := runBatchPR(d, w, r, ops, email, p, meta)
			if !ok {
				return
			}
			writeJSON(w, http.StatusOK, GroupBatchResponse{
				Status:   resp.Status,
				TaskID:   taskID,
				GroupID:  groupID,
				PRURL:    resp.PRURL,
				PRNumber: resp.PRNumber,
				Results:  resp.Results,
				Summary:  resp.Summary,
				Message:  resp.Message,
				Warnings: resp.Warnings,
			})
			return
		}

		// v2.6.0: Async mode — submit to goroutine pool and return immediately
		if r.URL.Query().Get("async") == "true" && d.Tasks != nil {
			task := d.Tasks.Submit(taskID, func(ctx context.Context) ([]async.TaskResult, error) {
				results := executeBatchOps(ctx, d.Writer, d.ConfigDir, ops, email, p, d.RBAC, d.TenantOrg, d.Policy)
				return toTaskResults(results), nil
			})

			write202Pending(w, task)
			return
		}

		// Synchronous mode (default, backward compatible)
		results := executeBatchOps(r.Context(), d.Writer, d.ConfigDir, ops, email, p, d.RBAC, d.TenantOrg, d.Policy)

		writeJSON(w, http.StatusOK, GroupBatchResponse{
			Status:  "completed",
			TaskID:  taskID,
			GroupID: groupID,
			Results: results,
			Summary: summarizeBatchResults(results),
		})
	}
}
