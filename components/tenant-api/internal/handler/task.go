package handler

import (
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/rbac"
)

// GetTask handles GET /api/v1/tasks/{id}
// Returns current task status for polling.
// If task not found (e.g., pod restarted), returns 404 with hint.
//
// **v2.8.0 B-6 PR-2 hardening**: filters `task.Results[*]` to only
// include entries for tenants the caller can read. Without this
// filter, anyone with PermRead on the API could poll any task ID
// and learn which tenants it touched + their pass/fail status —
// info disclosure on tenant existence + operational state.
//
// Filter behaviour:
//   - 0 accessible results AND original was non-empty → return 403
//     (caller has no access to ANY of the task's tenants)
//   - some accessible → return Task with `Results` truncated to
//     accessible entries (response shape preserved for clients)
//   - all accessible → return Task as-is
//   - empty original (Task with no Results yet — still running) →
//     return Task as-is (no tenants disclosed yet)
func (d *Deps) GetTask() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		taskID := chi.URLParam(r, "id")

		task, ok := d.Tasks.Get(taskID)
		if !ok {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusNotFound)
			_ = json.NewEncoder(w).Encode(map[string]string{
				"error": "task_not_found",
				"hint":  "pod_may_have_restarted",
			})
			return
		}

		// Tenant-scope the Results array. Defensive copy of the
		// task struct so we never mutate the in-memory copy that
		// other concurrent pollers would observe.
		idpGroups := rbac.RequestGroups(r)
		filtered := filterTaskResults(d.RBAC, idpGroups, task.Results)
		if len(task.Results) > 0 && len(filtered) == 0 {
			// Caller has zero access to any of the touched tenants.
			// 403 (not 404) — the task exists, just none of its
			// tenants are within the caller's RBAC scope.
			writeJSONError(w, http.StatusForbidden,
				"insufficient permission to read task results: caller has no access to any of the task's tenants")
			return
		}
		// Return a defensive copy (don't mutate manager-held pointer).
		response := task
		response.Results = filtered

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(response)
	}
}

// filterTaskResults returns the subset of results visible to the
// caller. Open-mode RBAC (no _rbac.yaml) returns the input as-is
// (HasPermission short-circuits). Mirrors filterAccessibleMembers /
// filterAccessiblePRs via the shared filterByRBAC generic.
func filterTaskResults(rbacMgr *rbac.Manager, idpGroups []string, results []async.TaskResult) []async.TaskResult {
	return filterByRBAC(rbacMgr, idpGroups, results, tenantIDFromTaskResult, rbac.PermRead)
}

// tenantIDFromTaskResult is the per-element extractor for filterByRBAC
// over async.TaskResult slices.
func tenantIDFromTaskResult(r async.TaskResult) string { return r.TenantID }
