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
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/policy"
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
	// keys to remove from the tenant's config block; at most 1000 entries. Only "_routing" is
	// accepted: removing it resets the tenant to `_routing_defaults` and its routing profile,
	// which turns routing back on for a tenant a disabling `_routing` turned off (the change is
	// judged by domain policy like a `_routing` patch). A key the tenant does not carry, or a
	// tenant that does not exist, makes it a no-op: with no patch nothing is written and no
	// tenant is created. A key must not appear twice, nor in both patch and unset.
	Unset []string `json:"unset,omitempty" validate:"max=1000"`
}

// BatchRequest is the body for POST /api/v1/tenants/batch.
type BatchRequest struct {
	Operations []BatchOperation `json:"operations" validate:"required,min=1,max=1000,dive"`
}

// BatchResult is the per-tenant result in a batch response.
type BatchResult struct {
	TenantID string `json:"tenant_id"`
	Status   string `json:"status"` // "ok" | "error"
	Message  string `json:"message,omitempty"`
	// Code is a machine-readable error code for a FAILED op, set only for the
	// failure classes a client is expected to branch on: TENANT_DECLARED_ELSEWHERE
	// (the per-op form of the 409 that PUT and the PR-mode batch return),
	// TENANT_CONFIG_NOT_LOADABLE (the tenant file cannot be loaded as a tenant
	// config, #2373; nothing written), INTERNAL_ERROR (the conf.d walk behind that check could not run; nothing
	// written) and POLICY_UNAVAILABLE (direct mode: the op is one the domain policy judges and a policy file is
	// present but unusable with no last good version; nothing written). Empty for every other failure and for a
	// successful op.
	Code string `json:"code,omitempty"`
	// Warnings carries non-blocking advisories for an op that SUCCEEDED
	// (#1231 deprecated-key alias notices from the direct WriteMerged path,
	// and the #2325 require_critical_escalation advisories).
	// Error results never carry warnings — Message owns the failure text.
	Warnings []string `json:"warnings,omitempty"`
}

// BatchResponse is the full response for POST /api/v1/tenants/batch.
type BatchResponse struct {
	Status   string        `json:"status"`              // "completed" | "pending_review" (PR mode)
	TaskID   string        `json:"task_id,omitempty"`   // async task ID
	PRURL    string        `json:"pr_url,omitempty"`    // v2.6.0: PR/MR URL in PR mode
	PRNumber int           `json:"pr_number,omitempty"` // v2.6.0: PR/MR number in PR mode
	Results  []BatchResult `json:"results"`
	Summary  string        `json:"summary"`           // e.g., "5 succeeded, 1 failed"
	Message  string        `json:"message,omitempty"` // v2.6.0: human-readable message
	// Warnings is the batch-level advisory list for PR mode (#1231): the
	// per-op notices aggregated by WritePRBatch. Batch-level (not per-result)
	// because PR-mode Results are frozen at "included" BEFORE the write runs;
	// each notice self-identifies its tenant (`tenant=<id>` in the wording).
	Warnings []string `json:"warnings,omitempty"`
}

