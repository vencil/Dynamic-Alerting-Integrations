package handler

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"gopkg.in/yaml.v3"
)

// BatchOperation describes a single operation in a batch request.
type BatchOperation struct {
	TenantID string            `json:"tenant_id"`
	Patch    map[string]string `json:"patch"` // key → value to set (e.g., "_silent_mode": "warning")
}

// BatchRequest is the body for POST /api/v1/tenants/batch.
type BatchRequest struct {
	Operations []BatchOperation `json:"operations"`
}

// BatchResult is the per-tenant result in a batch response.
type BatchResult struct {
	TenantID string `json:"tenant_id"`
	Status   string `json:"status"` // "ok" | "error"
	Message  string `json:"message,omitempty"`
}

// BatchResponse is the full response for POST /api/v1/tenants/batch.
// task_id is pre-reserved for v2.5.0 async queue upgrade — currently always "completed".
type BatchResponse struct {
	Status  string        `json:"status"`   // "completed" in v2.4.0
	TaskID  string        `json:"task_id"`  // reserved for v2.5.0 async
	Results []BatchResult `json:"results"`
}

// BatchTenants handles POST /api/v1/tenants/batch
//
// Executes a list of patch operations synchronously.
// The sync.Mutex inside gitops.Writer ensures serial execution.
// Response includes task_id for future async upgrade compatibility.
//
// @Summary     Batch tenant operations
// @Description Apply patch operations to multiple tenants in one call.
// @Tags        tenants
// @Accept      json
// @Produce     json
// @Param       body body     BatchRequest true "Batch operations"
// @Success     200  {object} BatchResponse
// @Failure     400  {object} map[string]string
// @Router      /api/v1/tenants/batch [post]
func BatchTenants(w *gitops.Writer, configDir string, rbacMgr *rbac.Manager) http.HandlerFunc {
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

		taskID := fmt.Sprintf("batch-%s-%04d",
			time.Now().UTC().Format("20060102"), len(req.Operations))

		results := make([]BatchResult, 0, len(req.Operations))

		for _, op := range req.Operations {
			if err := ValidateTenantID(op.TenantID); err != nil {
				results = append(results, BatchResult{
					TenantID: op.TenantID, Status: "error", Message: err.Error(),
				})
				continue
			}
			// Per-tenant write permission check (the route-level middleware
			// only checks wildcard; scoped users need per-tenant validation).
			if !rbacMgr.HasPermission(groups, op.TenantID, rbac.PermWrite) {
				results = append(results, BatchResult{
					TenantID: op.TenantID, Status: "error",
					Message: "insufficient permissions for tenant " + op.TenantID,
				})
				continue
			}
			result := applyPatch(w, configDir, op, email)
			results = append(results, result)
		}

		rw.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(rw).Encode(BatchResponse{
			Status:  "completed",
			TaskID:  taskID,
			Results: results,
		})
	}
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
