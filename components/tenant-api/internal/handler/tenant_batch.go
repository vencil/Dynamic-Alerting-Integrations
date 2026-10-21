package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"sort"
	"strings"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/customalerts"
	"github.com/vencil/tenant-api/internal/generator"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
	"gopkg.in/yaml.v3"
)

// BatchOperation describes a single operation in a batch request.
//
// `Patch` map shape can't be expressed in struct-tag rules; per-key
// validation lives in `body_validator.go::validateBatchEdit` (patch keys via
// validatePatchMap, unset keys against unsetAllowedKeys).
type BatchOperation struct {
	TenantID string `json:"tenant_id" validate:"required,min=1,max=256"`
	// key → value to set (e.g., "_silent_mode": "warning"); at most 1000 entries.
	// May be omitted; an operation with neither patch nor unset changes nothing.
	Patch map[string]string `json:"patch" validate:"max=1000"`
	// keys to remove from the tenant's config block; at most 1000 entries. Only "_routing" and
	// "_routing_receiver_type" are accepted: removing `_routing` resets the tenant to
	// `_routing_defaults` and its routing profile, which turns routing back on for a tenant a
	// disabling `_routing` turned off (the route generator judges the result, as every
	// write); removing `_routing_receiver_type` drops the flat key tenant-api used to write,
	// which nothing renders (#2983). A key the tenant does not carry, or a
	// tenant that does not exist, makes it a no-op: with no patch nothing is written and no
	// tenant is created. A key must not appear twice, nor in both patch and unset.
	Unset []string `json:"unset,omitempty" validate:"max=1000"`
	// skipAbsent: the op comes from a group's stored member list
	// (POST /groups/{id}/batch, #2882). A member with no tenant section on
	// the base the write merges into is skipped (status "skipped"), not
	// created: a stale member list must not bring back an offboarded
	// tenant. Not part of the request body — POST /tenants/batch naming an
	// absent tenant is the caller's explicit intent and still creates it.
	skipAbsent bool
}

// BatchRequest is the body for POST /api/v1/tenants/batch.
type BatchRequest struct {
	Operations []BatchOperation `json:"operations" validate:"required,min=1,max=1000,dive"`
}

// BatchResult is the per-tenant result in a batch response.
type BatchResult struct {
	TenantID string `json:"tenant_id"`
	// "ok" | "error" | "not_applied"; PR mode: "included" for an op taken into
	// the PR/MR. A batch is all or nothing: when any op is "error", nothing in
	// the batch is written and every op that was not refused itself is
	// "not_applied". A group batch also reports "skipped" for a member that
	// does not exist on the base the write merges into (message "tenant does
	// not exist"); nothing is written for it, and it does not fail the batch.
	Status  string `json:"status"`
	Message string `json:"message,omitempty"`
	// Code is a machine-readable error code for a FAILED op, set only for the
	// failure classes a client is expected to branch on: TENANT_DECLARED_ELSEWHERE,
	// TENANT_CONFIG_NOT_LOADABLE (the tenant file cannot be loaded as a tenant
	// config, #2373), TENANT_GIT_MANAGED (the op's tenant is git-managed, #3064:
	// nothing written, change it in git), INTERNAL_ERROR (the conf.d walk behind
	// that check could not
	// run, or another server-side failure), the route generator's codes
	// (GENERATOR_FINDINGS, GENERATOR_REJECTED, VALIDATOR_UNAVAILABLE,
	// POLICY_UNAVAILABLE; #2903), and — PR mode, judged on the latest base branch —
	// FORBIDDEN (write scope) and CONFIG_DECODE_ERROR (the base's
	// _tenant_orgs.yaml cannot be read). Empty for every other failure and for an
	// op that was not refused.
	Code string `json:"code,omitempty"`
	// Warnings carries non-blocking advisories for an op that SUCCEEDED
	// (#1231 deprecated-key alias notices from the direct write path).
	// Error results never carry warnings — Message owns the failure text.
	Warnings []string `json:"warnings,omitempty"`
}

// BatchResponse is the full response for POST /api/v1/tenants/batch.
type BatchResponse struct {
	Status   string        `json:"status"`              // "completed" | "pending_review" (PR mode) | "rejected" (HTTP 422: an op was refused, nothing written)
	TaskID   string        `json:"task_id,omitempty"`   // async task ID
	PRURL    string        `json:"pr_url,omitempty"`    // v2.6.0: PR/MR URL in PR mode
	PRNumber int           `json:"pr_number,omitempty"` // v2.6.0: PR/MR number in PR mode
	Results  []BatchResult `json:"results"`
	Summary  string        `json:"summary"`           // e.g., "5 succeeded"; a rejected batch: "1 failed, 4 not applied"
	Message  string        `json:"message,omitempty"` // v2.6.0: human-readable message
	// Warnings is the batch-level advisory list for PR mode (#1231): the
	// per-op notices aggregated by WritePRBatch. Batch-level (not per-result)
	// because PR-mode Results are frozen at "included" BEFORE the write runs;
	// each notice self-identifies its tenant (`tenant=<id>` in the wording).
	Warnings []string `json:"warnings,omitempty"`
	// Findings is set on a batch the generator container refused with
	// findings (#2903, ADR-036 §4; every result then has code
	// GENERATOR_FINDINGS): its --findings-json entries for the tree the
	// batch would produce, the ones naming a file or tenant of the batch
	// first and marked related; pre_existing when the base was judged too
	// (FindingsComparedWithBase). Unrelated findings only for a caller with
	// write permission on all tenants; others get UnrelatedFindingsWithheld.
	Findings                  []generator.Finding `json:"findings,omitempty"`
	FindingsComparedWithBase  *bool               `json:"findings_compared_with_base,omitempty"`
	UnrelatedFindingsWithheld int                 `json:"unrelated_findings_withheld,omitempty"`
}