// BatchTenants handles POST /api/v1/tenants/batch
//
// Executes a list of patch operations synchronously (default) or asynchronously (if ?async=true).
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
// @Description An operation may also remove keys with unset (only "_routing"): unset ["_routing"] turns routing back on for a tenant a disabling _routing turned off, judged by domain policy.
// @Description Direct mode: an operation whose tenant config file cannot be loaded as a tenant config (config_error malformed_yaml | invalid_config)
// @Description is not applied: its result carries status error and code TENANT_CONFIG_NOT_LOADABLE; repair the tenant file itself first.
// @Tags        tenants
// @Accept      json
// @Produce     json
// @Param       body body     BatchRequest true "Batch operations"
// @Param       async query   string       false "Enable async mode (true/false)"
// @Success     200  {object} BatchResponse
// @Success     202  {object} map[string]interface{}
// @Failure     400  {object} ErrorResponse
// @Failure     403  {object} ErrorResponse "PR write-back mode: the forge token lacks write scope to open the PR/MR, or an operation breaks the domain policy on the latest base branch, which this server's local copy lags, or the base's _domain_policy.yaml / .yml cannot be loaded (code POLICY_VIOLATION, with tenant_id and operation); nothing written. Direct mode and violations the local copy shows are reported per operation in results instead."
// @Failure     409  {object} ErrorResponse "PR write-back mode: a tenant in the batch is already declared by another conf.d file (code TENANT_DECLARED_ELSEWHERE), or its config file cannot be loaded as a tenant config (code TENANT_CONFIG_NOT_LOADABLE, with tenant_id and config_error; repair the tenant file itself first); nothing written. Direct mode reports these per op in results[].code instead."
// @Failure     413  {object} ErrorResponse
// @Failure     500  {object} ErrorResponse
// @Failure     503  {object} ErrorResponse "Service unavailable: the write plane is busy (WRITE_OVERLOADED) or the forge is degraded (FORGE_UNAVAILABLE); or, in direct mode, an operation writes _routing_receiver_type or touches _routing / _routing_profile while a _domain_policy.yaml / .yml is present but cannot be read or parsed and has no last good version (code POLICY_UNAVAILABLE, Retry-After; nothing written). An async batch is judged again per operation when it runs (results[].code POLICY_UNAVAILABLE)."
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

		taskID := fmt.Sprintf("batch-%s-%04d",
			time.Now().UTC().Format("20060102"), len(req.Operations))

		// v2.6.0: PR-based write-back mode for batch operations (ADR-011)
		// All operations are consolidated into a single PR/MR.
		// Supports both GitHub PRs and GitLab MRs via platform interfaces.
		if d.prWritePath() {
			batchTenantsPRMode(d, rw, r, req, email, p)
			return
		}

		// Hub #2486 Q7-2: a direct-mode batch with an op the domain policy
		// judges is refused whole (503) while the policy is unavailable; an
		// async one is judged again per op when it runs (executeBatchOps).
		if err := batchPolicyUnavailable(d.Policy, req.Operations); err != nil {
			writePolicyUnavailable(rw, r, err)
			return
		}

		// v2.6.0: Async mode — submit to goroutine pool and return immediately
		if r.URL.Query().Get("async") == "true" && d.Tasks != nil {
			task := d.Tasks.Submit(taskID, func(ctx context.Context) ([]async.TaskResult, error) {
				results := executeBatchOps(ctx, d.Writer, d.ConfigDir, req.Operations, email, p, d.RBAC, d.TenantOrg, d.Policy)
				return toTaskResults(results), nil
			})

			write202Pending(rw, task)
			return
		}

		// Synchronous mode (default, backward compatible)
		results := executeBatchOps(r.Context(), d.Writer, d.ConfigDir, req.Operations, email, p, d.RBAC, d.TenantOrg, d.Policy)

		writeJSON(rw, http.StatusOK, BatchResponse{
			Status:  "completed",
			TaskID:  taskID,
			Results: results,
			Summary: summarizeBatchResults(results),
		})
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
	if resp, ok := runBatchPR(d, rw, r, req.Operations, email, p, tenantBatchPRMeta); ok {
		writeJSON(rw, http.StatusOK, resp)
	}
}

