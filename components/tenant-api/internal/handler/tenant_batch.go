package handler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
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
// @Failure     400  {object} map[string]string
// @Router      /api/v1/tenants/batch [post]
func (d *Deps) BatchTenants() http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		email := rbac.RequestEmail(r)
		groups := rbac.RequestGroups(r)

		var req BatchRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSONError(rw, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if len(req.Operations) == 0 {
			writeJSONError(rw, http.StatusBadRequest, "operations list is empty")
			return
		}

		// v2.8.0 issue #134 — body-content range validation (Phase B
		// Track C C4). Run BEFORE any RBAC / per-op work so a malformed
		// body fails fast with the full violation list (one round-trip
		// for the operator to fix everything, not retry-and-discover).
		violations := validateStructTags(&req)
		for i, op := range req.Operations {
			fieldPrefix := fmt.Sprintf("operations[%d].patch", i)
			violations = append(violations, validatePatchMap(op.Patch, fieldPrefix)...)
		}
		if len(violations) > 0 {
			writeValidationErrors(rw, violations)
			return
		}

		taskID := fmt.Sprintf("batch-%s-%04d",
			time.Now().UTC().Format("20060102"), len(req.Operations))

		// v2.6.0: PR-based write-back mode for batch operations (ADR-011)
		// All operations are consolidated into a single PR/MR.
		// Supports both GitHub PRs and GitLab MRs via platform interfaces.
		if d.WriteMode.IsPRMode() && d.PRClient != nil && d.PRTracker != nil {
			// Pre-validate all ops (RBAC + policy) before creating any branch
			var batchOps []gitops.PRBatchOp
			var batchResults []BatchResult
			for _, op := range req.Operations {
				if err := ValidateTenantID(op.TenantID); err != nil {
					batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "error", Message: err.Error()})
					continue
				}
				if !d.RBAC.HasPermission(groups, op.TenantID, rbac.PermWrite) {
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
				yamlContent := buildPatchYAML(op.TenantID, op.Patch)
				batchOps = append(batchOps, gitops.PRBatchOp{TenantID: op.TenantID, YAMLContent: yamlContent})
				batchResults = append(batchResults, BatchResult{TenantID: op.TenantID, Status: "included"})
			}

			if len(batchOps) == 0 {
				rw.Header().Set("Content-Type", "application/json")
				_ = json.NewEncoder(rw).Encode(BatchResponse{
					Status:  "completed",
					Results: batchResults,
					Summary: fmt.Sprintf("%d failed", len(batchResults)),
					Message: "No valid operations to create PR/MR.",
				})
				return
			}

			result, err := d.Writer.WritePRBatch(batchOps, email)
			if err != nil {
				writeJSONError(rw, http.StatusInternalServerError, "PR/MR batch write failed: "+err.Error())
				return
			}

			prTitle := fmt.Sprintf("[tenant-api] Batch update %d tenants", len(batchOps))
			tenantList := make([]string, len(batchOps))
			for i, op := range batchOps {
				tenantList[i] = op.TenantID
			}
			prBody := fmt.Sprintf("**Operator:** %s\n**Source:** tenant-manager UI (batch)\n**Tenants:** %s",
				email, strings.Join(tenantList, ", "))
			pr, err := d.PRClient.CreatePR(prTitle, prBody, result.BranchName, []string{"tenant-api", "auto-generated", "batch"})
			if err != nil {
				provider := d.PRClient.ProviderName()
				writeJSONError(rw, http.StatusServiceUnavailable, fmt.Sprintf("%s PR/MR creation failed: %s", provider, err.Error()))
				return
			}

			// Register each tenant's PR/MR in tracker
			for _, op := range batchOps {
				d.PRTracker.RegisterPR(platform.PRInfo{
					Number:   pr.Number,
					WebURL:   pr.WebURL,
					State:    "open",
					Title:    pr.Title,
					HeadRef:  result.BranchName,
					TenantID: op.TenantID,
				})
			}

			rw.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(rw).Encode(BatchResponse{
				Status:   "pending_review",
				PRURL:    pr.WebURL,
				PRNumber: pr.Number,
				Results:  batchResults,
				Summary:  fmt.Sprintf("%d included in PR/MR, %d failed", len(batchOps), len(batchResults)-len(batchOps)),
				Message:  fmt.Sprintf("Batch PR/MR created with %d tenant changes.", len(batchOps)),
			})
			return
		}

		// v2.6.0: Async mode — submit to goroutine pool and return immediately
		if r.URL.Query().Get("async") == "true" && d.Tasks != nil {
			task := d.Tasks.Submit(taskID, func(ctx context.Context) ([]async.TaskResult, error) {
				results := executeBatchOps(d.Writer, d.ConfigDir, req.Operations, email, groups, d.RBAC, d.Policy)
				asyncResults := make([]async.TaskResult, len(results))
				for i, br := range results {
					asyncResults[i] = async.TaskResult{
						TenantID: br.TenantID,
						Status:   br.Status,
						Message:  br.Message,
					}
				}
				return asyncResults, nil
			})

			rw.Header().Set("Content-Type", "application/json")
			rw.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(rw).Encode(map[string]interface{}{
				"status":   "pending",
				"task_id":  task.ID,
				"poll_url": fmt.Sprintf("/api/v1/tasks/%s", task.ID),
			})
			return
		}

		// Synchronous mode (default, backward compatible)
		results := executeBatchOps(d.Writer, d.ConfigDir, req.Operations, email, groups, d.RBAC, d.Policy)

		// Compute summary
		successes := 0
		failures := 0
		for _, result := range results {
			if result.Status == "ok" {
				successes++
			} else {
				failures++
			}
		}
		var summary string
		if failures == 0 {
			summary = fmt.Sprintf("%d succeeded", successes)
		} else if successes == 0 {
			summary = fmt.Sprintf("%d failed", failures)
		} else {
			summary = fmt.Sprintf("%d succeeded, %d failed", successes, failures)
		}

		rw.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(rw).Encode(BatchResponse{
			Status:  "completed",
			TaskID:  taskID,
			Results: results,
			Summary: summary,
		})
	}
}