// BatchTenants handles POST /api/v1/tenants/batch
//
// Executes a list of patch operations synchronously (default) or asynchronously (if ?async=true),
// all or nothing in one commit (#2900): every op passes the checks, or nothing is written.
// The sync.Mutex inside gitops.Writer ensures serial execution.
// Response includes task_id for async tracking.
//
// v2.6.0 Phase E: Supports both GitHub PRs and GitLab MRs via platform.Client interface.
//
// Query Parameters:
//
//	?async=true  — Enable async mode; returns 202 with task_id for polling
//	(default)    — Sync mode; returns 200 with completed results
//
// @Summary     Batch tenant operations
// @Description Apply patch operations to multiple tenants in one call.
// @Description All or nothing, in one commit: every operation is checked (tenant id, write permission, then merged and validated under the write lock on the tree the earlier operations leave; the route generator container then judges the tree with the whole batch applied, #2903),
// @Description and only when every one passes are the changed files committed together — one commit in direct mode, a PR/MR with one commit in PR write-back mode.
// @Description When any operation is refused, nothing is written: 422 with status rejected, the refused operations with status error (message, code) and every other operation with status not_applied.
// @Description With ?async=true (direct mode) the same verdict is reached when the task runs; a rejected batch ends the task as failed, with the same per-operation results.
// @Description The batch is applied and judged in a separate staging git worktree; the branch is advanced first, then the served config files, so readers never see content that was not committed (while the files are updated they may see some of the batch's files new and others old, all committed).
// @Description An operation may also remove keys with unset (only "_routing" and "_routing_receiver_type"): unset ["_routing"] turns routing back on for a tenant a disabling _routing turned off; unset ["_routing_receiver_type"] removes the flat key tenant-api used to write, which nothing renders and which, while on disk, makes every other write to the tenant fail key validation (#2983). A patch may not write a _routing-prefixed key other than _routing and _routing_profile (400 INVALID_BODY).
// @Description Direct mode: an operation whose tenant config file cannot be loaded as a tenant config (config_error malformed_yaml | invalid_config)
// @Description is refused with code TENANT_CONFIG_NOT_LOADABLE (and rejects the batch); repair the tenant file itself first.
// @Description Also config_error not_round_trippable (#2903): re-encoding the tenant file would change content the update does not edit (the message names the first difference inside the tenant's own block); rewrite the file with a whole-file PUT.
// @Tags        tenants
// @Accept      json
// @Produce     json
// @Param       body body     BatchRequest true "Batch operations"
// @Param       async query   string       false "Enable async mode (true/false)"
// @Success     200  {object} BatchResponse
// @Success     202  {object} map[string]interface{}
// @Failure     400  {object} ErrorResponse
// @Failure     403  {object} ErrorResponse "PR write-back mode: the forge token lacks write scope to open the PR/MR. (An operation refused by the write scope, on the local copy or on the latest base branch, rejects the batch with a 422 instead.)"
// @Failure     409  {object} ErrorResponse "Direct mode, code CONFLICT, with committed: false — the repository changed outside tenant-api while the batch ran; nothing was written, refresh and retry. With committed: true and commit — the batch WAS committed (that commit) but the write did not finish cleanly on the server; refresh before changing anything."
// @Failure     413  {object} ErrorResponse
// @Failure     422  {object} BatchResponse "Batch rejected (status rejected): at least one operation was refused — by the checks on this server's copy, or (PR write-back mode) on the latest base branch the PR/MR would be cut from — so nothing was written and no PR/MR was opened. results lists every operation in request order: status error (message, code: e.g. FORBIDDEN, TENANT_DECLARED_ELSEWHERE, TENANT_FILE_DIRTY (direct mode, #1535: the operation's tenant file differs from HEAD in tenant-api's copy), TENANT_CONFIG_NOT_LOADABLE, TENANT_TREE_UNREADABLE, CONFIG_DECODE_ERROR, INTERNAL_ERROR) for the refused ones, not_applied for the rest. TENANT_TREE_UNREADABLE (#2939): the tree after the batch holds an unresolvable symlink (dangling, looping, or with a non-directory in its path) that no operation replaces; every operation not refused for another reason carries it; an operation whose own tenant file cannot be read (and is not an unresolvable symlink (dangling, looping, or with a non-directory in its path)) is TENANT_CONFIG_NOT_LOADABLE, and the message names the entries only for a caller with write permission on all tenants (otherwise their number); an async task's stored results never name them. Also when the route generator container refuses the batch as a whole (#2903, ADR-036 §4: the tree with the whole batch applied is judged once): every result is status error with code GENERATOR_FINDINGS (its message names the findings about that result's tenant) and findings[] / findings_compared_with_base / unrelated_findings_withheld carry the findings as for PUT /tenants/{id}; or code GENERATOR_REJECTED (memory, incomplete, too_large: the files the batch produces are too large to judge in one request — split the batch, invalid_request, normalized: git attributes would commit other bytes than the ones judged). An async batch's task carries the same per-op results."
// @Failure     429  {object} ErrorResponse "Too many requests: code GENERATOR_REJECTED (#2903) — the route generator container gave no verdict this time, retry (Retry-After): reason busy, deadline, git_read, mismatch (what it judged is still not what would be written after one re-judgement) or no_time (too little of the request's time left to judge). Nothing written. Also code RATE_LIMITED (per-caller rate limit)"
// @Failure     500  {object} ErrorResponse "Internal error; or code TENANT_STORE_FAULT (#2939): tenant-api's own copy of conf.d holds an entry the server cannot read and git cannot hold (a FIFO, socket or device named like a config file, a failed read, an unlistable directory); the batch fails whole, nothing written — it is fixed on the server or its volume, not by changing operations. Entry names as for the 422; an async task's stored results never name them"
// @Failure     503  {object} ErrorResponse "Service unavailable: the write plane is busy (WRITE_OVERLOADED), the forge is degraded (FORGE_UNAVAILABLE) or the config worktree was on a PR branch (TREE_NOT_ON_BASE). Also, for every write that changes a conf.d file (#2903, ADR-036 §5): code VALIDATOR_UNAVAILABLE when the route generator container cannot judge (reason unreachable, bad_response, amtool_missing, generator_too_old, internal_error), or POLICY_UNAVAILABLE when its deployment policy file is unusable (reason policy_unusable, or policy_unreadable for tenant-api's own copy); Retry-After; nothing written. In PR/MR write-back mode, code GIT_PUSH_FAILED (#1724) when pushing the batch branch to the config repository failed, a timeout included: the whole batch fails, no PR/MR opened, Retry-After (the branch may be left on origin without a PR/MR). Also code UPSTREAM_ERROR (#1535, #3173) when a synchronous batch was stopped before anything of it was committed: the request was cancelled or reached its deadline, or a git command hit its own deadline (TENANT_API_GIT_TIMEOUT) and the server read the branch back unmoved; nothing written, safe to retry. And code UPSTREAM_ERROR with another message when updating the branch failed and the server could not read the branch back: unlike the cases above the batch may already be committed, so refresh to see whether it was before retrying (an async task says the same in its error)"
// @Router      /api/v1/tenants/batch [post]
func BatchTenants(d *Deps) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		email := rbac.RequestEmail(r)
		// Capture the verified principal VALUE for the async path below —
		// same discipline as email: the closure must never reach back into
		// the request context (it outlives the HTTP request), so it captures
		// the immutable principal snapshot, not r / r.Context().
		p := rbac.RequestPrincipal(r)

		// #1722: this endpoint read its body with a bare json.NewDecoder and so
		// had NO size cap — d.MaxBody() only ever reached the handlers that call
		// readLimitedBody, and there is no body-size middleware. That mattered
		// here more than anywhere else: WritePRBatch takes the single-writer
		// token BEFORE its pre-flight and validates every op twice inside it, so
		// an unbounded batch body is parsed repeatedly while every other tenant's
		// write waits behind the token.
		batchLimit, batchKnob := d.BatchBodyLimit()
		body, ok := readBodyWithin(rw, r, batchLimit, batchKnob)
		if !ok {
			return
		}
		var req BatchRequest
		if err := json.NewDecoder(bytes.NewReader(body)).Decode(&req); err != nil {
			WriteJSONError(rw, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if len(req.Operations) == 0 {
			WriteJSONError(rw, r, http.StatusBadRequest, "operations list is empty")
			return
		}

		// v2.8.0 issue #134 — body-content range validation (Phase B
		// Track C C4). Run BEFORE any RBAC / per-op work so a malformed
		// body fails fast with the full violation list (one round-trip
		// for the operator to fix everything, not retry-and-discover).
		violations := ValidateStructTags(&req)
		for i, op := range req.Operations {
			fieldPrefix := fmt.Sprintf("operations[%d]", i)
			violations = append(violations, validateBatchEdit(op.Patch, op.Unset, fieldPrefix)...)
		}
		if len(violations) > 0 {
			WriteValidationErrors(rw, r, violations)
			return
		}

		// #3123: an opaque random id (it used to be batch-<date>-<op count>).
		taskID := async.NewTaskID()

		// v2.6.0: PR-based write-back mode for batch operations (ADR-011)
		// All operations are consolidated into a single PR/MR.
		// Supports both GitHub PRs and GitLab MRs via platform interfaces.
		if d.prWritePath() {
			batchTenantsPRMode(d, rw, r, req, email, p)
			return
		}

		// v2.6.0: Async mode — submit to goroutine pool and return immediately
		if r.URL.Query().Get("async") == "true" && d.Tasks != nil {
			submitTask(rw, r, d, taskID, p, batchTaskFunc(d, req.Operations, email, p))
			return
		}

		// Synchronous mode (default). #2900: all or nothing.
		run := d.executeBatch(r.Context(), r.Context(), req.Operations, email, p, batchResultsAnswer)
		switch {
		case run.Err != nil && generatorRejectsBatch(run.Err):
			// #2903: refused by the generator — a rejected batch, as any.
			writeJSON(rw, http.StatusUnprocessableEntity, rejectedByGenerator(d, r, taskID, run.Results, run.Err))
		case run.Err != nil:
			writeBatchFailure(rw, r, d, run.Err)
		case run.Rejected:
			writeJSON(rw, http.StatusUnprocessableEntity, rejectedBatchResponse(taskID, run.Results))
		default:
			writeJSON(rw, http.StatusOK, BatchResponse{
				Status:  "completed",
				TaskID:  taskID,
				Results: run.Results,
				Summary: summarizeBatchResults(run.Results),
			})
		}
	}
}