// runBatchPR is the PR write-back core shared by POST /tenants/batch and
// POST /groups/{id}/batch (#2339): per-op RBAC and domain policy, one
// WritePRBatch, one PR/MR. On an error it writes the error response itself
// and returns ok=false; otherwise it returns the 200 body for the caller to
// write (the group endpoint wraps it with its group id). The caller must have
// verified d.prWritePath().
func runBatchPR(d *Deps, rw http.ResponseWriter, r *http.Request, ops []BatchOperation, email string, p *rbac.VerifiedPrincipal, meta batchPRMeta) (BatchResponse, bool) {
	// Pre-validate all ops (RBAC + policy) before creating any branch
	var batchOps []gitops.PRBatchOp
	var batchResults []BatchResult
	// included[t]: t's ops already taken into this PR, in order — each with
	// its patch AND its unset, since removing `_routing` changes the routing
	// as much as setting it. WritePRBatch merges them all onto one base in
	// that order, so a routing check must see them stacked
	// (batchRoutingViolations). A refused op is never appended: nothing of it
	// lands.
	included := map[string][]BatchOperation{}
	// advisoriesByTenant: the non-blocking #2325 domain-policy notes of each
	// tenant's LAST routing op taken into the PR. An earlier op of the same
	// tenant is judged on an intermediate routing the later ops are stacked
	// over, so its notes are replaced, not kept (#2440 review). An op that
	// does not touch routing is not judged (batchRoutingViolations) and leaves
	// the routing as is, so it replaces nothing and registers nothing. A key
	// in the map means the tenant is registered (even with nil advisories, so
	// a later op can clear an earlier one's). advisoryTenants holds the order
	// of each tenant's FIRST routing op taken into the PR, each tenant once;
	// advisories below flattens them.
	advisoriesByTenant := map[string][]string{}
	var advisoryTenants []string
	for i, op := range ops {
		if err := ValidateWritableTenantID(op.TenantID); err != nil {
			batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "error", Message: err.Error()})
			continue
		}
		if !OrgAllowed(d.RBAC, d.TenantOrg, p, op.TenantID, rbac.PermWrite, WriteScopeMeta(d.ConfigDir)) {
			batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "error", Message: "insufficient permissions"})
			continue
		}
		if d.Policy != nil {
			// #2280: an op that sets `_routing_profile` / `_routing`, or
			// unsets `_routing`, is judged on the routing it produces; see
			// batchRoutingViolations. Checked here, not inside the merge
			// closure, so one refused op is left out instead of aborting the
			// whole PR.
			violations := d.Policy.CheckWrite(op.TenantID, op.Patch)
			routingViolations, adv, judged := batchRoutingViolations(d.ConfigDir, d.Policy, included[op.TenantID], op)
			violations = append(violations, routingViolations...)
			if len(violations) > 0 {
				msgs := make([]string, len(violations))
				for i, v := range violations {
					msgs[i] = v.Message
				}
				batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "error", Message: "policy violation: " + strings.Join(msgs, "; ")})
				continue
			}
			// #2325: batch-level, like the notices (each names its tenant).
			if judged {
				if _, seen := advisoriesByTenant[op.TenantID]; !seen {
					advisoryTenants = append(advisoryTenants, op.TenantID)
				}
				advisoriesByTenant[op.TenantID] = adv
			}
		}
		// #1097: carry a merge closure, not pre-built content, so the
		// authoritative partial merge runs under the writer lock against
		// the fresh base (preserving untouched keys). op is per-iteration
		// (Go 1.22+), so the closure binds this op's TenantID/Patch/Unset.
		op := op
		batchOps = append(batchOps, gitops.PRBatchOp{
			TenantID: op.TenantID,
			Merge: func(existing []byte) (string, error) {
				merged, err := mergePatchYAML(existing, op.TenantID, op.Patch, op.Unset)
				if err != nil {
					return "", err
				}
				// B2 F1: the check above read the pod's local tree; this one
				// reads the fresh base the branch is cut from. A refusal here
				// aborts the whole batch (WritePRBatch), nothing written.
				if d.Policy != nil {
					v, loadErr := freshBasePolicyCheck(d.ConfigDir, op, existing, merged)
					if loadErr != nil || len(v) > 0 {
						return "", &freshBasePolicyError{TenantID: op.TenantID, Op: i, Violations: v, LoadErr: loadErr}
					}
				}
				return merged, nil
			},
		})
		batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "included"})
		included[op.TenantID] = append(included[op.TenantID], op)
	}

	var advisories []string
	for _, t := range advisoryTenants {
		advisories = append(advisories, advisoriesByTenant[t]...)
	}

	if len(batchOps) == 0 {
		return BatchResponse{
			Status:  "completed",
			Results: batchResults,
			Summary: prBatchSummary("completed", batchResults),
			Message: "No valid operations to create PR/MR.",
		}, true
	}

	result, err := d.Writer.WritePRBatch(r.Context(), batchOps, email)
	if err != nil {
		// TRK-320 ErrWriteOverloaded / TRK-318 ErrForgeDegraded → canonical
		// retry-hinting 503s (shared with the single-write path).
		if writeWriteFlowError(rw, r, err) {
			return BatchResponse{}, false
		}
		// #2373 review F1: an op's tenant file is one threshold-exporter
		// rejects; the whole PR is refused (nothing is written), like the
		// other per-tenant refusals WritePRBatch reports.
		var notLoadable *tenantFileNotLoadableError
		if errors.As(err, &notLoadable) {
			writeTenantFileNotLoadable(rw, r, notLoadable)
			return BatchResponse{}, false
		}
		// B2 F1: an op breaks the domain policy on the fresh base although
		// the pre-check (the pod's local tree) let it through. The whole PR
		// is refused, like the refusals above; same 403 POLICY_VIOLATION as
		// PUT (writePolicyViolation).
		var freshPolicy *freshBasePolicyError
		if errors.As(err, &freshPolicy) {
			writeFreshBasePolicyViolation(rw, r, freshPolicy)
			return BatchResponse{}, false
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
			return BatchResponse{
				Status:   "completed",
				Results:  batchResults,
				Summary:  prBatchSummary("completed", batchResults),
				Message:  "No changes to apply; no PR/MR created.",
				Warnings: append(warnings, advisories...),
			}, true
		}
		// #795 F1: a malformed op body is a CLIENT error → 400, not a 500.
		if errors.Is(err, gitops.ErrValidation) {
			WriteJSONError(rw, r, http.StatusBadRequest, err.Error())
			return BatchResponse{}, false
		}
		// Anything else is an unexpected git failure → generic 500.
		WriteJSONError(rw, r, http.StatusInternalServerError, "PR/MR batch write failed: "+err.Error())
		return BatchResponse{}, false
	}

	// PR-6/11: shared post-write flow via createPRAndRegister.
	// Per-tenant tracker entries get every field of the PR
	// response (Title / HeadRef / CreatedAt) preserved
	// consistently with the single-tenant path.
	prTitle := meta.title(len(batchOps))
	tenantList := make([]string, len(batchOps))
	for i, op := range batchOps {
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
		return BatchResponse{}, false
	}

	return BatchResponse{
		Status:   "pending_review",
		PRURL:    pr.WebURL,
		PRNumber: pr.Number,
		Results:  batchResults,
		Summary:  prBatchSummary("pending_review", batchResults),
		Message:  fmt.Sprintf("Batch PR/MR created with %d tenant changes.", len(batchOps)),
		Warnings: append(result.Notices, advisories...),
	}, true
}

