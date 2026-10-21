package handler

// batch_common.go holds the byte-identical logic shared by the tenant batch
// (tenant_batch.go) and group batch (group_batch.go) handlers: per-op access
// gating, result→async conversion, summary formatting, and the 202 pending
// response. Keeping one copy prevents the two paths from drifting apart.

import (
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"path/filepath"

	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/confd"
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
// tenant's environment/domain (#2830): `_profile`, whose profile's
// `_metadata` the tenant reads when no layer writes its own. `_metadata`
// itself is not one: a batch cannot set it (rejectMetadataPatch, #2859).
var scopeMetadataKeys = []string{"_profile"}

// gateBatchOpPostState is the post-state half of a batch op's write gate, the
// batch twin of RequireOrgWriteProposed: gateBatchOp authorized the op
// against the tenant's metadata on disk; an op that sets a
// scopeMetadataKeys key is authorized again against the metadata the tenant
// will have once it is written (postStateScopeMeta), so a caller scoped to
// environment=production cannot move a tenant to dev by patching its
// `_profile`. prior is the same tenant's earlier ops of the request, merged
// onto the same base before this one.
// Like the
// pre-state check it inherits the metadata write axis's flag: inert in
// shadow, refusing under --rbac-metadata-write-scope-enforce.
func gateBatchOpPostState(configDir string, prior []BatchOperation, op BatchOperation, p *rbac.VerifiedPrincipal, rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager) (BatchResult, bool) {
	return gateBatchOpPostStateAt(scopePairOf(postStateScopeMeta(configDir, prior, op), op.TenantID), op, p, rbacMgr, tenantOrg)
}

// gateBatchOpPostStateAt is gateBatchOpPostState over a post-state pair the
// caller computed (and keeps, to pass to the in-lock re-judgement).
func gateBatchOpPostStateAt(post scopePair, op BatchOperation, p *rbac.VerifiedPrincipal, rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager) (BatchResult, bool) {
	if !touchesScopeMetadata(op) {
		return BatchResult{}, false
	}
	if !OrgAllowed(rbacMgr, tenantOrg, p, op.TenantID, rbac.PermWrite, fixedScopeMeta(post)) {
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
	block := scopeBlockOnDisk(configDir, op.TenantID)
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

// summarizeBatchResults renders the human-readable summary line ("N
// succeeded", "N succeeded, M failed", "…, K skipped") from batch results,
// shared by both the tenant and group batch sync responses. It is
// async.SummarizeResults over the same results, so the sync response and
// GET /tasks/{id} for an async run cannot word one outcome two ways.
func summarizeBatchResults(results []BatchResult) string {
	return async.SummarizeResults(toTaskResults(results))
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

// scopeBlockOnDisk is tenants.<tenantID> of the tenant's file in configDir,
// or an empty block for a new tenant — the base of the metadata write-scope
// pre-check (postStateScopeMeta), and nothing else. A file that cannot be
// resolved or read is logged and read as empty: the pre-check then judges
// the patch's own metadata, and the write itself refuses a tenant file it
// cannot read before it merges anything (gitops: TargetUnreadableError /
// ErrMergeBaseNotLoadable; ADR-036 §4).
func scopeBlockOnDisk(configDir, tenantID string) map[string]any {
	block := map[string]any{}
	path, err := confd.ResolveTenantFile(configDir, tenantID)
	switch {
	case err == nil:
		data, problem := confd.ReadTenantFile(filepath.Dir(path), filepath.Base(path))
		if problem != confd.ProblemNone {
			slog.Warn("batch scope check: tenant file unreadable, judging the patch alone",
				"tenant", tenantID, "problem", problem)
		} else if err := confd.Judge(data); err != nil {
			// #3206: a file parse-tree refuses is not read either — its first
			// document is not the tenant's metadata (nothing serves it).
			// ⚠️ Visible effect: a caller restricted on environments/domains
			// whose scope the first document's metadata would have failed
			// (an op-level FORBIDDEN scope refusal before) now passes this pre-check and is
			// refused by the write itself with TENANT_CONFIG_NOT_LOADABLE
			// (the op's code; the batch is rejected) — nothing is written
			// either way, and that refusal names only the tenant the caller
			// addressed, its config_error and parse-tree's kind.
			slog.Warn("batch scope check: tenant file refused by parse-tree, judging the patch alone",
				"tenant", tenantID, "error", err)
			data = nil
		}
		for k, v := range tenantBlockOf(data, tenantID) {
			block[k] = v
		}
	case errors.Is(err, confd.ErrTenantFileNotFound):
		// A new tenant: the patches are the whole block.
	default:
		slog.Warn("batch scope check: tenant file not resolved, judging the patch alone",
			"tenant", tenantID, "error", err)
	}
	return block
}

// tenantBlockOf returns `tenants.<tenantID>` of a tenant document as yaml.v3
// reads it, or nil when the document does not parse or does not declare the
// tenant.
func tenantBlockOf(body []byte, tenantID string) map[string]any {
	var doc struct {
		Tenants map[string]map[string]any `yaml:"tenants"`
	}
	if err := yaml.Unmarshal(body, &doc); err != nil {
		return nil
	}
	return doc.Tenants[tenantID]
}

// applyBatchEdit lays one op over a tenant block as mergePatchYAML lays it
// over the file: its patch keys set, then its unset keys removed (the two
// are disjoint — validateBatchEdit).
func applyBatchEdit(block map[string]any, op BatchOperation) {
	for k, v := range op.Patch {
		block[k] = v
	}
	for _, k := range op.Unset {
		delete(block, k)
	}
}