// batchPRMeta is what a batch PR/MR says about where it came from. Everything
// else about PR-mode batch writes is shared by runBatchPR.
type batchPRMeta struct {
	// title renders the PR/MR title from the number of ops taken into it.
	title  func(included int) string
	source string // the PR body's **Source:** line
	labels []string
}

var tenantBatchPRMeta = batchPRMeta{
	title:  func(n int) string { return fmt.Sprintf("[tenant-api] Batch update %d tenants", n) },
	source: "tenant-manager UI (batch)",
	labels: []string{"tenant-api", "auto-generated", "batch"},
}

// batchTenantsPRMode handles a batch request in PR write-back mode (ADR-011):
// all operations are consolidated into a single PR/MR (GitHub or GitLab via
// the platform interfaces). Split out of BatchTenants (Cycle 10 refactor) to
// keep the handler readable — behavior is unchanged. The caller must have
// verified IsPRMode && PRClient != nil && PRTracker != nil. Always writes a
// response.
func batchTenantsPRMode(d *Deps, rw http.ResponseWriter, r *http.Request, req BatchRequest, email string, p *rbac.VerifiedPrincipal) {
	if resp, status, ok := runBatchPR(d, rw, r, req.Operations, email, p, tenantBatchPRMeta); ok {
		writeJSON(rw, status, resp)
	}
}

// runBatchPR is the PR write-back core shared by POST /tenants/batch and
// POST /groups/{id}/batch (#2339): per-op RBAC, one
// WritePRBatch, one PR/MR with one commit. All or nothing (#2900): an op
// refused by those checks rejects the whole batch — no branch, no PR/MR — and
// the 422 body lists every op. On an error it writes the error response itself
// and returns ok=false; otherwise it returns the body and its HTTP status (200,
// or 422 for a rejected batch) for the caller to write (the group endpoint
// wraps it with its group id). The caller must have verified d.prWritePath().
func runBatchPR(d *Deps, rw http.ResponseWriter, r *http.Request, ops []BatchOperation, email string, p *rbac.VerifiedPrincipal, meta batchPRMeta) (BatchResponse, int, bool) {
	// #3039: the answer goes to this caller, so it may name the tenants the
	// caller may read.
	r = withTenantNames(r, d)
	names := gitops.TenantNamesOf(r.Context())
	// Pre-validate all ops (RBAC) before creating any branch
	var batchOps []gitops.PRBatchOp
	var batchResults []BatchResult
	// included[t]: t's ops already taken into this PR, in order — each with
	// its patch AND its unset. WritePRBatch merges them all onto one base in
	// that order, so the metadata post-state check must see them stacked
	// (postStateScopeMeta). A refused op is never appended: nothing of it
	// lands.
	included := map[string][]BatchOperation{}
	metaOf := WriteScopeMeta(d.ConfigDir) // one resolver: the root platform files are read once per batch
	// lastPost[t]: the local post-state pair of t's last op taken into the PR.
	lastPost := map[string]writeGate{}
	// skipped: per op taken into the PR, its result's index and whether the
	// in-lock merge found its tenant absent (#2882); read after WritePRBatch.
	var skipped []skippedOp
	for i, op := range ops {
		if err := ValidateWritableTenantID(op.TenantID); err != nil {
			batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "error", Message: err.Error()})
			continue
		}
		gatePre := writeGate{scopePairOf(metaOf, op.TenantID), gateOrgs(d, op.TenantID)}
		if !OrgAllowed(d.RBAC, d.TenantOrg, p, op.TenantID, rbac.PermWrite, fixedScopeMeta(gatePre.pair)) {
			batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "error", Message: "insufficient permissions"})
			continue
		}
		gatePost := writeGate{scopePairOf(postStateScopeMeta(d.ConfigDir, included[op.TenantID], op), op.TenantID), gatePre.orgs}
		if res, refused := gateBatchOpPostStateAt(gatePost.pair, op, p, d.RBAC, d.TenantOrg); refused {
			batchResults = append(batchResults, res)
			continue
		}
		// #1097: carry a merge closure, not pre-built content, so the
		// authoritative partial merge runs under the writer lock against
		// the fresh base (preserving untouched keys). op is per-iteration
		// (Go 1.22+), so the closure binds this op's TenantID/Patch/Unset.
		op := op
		merge := func(existing []byte) (string, error) {
			if op.skipAbsent && tenantAbsentIn(existing, op.TenantID) {
				return "", gitops.ErrMergeNoOp
			}
			return mergePatchYAML(existing, op.TenantID, op.Patch, op.Unset, names)
		}
		// #2882: set only by FreshMerge, the in-lock merge onto the fresh
		// base; the pre-flight Merge above reads the local tree and decides
		// nothing.
		absent := new(bool)
		skipped = append(skipped, skippedOp{result: len(batchResults), absent: absent})
		// #2857: the checks above read the pod's local tree, which may lag
		// the base the branch is cut from, so they can only refuse early.
		// The authoritative verdict is FreshMerge's, on that base, in the
		// same order: the metadata write scope before and after the op; the
		// writer validates after, and the generator judges the result
		// (#2903). existing there is
		// the tenant's file on the base with this request's earlier ops for
		// it already applied. A refusal aborts the whole batch, nothing
		// written.
		//
		// The pairs passed down are the ones judged on the local tree, not
		// read again. On the base, the pre-state of an op after the tenant's
		// first is its file with this request's earlier ops applied, so it
		// is compared with the local post-state of the op before it.
		comparePre := gatePre
		prev, seen := lastPost[op.TenantID]
		if seen {
			comparePre = prev
		}
		firstOp := !seen
		lastPost[op.TenantID] = gatePost
		batchOps = append(batchOps, gitops.PRBatchOp{
			TenantID: op.TenantID,
			Merge:    merge,
			FreshMerge: func(baseDir string, existing []byte) (string, error) {
				// baseDir: conf.d of the staging worktree at the fresh base (#2900).
				// #2882: an absent group member is skipped before any verdict:
				// nothing is written for it, so there is nothing to judge.
				*absent = op.skipAbsent && tenantAbsentIn(existing, op.TenantID)
				if *absent {
					return "", gitops.ErrMergeNoOp
				}
				// The org axis is counted on the tenant's first op only:
				// its organizations are the same for all of them.
				countOrg := firstOp
				pre, judged := freshWriteScope(baseDir, op.TenantID)
				if judged {
					if ok, err := freshScopeAllowed(d, p, baseDir, op.TenantID, comparePre, pre, countOrg); err != nil || !ok {
						return "", orFreshScopeError(err, &freshBaseScopeError{TenantID: op.TenantID, Op: i, PreState: true})
					}
					countOrg = false
				}
				merged, err := merge(existing)
				if err != nil {
					return "", err
				}
				if touchesScopeMetadata(op) || countOrg {
					post := scopePairOf(proposedScopeMeta(baseDir, merged), op.TenantID)
					if ok, err := freshScopeAllowed(d, p, baseDir, op.TenantID, gatePost, post, countOrg); err != nil || !ok {
						return "", orFreshScopeError(err, &freshBaseScopeError{TenantID: op.TenantID, Op: i})
					}
				}
				return merged, nil
			},
		})
		batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "included"})
		included[op.TenantID] = append(included[op.TenantID], op)
	}

	// #2900: one refused op rejects the whole batch — no branch, no PR/MR.
	// The ops the checks let through are reported as not applied.
	if len(batchOps) < len(ops) {
		for i := range batchResults {
			if batchResults[i].Status == "included" {
				batchResults[i] = BatchResult{TenantID: batchResults[i].TenantID, Status: resultNotApplied, Message: msgNotApplied}
			}
		}
		return rejectedBatchResponse("", batchResults), http.StatusUnprocessableEntity, true
	}

	// #3054: one write attempt for the whole batch (the branch and the
	// PR/MR), counted by the answer it gets — written here on an error, or
	// returned to the caller (answered).
	attempt := d.startWriteAttempt(rw, r)
	defer attempt.end()
	rw = attempt
	answer := func(resp BatchResponse, status int) (BatchResponse, int, bool) {
		attempt.answered(status)
		return resp, status, true
	}

	result, err := d.Writer.WritePRBatch(r.Context(), batchOps, email)
	// #2882: the members found absent on the fresh base were not written;
	// their results and the PR's tenant list say so. Meaningful only when
	// the batch was not aborted (every FreshMerge ran).
	var written []gitops.PRBatchOp
	for i, s := range skipped {
		if *s.absent {
			batchResults[s.result] = BatchResult{TenantID: batchOps[i].TenantID, Status: "skipped", Message: msgTenantAbsent}
			continue
		}
		written = append(written, batchOps[i])
	}
	if err != nil {
		// #2900 (F4): WritePRBatch judged every op on the fresh base; any
		// refusal rejects the batch — the same 422 with per-op results as a
		// refusal the local pre-check finds, nothing written, no PR/MR.
		var rejected *gitops.BatchRejectedError
		if errors.As(err, &rejected) {
			for i, s := range skipped {
				o := rejected.Outcomes[i]
				id := batchOps[i].TenantID
				switch {
				case o.Err != nil:
					batchResults[s.result] = freshBaseOpResult(id, o.Err, treePathsVisible(d.RBAC, p))
				case *s.absent:
					batchResults[s.result] = BatchResult{TenantID: id, Status: "skipped", Message: msgTenantAbsent}
				default:
					batchResults[s.result] = BatchResult{TenantID: id, Status: resultNotApplied, Message: msgNotApplied}
				}
			}
			return answer(rejectedBatchResponse("", batchResults), http.StatusUnprocessableEntity)
		}
		// #2903: the generator container refused the batch as a whole — a
		// rejected batch (422, every op carrying the refusal), as above.
		if generatorRejectsBatch(err) {
			for i, s := range skipped {
				id := batchOps[i].TenantID
				if *s.absent {
					batchResults[s.result] = BatchResult{TenantID: id, Status: "skipped", Message: msgTenantAbsent}
					continue
				}
				batchResults[s.result], _ = generatorOpResult(id, err)
			}
			return answer(rejectedByGenerator(d, r, "", batchResults, err), http.StatusUnprocessableEntity)
		}
		// TRK-320 ErrWriteOverloaded / TRK-318 ErrForgeDegraded → canonical
		// retry-hinting 503s (shared with the single-write path).
		if writeWriteFlowError(rw, r, d, err) {
			return BatchResponse{}, 0, false
		}
		// #1724: the batch branch did not reach origin — one 503
		// GIT_PUSH_FAILED for the whole batch, no PR/MR, no pending_review.
		if writePushFailed(rw, r, err) {
			return BatchResponse{}, 0, false
		}
		// #1102: an all-no-op batch (idempotent patch / retry) produced no
		// commits — return a clean "no changes" success, never a forge error.
		// #1231 F5: ErrNoChanges is the one error WritePRBatch pairs with a
		// non-nil result — it carries the per-op deprecation notices, and the
		// no-changes 200 must keep that migration signal (the file still
		// carries the deprecated spelling whether or not this patch changed
		// bytes; same invariant as WriteMerged's no-op path).
		if errors.Is(err, gitops.ErrNoChanges) {
			var warnings []string
			if result != nil {
				warnings = result.Notices
			}
			return answer(BatchResponse{
				Status:   "completed",
				Results:  batchResults,
				Summary:  prBatchSummary("completed", batchResults),
				Message:  "No changes to apply; no PR/MR created.",
				Warnings: warnings,
			}, http.StatusOK)
		}
		// #1724: cancelled, or a git command past its own deadline → the 503
		// the direct batch answers (#1535), not the generic 500.
		if writeWriteCancelled(rw, r, err) {
			return BatchResponse{}, 0, false
		}
		// Anything else is an unexpected git failure → generic 500 (its text
		// stays in the log — WriteErrorEnvelope, #1700).
		WriteJSONError(rw, r, http.StatusInternalServerError, "PR/MR batch write failed: "+err.Error())
		return BatchResponse{}, 0, false
	}

	// PR-6/11: shared post-write flow via createPRAndRegister.
	// Per-tenant tracker entries get every field of the PR
	// response (Title / HeadRef / CreatedAt) preserved
	// consistently with the single-tenant path.
	prTitle := meta.title(len(written))
	tenantList := make([]string, len(written))
	for i, op := range written {
		tenantList[i] = op.TenantID
	}
	prBody := fmt.Sprintf("**Operator:** %s\n**Source:** %s\n**Tenants:** %s",
		email, meta.source, strings.Join(tenantList, ", "))
	pr, err := createPRAndRegister(d,
		prTitle, prBody, result.BranchName,
		meta.labels,
		tenantList,
	)
	if err != nil {
		// Shared with the single-write path: forbidden → clean 403 (previously
		// the batch path was missing this and leaked a generic 503),
		// circuit-open → sanitized 503, else generic 503.
		writeForgeCreateError(rw, r, d.PRClient.ProviderName(), err)
		return BatchResponse{}, 0, false
	}

	return answer(BatchResponse{
		Status:   "pending_review",
		PRURL:    pr.WebURL,
		PRNumber: pr.Number,
		Results:  batchResults,
		Summary:  prBatchSummary("pending_review", batchResults),
		Message:  fmt.Sprintf("Batch PR/MR created with %d tenant changes.", len(written)),
		Warnings: result.Notices,
	}, http.StatusOK)
}

