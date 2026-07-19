package handler

// batch_common.go holds the byte-identical logic shared by the tenant batch
// (tenant_batch.go) and group batch (group_batch.go) handlers: per-op access
// gating, result→async conversion, summary formatting, and the 202 pending
// response. Keeping one copy prevents the two paths from drifting apart.

import (
	"fmt"
	"net/http"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
)

// gateBatchOp runs the per-op access gate shared by the tenant and group batch
// execution loops: tenant-id validation followed by the org-scoped write
// permission check (ADR-027 / LD-6 P4b), resolved at execution time. It returns
// the error BatchResult and true when the op is rejected — the caller should
// append it and `continue` — or a zero BatchResult and false when the op passes
// and execution should proceed.
//
// Note: the tenant path layers an additional policy.CheckWrite step AFTER this
// gate (group has none), so that check stays in executeBatchOps; only the two
// checks that are identical across both paths live here.
func gateBatchOp(tenantID string, p *rbac.VerifiedPrincipal, rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager) (BatchResult, bool) {
	if err := ValidateTenantID(tenantID); err != nil {
		return BatchResult{TenantID: tenantID, Status: "error", Message: err.Error()}, true
	}
	if !OrgAllowed(rbacMgr, tenantOrg, p, tenantID, rbac.PermWrite) {
		return BatchResult{TenantID: tenantID, Status: "error", Message: "insufficient permissions for tenant " + tenantID}, true
	}
	return BatchResult{}, false
}

// toTaskResults converts batch results into the async pool's TaskResult shape
// (TenantID/Status/Message), for the async submission path in both handlers.
func toTaskResults(results []BatchResult) []async.TaskResult {
	asyncResults := make([]async.TaskResult, len(results))
	for i, br := range results {
		asyncResults[i] = async.TaskResult{
			TenantID: br.TenantID,
			Status:   br.Status,
			Message:  br.Message,
		}
	}
	return asyncResults
}

// summarizeBatchResults renders the human-readable "N succeeded"/"N failed"/
// "N succeeded, M failed" summary line from batch results, shared by both the
// tenant and group batch sync responses.
func summarizeBatchResults(results []BatchResult) string {
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
	return summary
}

// write202Pending writes the 202 Accepted async-submission response shared by
// both batch handlers. The writer is passed in so callers using either `rw`
// (tenant) or `w` (group) work unchanged.
func write202Pending(w http.ResponseWriter, task *async.Task) {
	writeJSON(w, http.StatusAccepted, map[string]interface{}{
		"status":   "pending",
		"task_id":  task.ID,
		"poll_url": fmt.Sprintf("/api/v1/tasks/%s", task.ID),
	})
}