// prBatchSummary renders runBatchPR's summary line from its status and the
// per-op results, in which every op taken into the PR is "included" and every
// other op is a failure. It reads only the results it is given, so the group
// endpoint can re-render it over the results the caller may see (#1530) and
// get the same wording as for a /tenants/batch response.
func prBatchSummary(status string, results []BatchResult) string {
	included := 0
	for _, res := range results {
		if res.Status == "included" {
			included++
		}
	}
	failed := len(results) - included
	switch {
	case included == 0:
		return fmt.Sprintf("%d failed", failed)
	case status == "pending_review":
		return fmt.Sprintf("%d included in PR/MR, %d failed", included, failed)
	default:
		// "completed" with ops taken in: WritePRBatch found nothing to change.
		return fmt.Sprintf("%d unchanged", included)
	}
}

// executeBatchOps runs batch operations synchronously and returns results.
// This function is shared between sync and async paths to ensure consistency.
//
// The per-op permission check is org-scope-aware (ADR-027 / LD-6 P4b) and the
// tenant's org list is resolved INSIDE this loop, at execution time — not at
// submit time. The async path runs this closure after the HTTP request has
// returned, so a submit-time snapshot could authorize against orgs that a
// _tenant_orgs.yaml hot-reload has since changed (stale-orgs hazard).
func executeBatchOps(ctx context.Context, w *gitops.Writer, configDir string, ops []BatchOperation, email string, p *rbac.VerifiedPrincipal, rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, policyMgr *policy.Manager) []BatchResult {
	results := make([]BatchResult, 0, len(ops))
	for _, op := range ops {
		if res, failed := gateBatchOp(op.TenantID, p, rbacMgr, tenantOrg, configDir); failed {
			results = append(results, res)
			continue
		}
		var advisories []string
		if policyMgr != nil {
			// Hub #2486 Q7-2: judged again here, at execution time — an async
			// batch runs after the request's own check, and the policy file
			// may have broken in between.
			if res, refused := batchOpPolicyUnavailable(policyMgr, op); refused {
				results = append(results, res)
				continue
			}
			violations := policyMgr.CheckWrite(op.TenantID, op.Patch)
			// nil prior: each op is written before the next one is judged,
			// and the next one reads the file back.
			routingViolations, adv, _ := batchRoutingViolations(configDir, policyMgr, nil, op)
			violations = append(violations, routingViolations...)
			if len(violations) > 0 {
				msgs := make([]string, len(violations))
				for i, v := range violations {
					msgs[i] = v.Message
				}
				results = append(results, BatchResult{TenantID: op.TenantID, Status: "error", Message: "domain policy violation: " + strings.Join(msgs, "; ")})
				continue
			}
			advisories = adv
		}
		result := applyPatch(ctx, w, configDir, op, email)
		if result.Status == "ok" {
			// #2325: non-blocking domain-policy notes ride on a successful op.
			result.Warnings = append(result.Warnings, advisories...)
		}
		results = append(results, result)
	}
	return results
}