// prBatchSummary renders runBatchPR's summary line from its status and the
// per-op results, in which every op taken into the PR is "included" and every
// other op is a failure. It reads only the results it is given, so the group
// endpoint can re-render it over the results the caller may see (#1530) and
// get the same wording as for a /tenants/batch response.
func prBatchSummary(status string, results []BatchResult) string {
	included, skipped := 0, 0
	for _, res := range results {
		switch res.Status {
		case "included":
			included++
		case "skipped":
			skipped++
		}
	}
	failed := len(results) - included - skipped
	var summary string
	switch {
	case included == 0 && (failed > 0 || skipped == 0):
		summary = fmt.Sprintf("%d failed", failed)
	case included == 0:
		// Every op taken in was a skipped group member (#2882).
		return fmt.Sprintf("%d skipped", skipped)
	case status == "pending_review":
		summary = fmt.Sprintf("%d included in PR/MR, %d failed", included, failed)
	default:
		// "completed" with ops taken in: WritePRBatch found nothing to change.
		summary = fmt.Sprintf("%d unchanged", included)
	}
	if skipped > 0 {
		summary += fmt.Sprintf(", %d skipped", skipped)
	}
	return summary
}

// Result and response statuses of an all-or-nothing batch (#2900).
const (
	// batchStatusRejected is BatchResponse.Status of a batch refused because
	// of its operations: nothing was written (HTTP 422).
	batchStatusRejected = "rejected"
	// resultNotApplied is BatchResult.Status of an op that was not refused
	// itself but was not written either, because another op of its batch was.
	resultNotApplied = "not_applied"
	msgNotApplied    = "not written: another operation in this batch was refused, so nothing in the batch was written"
)

// directBatchRun is how a direct-mode batch ended (#2900).
//
//   - Rejected: at least one op was refused; Results says which (status
//     "error") and which were not written because of it ("not_applied"; a
//     group member that does not exist stays "skipped"). Nothing was written.
//   - Err != nil, Committed == "": the batch failed as a whole — the write
//     plane was busy, the tree was on a PR branch, it was cancelled, the
//     serving branch moved outside tenant-api (gitops.ErrConflict), the commit
//     failed. Results gives every op the same "error" text. Nothing was
//     written — except when Err is gitops.WriteOutcomeUnknownError (#3173):
//     the ref update failed and the branch could not be read back, so the
//     batch may be on it, and nothing may say it was not.
//   - Err != nil, Committed != "": the batch IS committed (that commit) but
//     the write did not finish cleanly (gitops.BatchCommittedError). Results
//     are the written ops' ("ok"); the caller must not say nothing was written.
//   - otherwise every op was accepted, and whatever changed is in ONE commit.
type directBatchRun struct {
	Results   []BatchResult
	Rejected  bool
	Err       error
	Committed string
}

