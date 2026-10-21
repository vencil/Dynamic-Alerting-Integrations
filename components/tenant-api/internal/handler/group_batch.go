package handler

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
)

// GroupBatchRequest is the body for POST /api/v1/groups/{id}/batch.
// Applies a patch to all members of the specified group.
type GroupBatchRequest struct {
	// key → value to set on every member (e.g., "_silent_mode": "warning");
	// at most 1000 entries. May be empty only when unset names a key (else 400).
	Patch map[string]string `json:"patch" validate:"max=1000"`
	// keys to remove from every member's config block; at most 1000 entries. Only "_routing" and
	// "_routing_receiver_type" are accepted: removing `_routing` resets the member to
	// `_routing_defaults` and its routing profile, which turns routing back on for a member a
	// disabling `_routing` turned off (the route generator judges the result, as every
	// write); removing `_routing_receiver_type` drops the flat key tenant-api used to write,
	// which nothing renders (#2983). A key the member does not carry makes it
	// a no-op. A key must not appear twice, nor in both patch and unset.
	Unset []string `json:"unset,omitempty" validate:"max=1000"`
}

// GroupBatchResponse is the response for POST /api/v1/groups/{id}/batch.
// The PR-mode fields mirror BatchResponse: the group is expanded into one op
// per member and runs the POST /tenants/batch pipeline (#2339).
type GroupBatchResponse struct {
	Status   string        `json:"status"` // "completed" | "pending_review" (PR mode) | "rejected" (HTTP 422: a member was refused, nothing written)
	TaskID   string        `json:"task_id"`
	GroupID  string        `json:"group_id"`
	PRURL    string        `json:"pr_url,omitempty"`    // PR/MR URL in PR mode
	PRNumber int           `json:"pr_number,omitempty"` // PR/MR number in PR mode
	Results  []BatchResult `json:"results"`
	Summary  string        `json:"summary"`           // e.g., "5 succeeded"; a rejected batch: "1 failed, 4 not applied"
	Message  string        `json:"message,omitempty"` // human-readable message (PR mode)
	// Warnings is the batch-level advisory list for PR mode, as in
	// BatchResponse.Warnings; each notice names its tenant.
	Warnings []string `json:"warnings,omitempty"`
	// Findings, FindingsComparedWithBase, UnrelatedFindingsWithheld: as in
	// BatchResponse — a batch the generator container refused with
	// findings (#2903).
	Findings                  []generator.Finding `json:"findings,omitempty"`
	FindingsComparedWithBase  *bool               `json:"findings_compared_with_base,omitempty"`
	UnrelatedFindingsWithheld int                 `json:"unrelated_findings_withheld,omitempty"`
}