// batchOpPolicyUnavailable is executeBatchOps' per-op availability gate (hub
// #2486 Q7-2): an op the domain policy judges (readsPolicy) is refused with
// POLICY_UNAVAILABLE while the policy is unavailable; refused reports it.
func batchOpPolicyUnavailable(mgr *policy.Manager, op BatchOperation) (res BatchResult, refused bool) {
	if mgr == nil || !readsPolicy(op) {
		return BatchResult{}, false
	}
	err := mgr.CheckAvailable("batch op on tenant " + op.TenantID)
	if err == nil {
		return BatchResult{}, false
	}
	slog.Warn("batch op refused: domain policy unavailable", "tenant", op.TenantID, "error", err)
	return BatchResult{TenantID: op.TenantID, Status: "error", Message: msgPolicyUnavailable,
		Code: CodePolicyUnavailable}, true
}

// applyPatch applies a single patch operation to a tenant config file.
//
// #1097: the patch is a PARTIAL update — it merges into the tenant's existing
// keys via WriteMerged (read-merge-write under the writer lock), so keys the
// patch does not name (other thresholds, `_metadata`, `_custom_alerts`) and the
// file's comments survive. A whole-document overwrite here would silently drop
// them.
func applyPatch(ctx context.Context, w *gitops.Writer, configDir string, op BatchOperation, authorEmail string) BatchResult {
	merge := func(existing []byte) (string, error) {
		return mergePatchYAML(existing, op.TenantID, op.Patch, op.Unset)
	}
	notices, err := w.WriteMerged(ctx, op.TenantID, authorEmail, merge)
	if err != nil {
		msg := err.Error()
		code := ""
		var notLoadable *tenantFileNotLoadableError
		switch {
		case errors.Is(err, gitops.ErrConflict):
			msg = "conflict: retry after refresh"
		case errors.Is(err, gitops.ErrWriteOverloaded):
			msg = "write plane busy: retry shortly"
		case errors.Is(err, gitops.ErrTreeNotOnBase):
			// #1723: fixed text — err names the stranded PR branch, and with
			// it another tenant's id, which only the server log may see.
			slog.Warn("batch op refused: worktree not on the base branch",
				"tenant", op.TenantID, "error", err)
			msg, code = "config worktree was not on the base branch; nothing written: retry shortly", CodeTreeNotOnBase
		case errors.Is(err, gitops.ErrTenantDeclaredElsewhere):
			// #2078: fixed text — err names the other file, which only the
			// server log may see.
			slog.Warn("batch op refused: tenant declared by another conf.d file",
				"tenant", op.TenantID, "error", err)
			msg, code = msgTenantDeclaredElsewhere, CodeTenantDeclaredElsewhere
		case errors.Is(err, gitops.ErrTenantTreeScan):
			slog.Error("batch op refused: conf.d scan failed", "tenant", op.TenantID, "error", err)
			msg, code = msgTenantTreeScan, CodeInternal
		case errors.As(err, &notLoadable):
			// #2373 review F1: nothing written; the tenant file must be
			// repaired first.
			msg, code = notLoadable.Error(), CodeTenantConfigNotLoadable
		}
		return BatchResult{TenantID: op.TenantID, Status: "error", Message: msg, Code: code}
	}
	// #1231 1b: a successful op surfaces its non-blocking deprecation notices
	// per tenant, so the operator sees exactly which member of the batch still
	// carries an old key spelling.
	return BatchResult{TenantID: op.TenantID, Status: "ok", Warnings: notices}
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
func mergePatchYAML(existing []byte, tenantID string, patch map[string]string, unset []string) (string, error) {
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
		if err := vn.Encode(patch[k]); err != nil { // string → correctly-quoted scalar (e.g. "50" stays a string)
			return "", fmt.Errorf("encode patch value for %q: %w", k, err)
		}
		yamlSetMapValue(tenantVal, k, &vn)
	}
	for _, k := range unset {
		yamlDeleteMapValue(tenantVal, k)
	}

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