// executeBatchOps runs a direct-mode batch, all or nothing (#2900): every op
// is judged first with the checks the batch has always run — tenant id,
// org-scoped write permission before and after the op — and when one
// refuses, nothing is written.
// Otherwise gitops.Writer.WriteBatch merges and validates every op under the
// writer lock and commits them together, or refuses them together. Shared by
// the sync and async paths, and by the tenant and group endpoints.
//
// An op is judged on the tenant's file with the same request's earlier ops for
// that tenant laid over it (prior), the state it will be merged onto — as the
// PR-mode batch always did. Before #2900 each op was written before the next
// was judged, which read the same state from disk.
//
// The per-op permission check is org-scope-aware (ADR-027 / LD-6 P4b) and the
// tenant's org list is resolved here, at execution time — not at submit time.
// The async path runs this after the HTTP request has returned, so a
// submit-time snapshot could authorize against orgs that a _tenant_orgs.yaml
// hot-reload has since changed (stale-orgs hazard).
//
// results is batchResultsAnswer (the synchronous answer, read only by the
// caller who sent it) or batchResultsStored (an async task's results, kept
// in the task manager and read by whoever polls the task). Only an answer
// may name conf.d entries in a TENANT_TREE_UNREADABLE result, and only to a
// caller treePathsVisible admits: stored results never do, whoever submitted
// — the poller is not the submitter (#2939 D3a). The log always names them.
//
// The same split decides which other tenants a validation message or a
// partial-write refusal may name (#3039): an answer, those the caller may
// read (tenantNamesFor); stored results, none.
func executeBatchOps(ctx context.Context, w *gitops.Writer, configDir string, ops []BatchOperation, email string, p *rbac.VerifiedPrincipal, rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, results batchResultsUse) directBatchRun {
	treeVisible := results == batchResultsAnswer && treePathsVisible(rbacMgr, p)
	var names gitops.TenantNames
	if results == batchResultsAnswer {
		names = tenantNamesFor(rbacMgr, tenantOrg, p)
	}
	ctx = gitops.WithTenantNames(ctx, names)
	return executeBatchOpsShown(ctx, w, configDir, ops, email, p, rbacMgr, tenantOrg, treeVisible)
}

// executeBatch is executeBatchOps for an endpoint or an async task, counting
// the run as one write attempt (#3054, batchRunResult): a direct-mode batch is
// one commit, so one attempt however many tenants it names. Every direct
// batch — sync and async, /tenants/batch and /groups/{id}/batch — runs
// through here. client is the request's own context for a sync batch (a run
// that failed because the client went away is not counted, clientGone) and
// nil for an async task, which no client's leaving cancels.
func (d *Deps) executeBatch(ctx, client context.Context, ops []BatchOperation, email string, p *rbac.VerifiedPrincipal, results batchResultsUse) directBatchRun {
	run := executeBatchOps(ctx, d.Writer, d.ConfigDir, ops, email, p, d.RBAC, d.TenantOrg, results)
	if res, counted := batchRunResult(run); counted && (res == writeResultOK || !clientGone(client)) {
		d.writeResults().record(res)
	}
	return run
}

// batchRunResult is the write result of a direct-mode batch run (#3054), or
// false when it is not counted. It gives the result the answer of the sync
// endpoint has (writeResultOfStatus over writeBatchFailure's status), so an
// async task — which has no HTTP answer — is counted the same:
//
//   - rejected (422: an op refused, or the generator's findings): not counted
//   - written: ok
//   - committed but not finished cleanly (409 committed: true): error
//   - otherwise batchFailureResult
func batchRunResult(run directBatchRun) (writeResult, bool) {
	switch {
	case run.Rejected:
		return 0, false
	case run.Err == nil:
		return writeResultOK, true
	case run.Committed != "":
		return writeResultError, true
	case generatorRejectsBatch(run.Err):
		return 0, false
	}
	return batchFailureResult(run.Err)
}

// batchFailureResult is the result of the status writeBatchFailure answers
// err with; TestBatchFailureResult_MatchesTheSyncAnswer holds the two
// together. 409 — the serving branch moved (gitops.ErrConflict), two files
// claim the tenant, another file declares it, an unresolvable link in the
// tree, the tenant's own file unreadable — is conflict; an invalid tenant id
// (400) is not counted; everything else (busy, cancelled, store fault, git) is
// error.
func batchFailureResult(err error) (writeResult, bool) {
	var (
		tree   *gitops.TreeUnreadableError
		target *gitops.TargetUnreadableError
	)
	switch {
	case errors.As(err, &tree):
		if _, _, status := treeRefusal(tree); status == http.StatusConflict {
			return writeResultConflict, true
		}
		return writeResultError, true
	case errors.As(err, &target),
		errors.Is(err, gitops.ErrTenantDeclaredElsewhere),
		errors.Is(err, gitops.ErrTenantFileDirty),
		errors.Is(err, confd.ErrAmbiguousTenantFile),
		errors.Is(err, gitops.ErrConflict):
		return writeResultConflict, true
	case errors.Is(err, gitops.ErrInvalidTenantID):
		return 0, false
	}
	return writeResultError, true
}

// batchResultsUse says who reads a direct-mode batch's results (see
// executeBatchOps).
type batchResultsUse int

const (
	// batchResultsAnswer: the synchronous response, to the caller who sent it.
	batchResultsAnswer batchResultsUse = iota
	// batchResultsStored: an async task's results, served to every poller.
	batchResultsStored
)

// executeBatchOpsShown is executeBatchOps with the disclosure decided:
// treeVisible names the entries of a TENANT_TREE_UNREADABLE result.
func executeBatchOpsShown(ctx context.Context, w *gitops.Writer, configDir string, ops []BatchOperation, email string, p *rbac.VerifiedPrincipal, rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, treeVisible bool) directBatchRun {
	results := make([]BatchResult, len(ops))
	refused := make([]bool, len(ops))
	anyRefused := false
	refuse := func(i int, res BatchResult) {
		results[i], refused[i], anyRefused = res, true, true
	}
	// included[t]: t's earlier ops of this request that passed, in order.
	included := map[string][]BatchOperation{}
	for i, op := range ops {
		// A resolver per op: its metadata, the root platform layer included
		// (#2370), is read when that op is judged.
		if res, failed := gateBatchOp(op.TenantID, p, rbacMgr, tenantOrg, WriteScopeMeta(configDir)); failed {
			refuse(i, res)
			continue
		}
		if res, failed := gateBatchOpPostState(configDir, included[op.TenantID], op, p, rbacMgr, tenantOrg); failed {
			refuse(i, res)
			continue
		}
		included[op.TenantID] = append(included[op.TenantID], op)
	}
	if anyRefused {
		for i, op := range ops {
			if !refused[i] {
				results[i] = BatchResult{TenantID: op.TenantID, Status: resultNotApplied, Message: msgNotApplied}
			}
		}
		return directBatchRun{Results: results, Rejected: true}
	}
	return writeBatchOps(ctx, w, ops, email, treeVisible)
}

