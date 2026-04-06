package handler

import (
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
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
}

// GroupBatch handles POST /api/v1/groups/{id}/batch
//
// Applies a patch operation to all members of a group.
// Per-tenant RBAC write permission is checked for each member.
//
// @Summary     Batch operation on group members
// @Description Apply a patch to all tenants in a group.
// @Tags        groups
// @Accept      json
// @Produce     json
// @Param       id   path     string            true "Group ID"
// @Param       body body     GroupBatchRequest  true "Patch to apply"
// @Success     200  {object} GroupBatchResponse
// @Failure     400  {object} map[string]string
// @Failure     404  {object} map[string]string
// @Router      /api/v1/groups/{id}/batch [post]
func GroupBatch(groupMgr *groups.Manager, writer *gitops.Writer, configDir string, rbacMgr *rbac.Manager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		groupID := chi.URLParam(r, "id")
		if err := groups.ValidateGroupID(groupID); err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}

		email := rbac.RequestEmail(r)
		idpGroups := rbac.RequestGroups(r)

		g, ok := groupMgr.GetGroup(groupID)
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

		results := make([]BatchResult, 0, len(g.Members))
		for _, tenantID := range g.Members {
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

			op := BatchOperation{TenantID: tenantID, Patch: req.Patch}
			result := applyPatch(writer, configDir, op, email)
			results = append(results, result)
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(GroupBatchResponse{
			Status:  "completed",
			TaskID:  taskID,
			GroupID: groupID,
			Results: results,
		})
	}
}

// applyPatch is reused from tenant_batch.go — already imported via the handler package.
// (The function is defined in tenant_batch.go and accessible here since both files are in the same package.)