// executeBatchOps runs batch operations synchronously and returns results.
// This function is shared between sync and async paths to ensure consistency.
func executeBatchOps(w *gitops.Writer, configDir string, ops []BatchOperation, email string, idpGroups []string, rbacMgr *rbac.Manager, policyMgr *policy.Manager) []BatchResult {
	results := make([]BatchResult, 0, len(ops))
	for _, op := range ops {
		if err := ValidateTenantID(op.TenantID); err != nil {
			results = append(results, BatchResult{TenantID: op.TenantID, Status: "error", Message: err.Error()})
			continue
		}
		if !rbacMgr.HasPermission(idpGroups, op.TenantID, rbac.PermWrite) {
			results = append(results, BatchResult{TenantID: op.TenantID, Status: "error", Message: "insufficient permissions for tenant " + op.TenantID})
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
		result := applyPatch(w, configDir, op, email)
		results = append(results, result)
	}
	return results
}

// applyPatch applies a single patch operation to a tenant config file.
func applyPatch(w *gitops.Writer, configDir string, op BatchOperation, authorEmail string) BatchResult {
	// Build minimal YAML content from the patch map
	yamlContent := buildPatchYAML(op.TenantID, op.Patch)

	if err := w.Write(op.TenantID, authorEmail, yamlContent); err != nil {
		msg := err.Error()
		if errors.Is(err, gitops.ErrConflict) {
			msg = "conflict: retry after refresh"
		}
		return BatchResult{TenantID: op.TenantID, Status: "error", Message: msg}
	}
	return BatchResult{TenantID: op.TenantID, Status: "ok"}
}

// buildPatchYAML constructs a minimal YAML string for a tenant patch.
// Uses yaml.Marshal for safe quoting of values containing special characters.
func buildPatchYAML(tenantID string, patch map[string]string) string {
	doc := map[string]interface{}{
		"tenants": map[string]interface{}{
			tenantID: patch,
		},
	}
	out, err := yaml.Marshal(doc)
	if err != nil {
		// Unreachable for map[string]string, but fallback gracefully
		fallback := fmt.Sprintf("tenants:\n  %s:\n", tenantID)
		for k, v := range patch {
			fallback += fmt.Sprintf("    %s: %q\n", k, v)
		}
		return fallback
	}
	return string(out)
}