// writeBatchOps is the write half of executeBatchOps: one WriteBatch for every
// op, each a PARTIAL update (#1097) — mergePatchYAML merges the patch into the
// tenant's existing keys under the writer lock, so keys the patch does not
// name (other thresholds, `_metadata`, `_custom_alerts`) and the file's
// comments survive. treeVisible
// is treePathsVisible for the caller: whether a TENANT_TREE_UNREADABLE result
// names the entries (batchErrorResult).
func writeBatchOps(ctx context.Context, w *gitops.Writer, ops []BatchOperation, email string, treeVisible bool) directBatchRun {
	// #2882: judged inside the merge, i.e. under the writer lock on the base
	// the write lands on, not on a read taken before it.
	absent := make([]bool, len(ops))
	names := gitops.TenantNamesOf(ctx) // the Writer's validation reads the same off ctx
	batch := make([]gitops.BatchOp, len(ops))
	for i, op := range ops {
		batch[i] = gitops.BatchOp{TenantID: op.TenantID, Merge: func(existing []byte) (string, error) {
			absent[i] = op.skipAbsent && tenantAbsentIn(existing, op.TenantID)
			if absent[i] {
				return "", gitops.ErrMergeNoOp
			}
			return mergePatchYAML(existing, op.TenantID, op.Patch, op.Unset, names)
		}}
	}
	outcomes, err := w.WriteBatch(ctx, batch, email)

	results := make([]BatchResult, len(ops))
	var rejected *gitops.BatchRejectedError
	var committed *gitops.BatchCommittedError
	switch {
	case errors.As(err, &committed):
		// F3: in the repository, whole — reported as written, with the error.
		slog.Error("batch committed but the write did not finish cleanly", "commit", committed.Commit, "error", err)
	case errors.As(err, &rejected):
		for i, op := range ops {
			o := rejected.Outcomes[i]
			switch {
			case o.Err != nil:
				results[i] = batchErrorResult(op.TenantID, o.Err, treeVisible)
			case absent[i]:
				results[i] = BatchResult{TenantID: op.TenantID, Status: "skipped", Message: msgTenantAbsent}
			default:
				results[i] = BatchResult{TenantID: op.TenantID, Status: resultNotApplied, Message: msgNotApplied}
			}
		}
		return directBatchRun{Results: results, Rejected: true}
	case err != nil:
		// #2903: the generator refused the batch as a whole — each op's
		// result names the findings about its own tenant.
		if _, ok := generatorOpResult(ops[0].TenantID, err); ok {
			for i, op := range ops {
				results[i], _ = generatorOpResult(op.TenantID, err)
			}
			return directBatchRun{Results: results, Err: err}
		}
		// The batch failed as a whole: every op gets the same verdict
		// (rendered and logged once).
		first := batchErrorResult(ops[0].TenantID, err, treeVisible)
		for i, op := range ops {
			results[i] = first
			results[i].TenantID = op.TenantID
		}
		return directBatchRun{Results: results, Err: err}
	}
	for i, op := range ops {
		if absent[i] {
			results[i] = BatchResult{TenantID: op.TenantID, Status: "skipped", Message: msgTenantAbsent}
			continue
		}
		// #1231 1b: a successful op surfaces its non-blocking deprecation
		// notices per tenant, so the operator sees exactly which member of the
		// batch still carries an old key spelling.
		results[i] = BatchResult{TenantID: op.TenantID, Status: "ok", Warnings: outcomes[i].Notices}
	}
	if committed != nil {
		return directBatchRun{Results: results, Err: err, Committed: committed.Commit}
	}
	return directBatchRun{Results: results}
}

// applyPatch writes ONE op as a one-op batch and returns its result — the
// direct write the batch endpoints run, without their permission gates (which sit one layer up, in executeBatchOps). A failure of the batch
// as a whole is that op's "error" result.
func applyPatch(ctx context.Context, w *gitops.Writer, _ string, op BatchOperation, authorEmail string) BatchResult {
	return writeBatchOps(ctx, w, []BatchOperation{op}, authorEmail, false).Results[0]
}

// batchErrorResult renders a write error as the "error" result of op
// tenantID: a refusal of that op (gitops.BatchRejectedError's outcomes), or a
// failure of the whole batch, which every op of it then reports. treeVisible
// (treePathsVisible) decides whether a TENANT_TREE_UNREADABLE result names
// the entries; the log always does.
func batchErrorResult(tenantID string, err error, treeVisible bool) BatchResult {
	// #2903: the generator refused the batch as a whole.
	if res, ok := generatorOpResult(tenantID, err); ok {
		return res
	}
	msg := err.Error()
	code := ""
	var notLoadable *tenantFileNotLoadableError
	var treeUnreadable *gitops.TreeUnreadableError
	var targetUnreadable *gitops.TargetUnreadableError
	var gitManaged *gitops.GitManagedError
	switch {
	case errors.As(err, &gitManaged):
		// #3064: the op's tenant is git-managed; the text names it and a
		// fixed reason code, nothing else of the file. Nothing written.
		msg, code = gitManaged.Error(), CodeTenantGitManaged
	case errors.As(err, &treeUnreadable):
		// #2939: the tree after the batch holds entries the route generator
		// cannot read, and no op of the batch replaces them — unresolvable links
		// (per op, TENANT_TREE_UNREADABLE) or a store fault (the whole
		// batch, TENANT_STORE_FAULT; every op reports it).
		logTreeUnreadable("batch refused: conf.d holds unreadable entries", tenantID, err)
		_, treeCode, _ := treeRefusal(treeUnreadable)
		msg, code = treeUnreadableText(treeUnreadable, treeVisible), treeCode
	case errors.As(err, &targetUnreadable):
		// #2939 D3c: the op's own tenant file has content nobody can read;
		// it is not replaced (as PUT's 409).
		slog.Warn("batch op refused: the tenant file cannot be read", "tenant", tenantID, "error", err)
		msg, code = targetUnreadableText(targetUnreadable), CodeTenantConfigNotLoadable
	case errors.Is(err, confd.ErrNotRegularFile):
		// #2939: the op's own tenant file is a FIFO, socket or device —
		// tenant-api neither reads nor replaces it (as PUT's 409). The text
		// names only the file's base name.
		code = CodeTenantConfigNotLoadable
	case errors.Is(err, gitops.ErrConflict):
		// #2900: a batch publishes with a compare-and-swap, so a conflict
		// means the serving branch moved and nothing was written. (The
		// committed case, gitops.BatchCommittedError, never reaches here.)
		msg = "conflict: retry after refresh"
	case errors.Is(err, gitops.ErrWriteOverloaded):
		msg = "write plane busy: retry shortly"
	case isWriteOutcomeUnknown(err):
		// #3173: the ref update failed and the branch could not be read
		// back — the batch may be on it. Never "nothing written".
		slog.Error("batch write outcome unknown; detail withheld from the client",
			"tenant", tenantID, "error", err)
		msg = msgWriteOutcomeUnknown
	case isWriteCancelled(err):
		msg = msgWriteCancelled
	case errors.Is(err, gitops.ErrTreeNotOnBase):
		// #1723: fixed text — err names the stranded PR branch, and with
		// it another tenant's id, which only the server log may see.
		slog.Warn("batch refused: worktree not on the base branch",
			"tenant", tenantID, "error", err)
		msg, code = "config worktree was not on the base branch; nothing written: retry shortly", CodeTreeNotOnBase
	case errors.Is(err, gitops.ErrTenantDeclaredElsewhere):
		// #2078: fixed text — err names the other file, which only the
		// server log may see.
		slog.Warn("batch op refused: tenant declared by another conf.d file",
			"tenant", tenantID, "error", err)
		msg, code = msgTenantDeclaredElsewhere, CodeTenantDeclaredElsewhere
	case errors.Is(err, gitops.ErrTenantTreeScan):
		slog.Error("batch op refused: conf.d scan failed", "tenant", tenantID, "error", err)
		msg, code = msgTenantTreeScan, CodeInternal
	case errors.Is(err, gitops.ErrTenantFileDirty):
		// #1535 item 6: fixed text — err names the path.
		slog.Warn("batch op refused: tenant file differs from HEAD", "tenant", tenantID, "error", err)
		msg, code = msgTenantFileDirty, CodeTenantFileDirty
	case errors.As(err, &notLoadable):
		// #2373 review F1: nothing written; the tenant file must be
		// repaired first.
		msg, code = notLoadable.Error(), CodeTenantConfigNotLoadable
	case !writeErrorIsForClient(err):
		// #1700: a file / git failure — its text carries server paths.
		// The batch may run async, after the request is gone, so the log
		// line is keyed by tenant rather than request_id.
		slog.Error("batch write failed; detail withheld from the client",
			"tenant", tenantID, "error", err)
		// #1535: the server's failure, so its code says so — as the sync
		// answer's 500 INTERNAL_ERROR does (writeBatchFailure).
		msg, code = msgBatchOpFailed, CodeInternal
	}
	return BatchResult{TenantID: tenantID, Status: "error", Message: msg, Code: code}
}

