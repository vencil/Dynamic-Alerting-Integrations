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
	"gopkg.in/yaml.v3"
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
func gateBatchOp(tenantID string, p *rbac.VerifiedPrincipal, rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, meta ScopeMetaFunc) (BatchResult, bool) {
	if err := ValidateWritableTenantID(tenantID); err != nil {
		return BatchResult{TenantID: tenantID, Status: "error", Message: err.Error()}, true
	}
	if !OrgAllowed(rbacMgr, tenantOrg, p, tenantID, rbac.PermWrite, meta) {
		return BatchResult{TenantID: tenantID, Status: "error", Message: "insufficient permissions for tenant " + tenantID}, true
	}
	return BatchResult{}, false
}

// scopeMetadataKeys are the tenant keys a batch op can write that decide the
// tenant's environment/domain (#2830): `_metadata` in its string form, and
// `_profile`, whose profile's `_metadata` the tenant reads when no layer
// writes its own.
var scopeMetadataKeys = []string{"_metadata", "_profile"}

// gateBatchOpPostState is the post-state half of a batch op's write gate, the
// batch twin of RequireOrgWriteProposed: gateBatchOp authorized the op
// against the tenant's metadata on disk; an op that sets a
// scopeMetadataKeys key is authorized again against the metadata the tenant
// will have once it is written (postStateScopeMeta), so a caller scoped to
// environment=production cannot move a tenant to dev by patching its
// `_profile`. prior is as for batchRoutingViolations: the same tenant's
// earlier ops merged onto the same base before this one (PR mode); nil when
// each op is written before the next is judged (direct mode). Like the
// pre-state check it inherits the metadata write axis's flag: inert in
// shadow, refusing under --rbac-metadata-write-scope-enforce.
func gateBatchOpPostState(configDir string, prior []BatchOperation, op BatchOperation, p *rbac.VerifiedPrincipal, rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager) (BatchResult, bool) {
	if !touchesScopeMetadata(op) {
		return BatchResult{}, false
	}
	if !OrgAllowed(rbacMgr, tenantOrg, p, op.TenantID, rbac.PermWrite, postStateScopeMeta(configDir, prior, op)) {
		return BatchResult{TenantID: op.TenantID, Status: "error",
			Message: "insufficient permissions for the tenant metadata this op proposes — its environment/domain would place tenant " +
				op.TenantID + " outside your scope (#1597)"}, true
	}
	return BatchResult{}, false
}

// touchesScopeMetadata reports whether op's patch sets a scopeMetadataKeys
// key. Its unset cannot remove one: unsetAllowedKeys admits neither, and
// widening it to one of them must widen this too.
func touchesScopeMetadata(op BatchOperation) bool {
	for _, k := range scopeMetadataKeys {
		if _, sets := op.Patch[k]; sets {
			return true
		}
	}
	return false
}

// postStateScopeMeta reads the tenant's environment/domain from its block on
// disk with prior, then op, laid over it (applyBatchEdit), read as a proposed
// body is (proposedScopeMeta: over configDir's root platform layer).
func postStateScopeMeta(configDir string, prior []BatchOperation, op BatchOperation) ScopeMetaFunc {
	block := tenantBlockOnDisk(configDir, op.TenantID)
	for _, p := range prior {
		applyBatchEdit(block, p)
	}
	applyBatchEdit(block, op)
	body, err := yaml.Marshal(map[string]any{"tenants": map[string]any{op.TenantID: block}})
	if err != nil {
		// Not reachable for a block decoded from YAML plus string patches;
		// an unreadable proposal reads as unlabeled, as an unreadable file does.
		return func(string) (string, string) { return "", "" }
	}
	return proposedScopeMeta(configDir, string(body))
}

// toTaskResults converts batch results into the async pool's TaskResult shape
// (TenantID/Status/Message/Warnings), for the async submission path in both
// handlers.
func toTaskResults(results []BatchResult) []async.TaskResult {
	asyncResults := make([]async.TaskResult, len(results))
	for i, br := range results {
		asyncResults[i] = async.TaskResult{
			TenantID: br.TenantID,
			Status:   br.Status,
			Message:  br.Message,
			Code:     br.Code,
			Warnings: br.Warnings,
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
