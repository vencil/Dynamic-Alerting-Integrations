package handler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
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
// validation lives in `body_validator.go::validatePatchMap`.
type BatchOperation struct {
	TenantID string            `json:"tenant_id" validate:"required,min=1,max=256"`
	Patch    map[string]string `json:"patch"` // key → value to set (e.g., "_silent_mode": "warning")
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
// @Tags        tenants
// @Accept      json
// @Produce     json
// @Param       body body     BatchRequest true "Batch operations"
// @Param       async query   string       false "Enable async mode (true/false)"
// @Success     200  {object} BatchResponse
// @Success     202  {object} map[string]interface{}
// @Failure     400  {object} ErrorResponse
// @Failure     500  {object} ErrorResponse
// @Failure     503  {object} ErrorResponse
// @Router      /api/v1/tenants/batch [post]
func BatchTenants(d *Deps) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		email := rbac.RequestEmail(r)
		// Capture the verified principal VALUE for the async path below —
		// same discipline as email: the closure must never reach back into
		// the request context (it outlives the HTTP request), so it captures
		// the immutable principal snapshot, not r / r.Context().
		p := rbac.RequestPrincipal(r)

		var req BatchRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
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
			fieldPrefix := fmt.Sprintf("operations[%d].patch", i)
			violations = append(violations, validatePatchMap(op.Patch, fieldPrefix)...)
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
		if d.WriteMode.IsPRMode() && d.PRClient != nil && d.PRTracker != nil {
			batchTenantsPRMode(d, rw, r, req, email, p)
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

// batchTenantsPRMode handles a batch request in PR write-back mode (ADR-011):
// all operations are consolidated into a single PR/MR (GitHub or GitLab via
// the platform interfaces). Split out of BatchTenants (Cycle 10 refactor) to
// keep the handler readable — behavior is unchanged. The caller must have
// verified IsPRMode && PRClient != nil && PRTracker != nil. Always writes a
// response.
func batchTenantsPRMode(d *Deps, rw http.ResponseWriter, r *http.Request, req BatchRequest, email string, p *rbac.VerifiedPrincipal) {
	// Pre-validate all ops (RBAC + policy) before creating any branch
	var batchOps []gitops.PRBatchOp
	var batchResults []BatchResult
	for _, op := range req.Operations {
		if err := ValidateTenantID(op.TenantID); err != nil {
			batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "error", Message: err.Error()})
			continue
		}
		if !OrgAllowed(d.RBAC, d.TenantOrg, p, op.TenantID, rbac.PermWrite) {
			batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "error", Message: "insufficient permissions"})
			continue
		}
		if d.Policy != nil {
			if violations := d.Policy.CheckWrite(op.TenantID, op.Patch); len(violations) > 0 {
				msgs := make([]string, len(violations))
				for i, v := range violations {
					msgs[i] = v.Message
				}
				batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "error", Message: "policy violation: " + strings.Join(msgs, "; ")})
				continue
			}
		}
		// #1097: carry a merge closure, not pre-built content, so the
		// authoritative partial merge runs under the writer lock against
		// the fresh base (preserving untouched keys). op is per-iteration
		// (Go 1.22+), so the closure binds this op's TenantID/Patch.
		op := op
		batchOps = append(batchOps, gitops.PRBatchOp{
			TenantID: op.TenantID,
			Merge: func(existing []byte) (string, error) {
				return mergePatchYAML(existing, op.TenantID, op.Patch)
			},
		})
		batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "included"})
	}

	if len(batchOps) == 0 {
		writeJSON(rw, http.StatusOK, BatchResponse{
			Status:  "completed",
			Results: batchResults,
			Summary: fmt.Sprintf("%d failed", len(batchResults)),
			Message: "No valid operations to create PR/MR.",
		})
		return
	}

	result, err := d.Writer.WritePRBatch(r.Context(), batchOps, email)
	if err != nil {
		// TRK-320 ErrWriteOverloaded / TRK-318 ErrForgeDegraded → canonical
		// retry-hinting 503s (shared with the single-write path).
		if writeWriteFlowError(rw, r, err) {
			return
		}
		// #1102: an all-no-op batch (idempotent patch / retry) produced no
		// commits — return a clean "no changes" success, never a forge error.
		if errors.Is(err, gitops.ErrNoChanges) {
			writeJSON(rw, http.StatusOK, BatchResponse{
				Status:  "completed",
				Results: batchResults,
				Summary: fmt.Sprintf("%d unchanged", len(batchOps)),
				Message: "No changes to apply; no PR/MR created.",
			})
			return
		}
		// #795 F1: a malformed op body is a CLIENT error → 400, not a 500.
		if errors.Is(err, gitops.ErrValidation) {
			WriteJSONError(rw, r, http.StatusBadRequest, err.Error())
			return
		}
		// Anything else is an unexpected git failure → generic 500.
		WriteJSONError(rw, r, http.StatusInternalServerError, "PR/MR batch write failed: "+err.Error())
		return
	}

	// PR-6/11: shared post-write flow via createPRAndRegister.
	// Per-tenant tracker entries get every field of the PR
	// response (Title / HeadRef / CreatedAt) preserved
	// consistently with the single-tenant path.
	prTitle := fmt.Sprintf("[tenant-api] Batch update %d tenants", len(batchOps))
	tenantList := make([]string, len(batchOps))
	for i, op := range batchOps {
		tenantList[i] = op.TenantID
	}
	prBody := fmt.Sprintf("**Operator:** %s\n**Source:** tenant-manager UI (batch)\n**Tenants:** %s",
		email, strings.Join(tenantList, ", "))
	pr, err := createPRAndRegister(d,
		prTitle, prBody, result.BranchName,
		[]string{"tenant-api", "auto-generated", "batch"},
		tenantList,
	)
	if err != nil {
		// Shared with the single-write path: forbidden → clean 403 (previously
		// the batch path was missing this and leaked a generic 503),
		// circuit-open → sanitized 503, else generic 503.
		writeForgeCreateError(rw, r, d.PRClient.ProviderName(), err)
		return
	}

	writeJSON(rw, http.StatusOK, BatchResponse{
		Status:   "pending_review",
		PRURL:    pr.WebURL,
		PRNumber: pr.Number,
		Results:  batchResults,
		Summary:  fmt.Sprintf("%d included in PR/MR, %d failed", len(batchOps), len(batchResults)-len(batchOps)),
		Message:  fmt.Sprintf("Batch PR/MR created with %d tenant changes.", len(batchOps)),
	})
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
		if res, failed := gateBatchOp(op.TenantID, p, rbacMgr, tenantOrg); failed {
			results = append(results, res)
			continue
		}
		if policyMgr != nil {
			if violations := policyMgr.CheckWrite(op.TenantID, op.Patch); len(violations) > 0 {
				msgs := make([]string, len(violations))
				for i, v := range violations {
					msgs[i] = v.Message
				}
				results = append(results, BatchResult{TenantID: op.TenantID, Status: "error", Message: "domain policy violation: " + strings.Join(msgs, "; ")})
				continue
			}
		}
		result := applyPatch(ctx, w, configDir, op, email)
		results = append(results, result)
	}
	return results
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
		return mergePatchYAML(existing, op.TenantID, op.Patch)
	}
	if err := w.WriteMerged(ctx, op.TenantID, authorEmail, merge); err != nil {
		msg := err.Error()
		if errors.Is(err, gitops.ErrConflict) {
			msg = "conflict: retry after refresh"
		} else if errors.Is(err, gitops.ErrWriteOverloaded) {
			msg = "write plane busy: retry shortly"
		}
		return BatchResult{TenantID: op.TenantID, Status: "error", Message: msg}
	}
	return BatchResult{TenantID: op.TenantID, Status: "ok"}
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
func mergePatchYAML(existing []byte, tenantID string, patch map[string]string) (string, error) {
	if len(bytes.TrimSpace(existing)) == 0 {
		return buildPatchYAML(tenantID, patch), nil
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