// freshBaseOpResult renders the refusal of a PR-mode op judged on the fresh
// base (#2900 F4) as its per-op result. The messages name no op index: in a
// group batch it would count the hidden members before this one (#2857).
func freshBaseOpResult(tenantID string, err error, treeVisible bool) BatchResult {
	var (
		scope *freshBaseScopeError
		orgs  *freshBaseOrgsError
	)
	switch {
	case errors.As(err, &scope) && scope.PreState:
		return BatchResult{TenantID: tenantID, Status: "error", Code: CodeForbidden,
			Message: "insufficient permissions for tenant " + tenantID + " on the latest base branch (permission and organization-scope checks, ADR-027)"}
	case errors.As(err, &scope):
		return BatchResult{TenantID: tenantID, Status: "error", Code: CodeForbidden,
			Message: "insufficient permissions for the tenant metadata this op proposes on the latest base branch: its environment/domain would place tenant " +
				tenantID + " outside your scope (#1597)"}
	case errors.As(err, &orgs):
		slog.Error("PR batch op refused: _tenant_orgs.yaml on the fresh base cannot be read", "tenant", tenantID, "error", orgs.Err)
		return BatchResult{TenantID: tenantID, Status: "error", Code: CodeConfigDecode,
			Message: "the tenant organization file (_tenant_orgs.yaml) on the latest base branch cannot be read, so this op's organization scope cannot be judged; repair it on the base branch"}
	}
	return batchErrorResult(tenantID, err, treeVisible)
}

// writeBatchFailure answers a sync direct-mode batch that failed as a whole
// (directBatchRun.Err) with the status a single write gets for the same
// cause: 503 busy / tree on a PR branch, 409 conflict, else 500 with the
// detail withheld (#1700).
func writeBatchFailure(rw http.ResponseWriter, r *http.Request, d *Deps, err error) {
	if writeWriteFlowError(rw, r, d, err) {
		return
	}
	var committed *gitops.BatchCommittedError
	switch {
	case errors.As(err, &committed):
		// F3: 409 like a conflict (refresh first), but saying what is true.
		WriteErrorEnvelope(rw, r, http.StatusConflict, ErrorResponse{
			Error: msgBatchCommittedIncomplete,
			Code:  CodeConflict,
			Extra: map[string]any{"committed": true, "commit": committed.Commit},
		})
	case errors.Is(err, gitops.ErrTreeNotOnBase):
		WriteTreeNotOnBase(rw, r, err)
	case errors.Is(err, gitops.ErrConflict):
		WriteErrorEnvelope(rw, r, http.StatusConflict, ErrorResponse{
			Error: msgBatchConflict,
			Code:  CodeConflict,
			Extra: map[string]any{"committed": false},
		})
	case writeWriteCancelled(rw, r, err):
	default:
		WriteJSONError(rw, r, http.StatusInternalServerError, err.Error())
	}
}

// msgBatchConflict / msgBatchCommittedIncomplete are the two 409s of a
// direct-mode batch (#2900 F3): one says nothing was written, the other that
// everything was — never the same words for the two.
const (
	msgBatchConflict            = "conflict: the repository changed outside tenant-api while the batch ran; nothing was written — refresh and retry"
	msgBatchCommittedIncomplete = "the batch WAS committed, but the write did not finish cleanly on the server; " +
		"refresh before changing anything (do not resend the batch blindly)"
)

// errBatchRejectedTask is the Task.Error of an async batch that was rejected
// (#2900); its results say which ops were refused.
var errBatchRejectedTask = errors.New("batch rejected: an operation was refused, so nothing in the batch was written; see results")

// batchTaskFunc is the async form of a direct-mode batch: executeBatchOps at
// execution time. A rejected batch, or one that failed as a whole, ends the
// task as failed — with its per-op results, so a poller sees which op was
// refused.
func batchTaskFunc(d *Deps, ops []BatchOperation, email string, p *rbac.VerifiedPrincipal) async.TaskFunc {
	return func(ctx context.Context) ([]async.TaskResult, error) {
		// #2903 (ADR-036 §3): the task's own deadline, from when it starts —
		// the generator is given what is left of it, as a request's.
		ctx, cancel := context.WithTimeout(ctx, d.WriteTimeout())
		defer cancel()
		run := d.executeBatch(ctx, nil, ops, email, p, batchResultsStored)
		return toTaskResults(run.Results), batchTaskError(run)
	}
}

// batchTaskError is the Task.Error of an async batch run: nil when it was
// written cleanly. Whether it says nothing was written is decided by what
// run.Err is, not added on top of every failure: a batch that IS committed
// never says it (#2900 F3), and neither does one whose ref update failed
// with the branch unreadable afterwards (#3173) — that may be on the branch
// and is msgWriteOutcomeUnknown. One its context stopped, or a git command
// past its own deadline before anything was committed, is msgWriteCancelled
// (#1535), as the sync 503 and the per-op results say.
// Order matters: a run the writer saw committed is the committed text even
// when its cause is the context's.
func batchTaskError(run directBatchRun) error {
	switch {
	case run.Rejected:
		return errBatchRejectedTask
	case run.Committed != "":
		return errors.New(msgBatchCommittedIncomplete + " (commit " + run.Committed + ")")
	case isWriteOutcomeUnknown(run.Err):
		return errors.New(msgWriteOutcomeUnknown)
	case isWriteCancelled(run.Err):
		return errors.New(msgWriteCancelled)
	case run.Err != nil:
		// The fixed per-op text, not err: err may carry server paths.
		return errors.New("batch failed, nothing was written: " + run.Results[0].Message)
	}
	return nil
}

// rejectedBatchResponse is the body of the 422 a rejected batch gets (#2900).
func rejectedBatchResponse(taskID string, results []BatchResult) BatchResponse {
	return BatchResponse{
		Status:  batchStatusRejected,
		TaskID:  taskID,
		Results: results,
		Summary: summarizeBatchResults(results),
		Message: msgBatchRejected,
	}
}

// msgBatchRejected is BatchResponse.Message of a rejected batch.
const msgBatchRejected = "Batch rejected: nothing was written. Fix or remove the operations with status error and resend the whole batch."

// msgTenantAbsent is the message of a group member skipped because it does
// not exist (#2882).
const msgTenantAbsent = "tenant does not exist"

// skippedOp ties an op taken into a PR-mode batch to its result, for the
// in-lock absence verdict (#2882).
type skippedOp struct {
	result int
	absent *bool
}

// tenantAbsentIn reports whether existing — the tenant's file as the writer
// read it — declares no `tenants.<tenantID>` section: the file is missing or
// blank, or carries no section for it (#2882). A file that cannot be read as
// a `tenants:` document is not called absent; mergePatchYAML refuses it.
func tenantAbsentIn(existing []byte, tenantID string) bool {
	if len(bytes.TrimSpace(existing)) == 0 {
		return true
	}
	var doc yaml.Node
	if err := yaml.Unmarshal(existing, &doc); err != nil || doc.Kind != yaml.DocumentNode || len(doc.Content) == 0 {
		return false
	}
	root := doc.Content[0]
	if root.Kind != yaml.MappingNode {
		return false
	}
	tenants := yamlMapValue(root, "tenants")
	if tenants == nil || tenants.Kind != yaml.MappingNode {
		return false
	}
	return yamlMapValue(tenants, tenantID) == nil
}

