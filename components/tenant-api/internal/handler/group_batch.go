package handler

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/rbac"
)

// GroupBatchRequest is the body for POST /api/v1/groups/{id}/batch.
// Applies a patch to all members of the specified group.
type GroupBatchRequest struct {
	Patch map[string]string `json:"patch"` // key → value (e.g., "_silent_mode": "warning")
}

// GroupBatchResponse is the response for POST /api/v1/groups/{id}/batch.
type GroupBatchResponse struct {
	Status  string        `json:"status"`
	TaskID  string        `json:"task_id"`
	GroupID string        `json:"group_id"`
	Results []BatchResult `json:"results"`
	Summary string        `json:"summary"`  // e.g., "5 succeeded, 1 failed"
}

// GroupBatch handles POST /api/v1/groups/{id}/batch
//
// Applies a patch operation to all members of a group.
// Per-tenant RBAC write permission is checked for each member.
// Supports async mode via ?async=true query parameter.
//
// Query Parameters:
//   ?async=true  — Enable async mode; returns 202 with task_id for polling
//   (default)    — Sync mode; returns 200 with completed results
//
// @Summary     Batch operation on group members
// @Description Apply a patch to all tenants in a group.
// @Tags        groups
// @Accept      json
// @Produce     json
// @Param       id    path     string            true "Group ID"
// @Param       body  body     GroupBatchRequest true "Patch to apply"
// @Param       async query   string            false "Enable async mode (true/false)"
// @Success     200   {object} GroupBatchResponse
// @Success     202   {object} map[string]interface{}
// @Failure     400   {object} map[string]string
// @Failure     404   {object} map[string]string
// @Router      /api/v1/groups/{id}/batch [post]
func (d *Deps) GroupBatch() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)
		idpGroups := rbac.RequestGroups(r)

		g, ok := d.Groups.GetGroup(groupID)
		if !ok {
			writeJSONError(w, http.StatusNotFound, "group not found: "+groupID)
			return
		}

		var req GroupBatchRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			writeJSONError(w, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if len(req.Patch) == 0 {
			writeJSONError(w, http.StatusBadRequest, "patch must not be empty")
			return
		}

		if len(g.Members) == 0 {
			writeJSONError(w, http.StatusBadRequest, "group has no members")
			return
		}

		taskID := fmt.Sprintf("group-batch-%s-%s",
			groupID, time.Now().UTC().Format("20060102-150405"))

		// v2.6.0: Async mode — submit to goroutine pool and return immediately
		if r.URL.Query().Get("async") == "true" && d.Tasks != nil {
			task := d.Tasks.Submit(taskID, func(ctx context.Context) ([]async.TaskResult, error) {
				results := executeGroupBatchOps(d.Writer, d.ConfigDir, g.Members, req.Patch, email, idpGroups, d.RBAC)
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

			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusAccepted)
			_ = json.NewEncoder(w).Encode(map[string]interface{}{
				"status":   "pending",
				"task_id":  task.ID,
				"poll_url": fmt.Sprintf("/api/v1/tasks/%s", task.ID),
			})
			return
		}

		// Synchronous mode (default, backward compatible)
		results := executeGroupBatchOps(d.Writer, d.ConfigDir, g.Members, req.Patch, email, idpGroups, d.RBAC)

		// Compute summary statistics
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

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(GroupBatchResponse{
			Status:  "completed",
			TaskID:  taskID,
			GroupID: groupID,
			Results: results,
			Summary: summary,
		})
	}
}

// executeGroupBatchOps runs group batch operations synchronously and returns results.
// This function is shared between sync and async paths to ensure consistency.
func executeGroupBatchOps(writer *gitops.Writer, configDir string, members []string, patch map[string]string, email string, idpGroups []string, rbacMgr *rbac.Manager) []BatchResult {
	results := make([]BatchResult, 0, len(members))
	for _, tenantID := range members {
		if err := ValidateTenantID(tenantID); err != nil {
			results = append(results, BatchResult{
				TenantID: tenantID, Status: "error", Message: err.Error(),
			})
			continue
		}

		if !rbacMgr.HasPermission(idpGroups, tenantID, rbac.PermWrite) {
			results = append(results, BatchResult{
				TenantID: tenantID, Status: "error",
				Message: "insufficient permissions for tenant " + tenantID,
			})
			continue
		}

		op := BatchOperation{TenantID: tenantID, Patch: patch}
		result := applyPatch(writer, configDir, op, email)
		results = append(results, result)
	}
	return results
}

// applyPatch is reused from tenant_batch.go — already imported via the handler package.
// (The function is defined in tenant_batch.go and accessible here since both files are in the same package.)