// GroupBatch handles POST /api/v1/groups/{id}/batch
//
// Applies a patch operation to all members of a group. The group is expanded
// into one BatchOperation per member and runs the POST /tenants/batch
// pipeline (#2339): the same patch-value check, per-member RBAC, the route
// generator's verdict (#2903), and — in PR write-back mode — one PR/MR for
// the whole group.
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
// @Description (one operation per member): patch values are range-checked (400), and each member is checked for write permission; the route generator container judges the tree with every member's change applied (#2903).
// @Description unset removes keys from every member (only "_routing" and "_routing_receiver_type"): unset ["_routing"] turns routing back on for members a disabling _routing turned off; unset ["_routing_receiver_type"] removes the flat key tenant-api used to write (#2983). A patch may not write a _routing-prefixed key other than _routing and _routing_profile (400).
// @Description All or nothing, in one commit, as POST /api/v1/tenants/batch: when any member is refused nothing is written (422, status rejected, the refused members with status error, the others not_applied);
// @Description otherwise every changed member file is in one commit (direct mode) or one PR/MR with one commit (PR write-back mode). A rejected async run ends the task as failed with the same results.
// @Description Results and summary list only the members the caller may read; a refusal of a member the caller cannot read still rejects the batch.
// @Description PR write-back mode: the whole group becomes one PR/MR (status pending_review, pr_url, pr_number); nothing is committed to the base branch.
// @Description A member that does not exist (no tenant section on the base the write merges into, e.g. offboarded while still listed in the group) is not created:
// @Description its result has status skipped and message "tenant does not exist", nothing is written for it, it does not reject the batch, and the summary counts it apart ("1 succeeded, 1 skipped").
// @Description PR write-back mode ignores ?async=true and answers 200 synchronously, as POST /api/v1/tenants/batch does.
// @Description Direct mode: a member whose config file cannot be loaded as a tenant config (config_error malformed_yaml | invalid_config)
// @Description is refused with code TENANT_CONFIG_NOT_LOADABLE (and rejects the batch); repair the tenant file itself first.
// @Description Also config_error not_round_trippable (#2903): re-encoding the tenant file would change content the update does not edit (the message names the first difference inside the tenant's own block); rewrite the file with a whole-file PUT.
// @Tags        groups
// @Accept      json
// @Produce     json
// @Param       id    path     string            true "Group ID"
// @Param       body  body     GroupBatchRequest true "Patch to apply"
// @Param       async query   string            false "Enable async mode (true/false)"
// @Success     200   {object} GroupBatchResponse
// @Success     202   {object} map[string]interface{}
// @Failure     400   {object} ErrorResponse
// @Failure     403   {object} ErrorResponse "PR write-back mode: the forge token lacks write scope to open the PR/MR. (A member refused by the write scope, on the local copy or on the latest base branch, rejects the batch with a 422 instead.)"
// @Failure     404   {object} ErrorResponse
// @Failure     409   {object} ErrorResponse "Direct mode, code CONFLICT, with committed: false — the repository changed outside tenant-api while the batch ran; nothing was written, refresh and retry. With committed: true and commit — the batch WAS committed (that commit) but the write did not finish cleanly on the server; refresh before changing anything."
// @Failure     413   {object} ErrorResponse
// @Failure     422   {object} GroupBatchResponse "Batch rejected (status rejected): at least one member was refused — by the checks on this server's copy, or (PR write-back mode) on the latest base branch — so nothing was written and no PR/MR was opened. results lists the members the caller may read: status error (message, code) for the refused ones, not_applied for the rest; the messages name no member index. Codes TENANT_TREE_UNREADABLE and TENANT_CONFIG_NOT_LOADABLE as on POST /api/v1/tenants/batch (#2939). Also when the route generator container refuses the batch as a whole (#2903, ADR-036 §4: the tree with the whole batch applied is judged once): every result is status error with code GENERATOR_FINDINGS (its message names the findings about that result's tenant) and findings[] / findings_compared_with_base / unrelated_findings_withheld carry the findings as for PUT /tenants/{id}; or code GENERATOR_REJECTED (memory, incomplete, too_large: the files the batch produces are too large to judge in one request — split the batch, invalid_request, normalized: git attributes would commit other bytes than the ones judged). An async batch's task carries the same per-op results."
// @Failure     429  {object} ErrorResponse "Too many requests: code GENERATOR_REJECTED (#2903) — the route generator container gave no verdict this time, retry (Retry-After): reason busy, deadline, git_read, mismatch (what it judged is still not what would be written after one re-judgement) or no_time (too little of the request's time left to judge). Nothing written. Also code RATE_LIMITED (per-caller rate limit)"
// @Failure     500   {object} ErrorResponse "Internal error; or code TENANT_STORE_FAULT (#2939): tenant-api's own copy of conf.d holds an entry the server cannot read and git cannot hold (a FIFO, socket or device named like a config file, a failed read, an unlistable directory); the batch fails whole, nothing written — it is fixed on the server or its volume, not by changing operations. Entry names as for the 422; an async task's stored results never name them"
// @Failure     503   {object} ErrorResponse "Service unavailable: the write plane is busy (WRITE_OVERLOADED) or the forge is degraded (FORGE_UNAVAILABLE). Also, for every write that changes a conf.d file (#2903, ADR-036 §5): code VALIDATOR_UNAVAILABLE when the route generator container cannot judge (reason unreachable, bad_response, amtool_missing, generator_too_old, internal_error), or POLICY_UNAVAILABLE when its deployment policy file is unusable (reason policy_unusable, or policy_unreadable for tenant-api's own copy); Retry-After; nothing written. In PR/MR write-back mode, code GIT_PUSH_FAILED (#1724) when pushing the batch branch to the config repository failed, a timeout included: the whole batch fails, no PR/MR opened, Retry-After (the branch may be left on origin without a PR/MR). Also code UPSTREAM_ERROR (#1535, #3173) when a synchronous batch was stopped before anything of it was committed: the request was cancelled or reached its deadline, or a git command hit its own deadline (TENANT_API_GIT_TIMEOUT) and the server read the branch back unmoved; nothing written, safe to retry. And code UPSTREAM_ERROR with another message when updating the branch failed and the server could not read the branch back: unlike the cases above the batch may already be committed, so refresh to see whether it was before retrying (an async task says the same in its error)"
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

		// #1530: a group the caller cannot see is answered exactly like a
		// missing one, BEFORE the body is read or validated — the later 400s
		// ("group has no members", a bad patch) would otherwise confirm it
		// exists, and the batch would run and report its hidden members.
		g, ok := d.Groups.GetGroup(groupID)
		if !ok || !groupVisible(d, p, g.Members) {
			writeGroupNotFound(w, r, groupID)
			return
		}

		// #1722: same budget, same reason as POST /tenants/batch — and this
		// endpoint needed it MORE, not less. It reaches the identical write
		// path (executeBatchOps → WriteBatch → readMergeValidate → validate) and applies ONE patch to EVERY member, so the in-lock
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
		// such ops in a /tenants/batch body would. #2882: unlike those ops, a
		// member that does not exist is skipped, not created — offboarding a
		// tenant does not prune it from the groups listing it.
		ops := make([]BatchOperation, len(g.Members))
		for i, member := range g.Members {
			ops[i] = BatchOperation{TenantID: member, Patch: req.Patch, Unset: req.Unset, skipAbsent: true}
		}

		// #3123: an opaque random id (it used to be group-batch-<group>-<ts>).
		taskID := async.NewTaskID()

		if d.prWritePath() {
			meta := batchPRMeta{
				title: func(n int) string {
					return fmt.Sprintf("[tenant-api] Group %s batch update %d tenants", groupID, n)
				},
				source: "tenant-manager UI (group batch: " + groupID + ")",
				labels: []string{"tenant-api", "auto-generated", "batch", "group"},
			}
			resp, status, ok := runBatchPR(d, w, r, ops, email, p, meta)
			if !ok {
				return
			}
			visible := visibleGroupBatchResults(d, p, resp.Results)
			summary := prBatchSummary(resp.Status, visible)
			if resp.Status == batchStatusRejected {
				summary = summarizeBatchResults(visible)
			}
			writeJSON(w, status, GroupBatchResponse{
				Status:   resp.Status,
				TaskID:   taskID,
				GroupID:  groupID,
				PRURL:    resp.PRURL,
				PRNumber: resp.PRNumber,
				Results:  visible,
				Summary:  summary,
				Message:  resp.Message,
				Warnings: resp.Warnings,

				Findings:                  resp.Findings,
				FindingsComparedWithBase:  resp.FindingsComparedWithBase,
				UnrelatedFindingsWithheld: resp.UnrelatedFindingsWithheld,
			})
			return
		}

		// v2.6.0: Async mode — submit to goroutine pool and return immediately
		if r.URL.Query().Get("async") == "true" && d.Tasks != nil {
			submitTask(w, r, d, taskID, p, batchTaskFunc(d, ops, email, p))
			return
		}

		// Synchronous mode (default). #2900: all or nothing, as
		// POST /tenants/batch.
		run := d.executeBatch(r.Context(), r.Context(), ops, email, p, batchResultsAnswer)
		if run.Err != nil && generatorRejectsBatch(run.Err) {
			// #2903: refused by the generator — a rejected batch, as any.
			results := visibleGroupBatchResults(d, p, run.Results)
			rej := rejectedByGenerator(d, r, taskID, results, run.Err)
			writeJSON(w, http.StatusUnprocessableEntity, GroupBatchResponse{
				Status: rej.Status, TaskID: taskID, GroupID: groupID, Results: results,
				Summary: rej.Summary, Message: rej.Message,
				Findings: rej.Findings, FindingsComparedWithBase: rej.FindingsComparedWithBase,
				UnrelatedFindingsWithheld: rej.UnrelatedFindingsWithheld,
			})
			return
		}
		if run.Err != nil {
			writeBatchFailure(w, r, d, run.Err)
			return
		}
		results := visibleGroupBatchResults(d, p, run.Results)
		resp := GroupBatchResponse{
			Status:  "completed",
			TaskID:  taskID,
			GroupID: groupID,
			Results: results,
			Summary: summarizeBatchResults(results),
		}
		status := http.StatusOK
		if run.Rejected {
			resp.Status, resp.Message, status = batchStatusRejected, msgBatchRejected, http.StatusUnprocessableEntity
		}
		writeJSON(w, status, resp)
	}
}

// visibleGroupBatchResults keeps the per-member results the caller may read
// (#1530). Unlike POST /tenants/batch, where every result names a tenant the
// caller put in the request, a group batch runs over the STORED member list,
// so its results would name members GET /groups/{id} filters away. The
// summary must be rendered from what this returns, not from the full list:
// "1 succeeded, 1 failed" beside one visible result is the hidden member again,
// as a count. GET /tasks/{id} applies the same filter to the async path.
func visibleGroupBatchResults(d *Deps, p *rbac.VerifiedPrincipal, results []BatchResult) []BatchResult {
	return filterByRBAC(d.RBAC, d.TenantOrg, p, results,
		func(r BatchResult) string { return r.TenantID }, rbac.PermRead)
}