// mergePatchYAML sets each patch key on `tenants.<tenantID>` in the existing
// document, preserving every OTHER key AND all comments/blank lines via
// yaml.Node surgery (mirroring the custom-alerts write path). This is the fix
// for #1097: a partial batch patch must not clobber keys it did not name.
//
// existing empty (nil / whitespace) → a brand-new tenant: build the minimal doc
// (buildPatchYAML). A non-empty but unparseable / structurally-wrong existing
// file returns an error — the caller must NOT fall back to an overwrite, which
// would reintroduce the very data loss this prevents.
// ⛔ COST NOTE (#1722): this is quadratic in len(patch) — yamlMapValue and
// yamlSetMapValue each scan the mapping's Content once per key — and it runs
// inside the single-writer token, BEFORE the merged document reaches
// TA_MAX_TENANT_DOC_BYTES. The byte caps therefore do not cover the most
// expensive step on this path; BatchOperation.Patch's `max` is what bounds it.
//
// ⚠️ The rationale lives HERE and not on the struct field because swag turns a
// field's doc comment into the published OpenAPI description — internal cost
// notes do not belong in the API contract.
//
// unset (B2, #2341) names keys to REMOVE from `tenants.<tenantID>` after the
// patch is set; a key the block does not carry is a no-op. Unlike the patch,
// unset MAY remove a structured (mapping / sequence) value: removing a
// `_routing` mapping is exactly how a batch resets the tenant to
// `_routing_defaults` + its profile, and nothing nested is clobbered by a
// scalar — the key goes away whole, as a PUT without it would leave it. A
// comment attached to the removed key goes with it. Which keys may be
// removed is decided before this runs (validateBatchEdit); a brand-new
// tenant (existing empty) has nothing to remove, so buildPatchYAML ignores
// unset.
//
// An edit that changes nothing returns gitops.ErrMergeNoOp, which the writer
// treats as success without writing (as RFC 7396 treats removing an absent
// member): an empty patch whose unset removes nothing — the tenant has no
// file, no section in its file, or none of the keys. Without it, an
// unset-only op would CREATE the tenant (`<id>: {}`) or re-encode an
// unchanged file into a reformatting commit. An empty patch with an empty
// unset is the same no-op (the pre-B2 tenant batch accepted it with 200).
// names is the caller's tenantNamesFor (verifyPartialWrite).
func mergePatchYAML(existing []byte, tenantID string, patch map[string]string, unset []string, names gitops.TenantNames) (string, error) {
	if len(bytes.TrimSpace(existing)) == 0 {
		if len(patch) == 0 {
			return "", gitops.ErrMergeNoOp
		}
		return buildPatchYAML(tenantID, patch), nil
	}
	// #2373 review F1: refuse a file threshold-exporter rejects. The patch
	// would land in a file no plane serves, and "succeeded" would read as
	// applied. This runs inside the merge closure, so it judges the base the
	// writer actually merges into (under the lock, on the fresh base).
	if err := checkPartialWriteBase(tenantID, existing); err != nil {
		return "", err
	}

	var doc yaml.Node
	if err := yaml.Unmarshal(existing, &doc); err != nil {
		return "", fmt.Errorf("parse current tenant yaml: %w", err)
	}
	if doc.Kind != yaml.DocumentNode || len(doc.Content) == 0 {
		return "", fmt.Errorf("current tenant yaml is not a document")
	}
	root := doc.Content[0]
	if root.Kind != yaml.MappingNode {
		return "", fmt.Errorf("current tenant yaml root is not a mapping")
	}
	tenantsVal := yamlMapValue(root, "tenants")
	if tenantsVal == nil || tenantsVal.Kind != yaml.MappingNode {
		return "", fmt.Errorf("current tenant yaml has no `tenants:` mapping")
	}
	tenantVal := yamlMapValue(tenantsVal, tenantID)
	if len(patch) == 0 && (tenantVal == nil || (tenantVal.Kind == yaml.MappingNode && !carriesAny(tenantVal, unset))) {
		// Nothing to set and nothing there to remove. A section that is not
		// a mapping still gets the structural error below.
		return "", gitops.ErrMergeNoOp
	}
	if tenantVal == nil {
		// File exists (perhaps other content) but not this tenant's section: add it.
		tenantVal = &yaml.Node{Kind: yaml.MappingNode, Tag: "!!map"}
		yamlSetMapValue(tenantsVal, tenantID, tenantVal)
	} else if tenantVal.Kind != yaml.MappingNode {
		return "", fmt.Errorf("current tenant yaml `tenants.%s` is not a mapping", tenantID)
	}

	// Sort so newly-ADDED keys land deterministically (existing keys keep their
	// authored position — setMapValue replaces in place).
	keys := make([]string, 0, len(patch))
	for k := range patch {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		// Refuse to clobber a structured (mapping/sequence) key like `_metadata`
		// or `_custom_alerts` with a flat scalar patch value — that would silently
		// destroy nested data, the exact loss this path exists to prevent. A flat
		// batch patch only ever sets scalar keys. Classified as a CLIENT
		// validation error (→ 400 / per-tenant "error"), not server-state
		// corruption, so it maps like every other bad-patch rejection. (Before
		// this, `_custom_alerts` was caught by the downstream validator but
		// `_metadata` was silently overwritten — an inconsistency this closes.)
		if cur := yamlMapValue(tenantVal, k); cur != nil && (cur.Kind == yaml.MappingNode || cur.Kind == yaml.SequenceNode) {
			return "", fmt.Errorf("%w: batch patch cannot overwrite structured key %q with a scalar value", gitops.ErrValidation, k)
		}
		var vn yaml.Node
		if err := vn.Encode(patch[k]); err != nil {
			return "", fmt.Errorf("encode patch value for %q: %w", k, err)
		}
		// #2903: a patch value is a string, written double-quoted whatever it
		// holds, so no reader takes it for a timestamp, a number or a
		// boolean (`2001-12-14t21:59:43` plain is a datetime to PyYAML).
		vn.Style = yaml.DoubleQuotedStyle
		yamlSetMapValue(tenantVal, k, &vn)
	}
	for _, k := range unset {
		yamlDeleteMapValue(tenantVal, k)
	}

	customalerts.DropImplicitTags(&doc)
	var buf bytes.Buffer
	enc := yaml.NewEncoder(&buf)
	// SetIndent(2) matches the conf.d 2-space convention, under which a file
	// round-trips unchanged (see customalerts.MergeCustomAlerts). A file authored
	// with a different indent reflows to 2-space — comments still survive.
	enc.SetIndent(2)
	if err := enc.Encode(&doc); err != nil {
		return "", fmt.Errorf("re-encode tenant yaml: %w", err)
	}
	_ = enc.Close()
	// #2903: the re-encode must change nothing the edit does not.
	edits := make([]partialEdit, 0, len(keys)+len(unset))
	for _, k := range keys {
		edits = append(edits, partialEdit{key: k, set: true, checkValue: true, value: patch[k]})
	}
	for _, k := range unset {
		edits = append(edits, partialEdit{key: k})
	}
	if err := verifyPartialWrite(existing, buf.Bytes(), tenantID, edits, names); err != nil {
		return "", err
	}
	return buf.String(), nil
}

// buildPatchYAML constructs a minimal, full-document YAML for a tenant patch —
// the new-tenant fallback for mergePatchYAML (no existing file to merge into).
// Uses the yaml encoder for safe quoting of values with special characters.
func buildPatchYAML(tenantID string, patch map[string]string) string {
	doc := map[string]interface{}{
		"tenants": map[string]interface{}{
			tenantID: patch,
		},
	}
	var buf bytes.Buffer
	enc := yaml.NewEncoder(&buf)
	enc.SetIndent(2) // conf.d 2-space convention, consistent with mergePatchYAML
	if err := enc.Encode(doc); err != nil {
		// Unreachable for map[string]string, but fallback gracefully.
		fallback := fmt.Sprintf("tenants:\n  %s:\n", tenantID)
		for k, v := range patch {
			fallback += fmt.Sprintf("    %s: %q\n", k, v)
		}
		return fallback
	}
	_ = enc.Close()
	return buf.String()
}

// yamlMapValue returns the value node for key in a mapping node, or nil.
func yamlMapValue(m *yaml.Node, key string) *yaml.Node {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			return m.Content[i+1]
		}
	}
	return nil
}

// yamlSetMapValue replaces the value node for key (preserving the key node and
// its comments / position), or appends a new key/value pair if absent.
func yamlSetMapValue(m *yaml.Node, key string, val *yaml.Node) {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			m.Content[i+1] = val
			return
		}
	}
	m.Content = append(m.Content,
		&yaml.Node{Kind: yaml.ScalarNode, Tag: "!!str", Value: key}, val)
}

// carriesAny reports whether mapping m carries at least one of keys.
func carriesAny(m *yaml.Node, keys []string) bool {
	for _, k := range keys {
		if yamlMapValue(m, k) != nil {
			return true
		}
	}
	return false
}

// yamlDeleteMapValue removes key and its value from a mapping node; a key the
// mapping does not carry is a no-op.
func yamlDeleteMapValue(m *yaml.Node, key string) {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if m.Content[i].Value == key {
			m.Content = append(m.Content[:i], m.Content[i+2:]...)
			return
		}
	}
}
