package handler

import (
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
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
// #3123: a task is visible to the caller who submitted it, and to nobody
// else — any other caller gets the byte-identical 404 of a task that does not
// exist (writeTaskNotFound). Task ids are random (async.NewTaskID). Before,
// ids were predictable, a task with no results yet was returned as-is to
// anyone, and a task whose results named only tenants the caller cannot read
// answered 403 — "it exists".
//
// The submitter's results are still filtered by the caller's CURRENT read
// permission: it may have been narrowed since the task was submitted.
func GetTask(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		taskID := chi.URLParam(r, "id")
		p := rbac.RequestPrincipal(r)

		task, ok := d.Tasks.Get(taskID)
		if !ok || !taskOwnedBy(task, p) {
			writeTaskNotFound(w, r)
			return
		}

		// Tenant-scope the Results array. Defensive copy of the
		// task struct so we never mutate the in-memory copy that
		// other concurrent pollers would observe.
		filtered := filterTaskResults(d.RBAC, d.TenantOrg, p, task.Results)
		// Return a defensive copy (don't mutate manager-held pointer).
		response := task
		response.Results = filtered
		// #1530: the stored summary counts EVERY result, including the ones
		// just filtered out — "1 succeeded, 1 failed" beside one visible
		// result tells the caller a hidden tenant was in the run. Re-render
		// it over what the caller gets. A task with no summary yet (pending,
		// running, failed) keeps none.
		if response.Summary != "" {
			response.Summary = async.SummarizeResults(filtered)
		}

		writeJSON(w, http.StatusOK, response)
	}
}

// writeTaskNotFound is the one 404 for a task that does not exist OR that the
// caller did not submit (#3123), so the two stay byte-identical. Its body names
// no task id.
func writeTaskNotFound(w http.ResponseWriter, r *http.Request) {
	WriteErrorEnvelope(w, r, http.StatusNotFound, ErrorResponse{
		Error: "task_not_found",
		Code:  errcode.TaskNotFound,
		Extra: map[string]any{
			"hint": "pod_may_have_restarted",
		},
	})
}

// taskOwner is the Task.Owner recorded for a task p submits: the principal's
// attesting source and stable subject (VerifiedPrincipal.Subject — the email
// on the human path, the ServiceAccount username on the machine path), not
// the email alone, which a machine principal does not have. "" for no
// principal, which owns nothing (taskOwnedBy).
func taskOwner(p *rbac.VerifiedPrincipal) string {
	if p == nil || p.Subject == "" {
		return ""
	}
	return p.Source + ":" + p.Subject
}

// taskOwnedBy reports whether p submitted task.
func taskOwnedBy(task *async.Task, p *rbac.VerifiedPrincipal) bool {
	owner := taskOwner(p)
	return owner != "" && task.Owner == owner
}

// submitTask submits fn under a fresh random id owned by p and answers the 202,
// or a 500 when the manager refuses it (a duplicate id, which a 128-bit random
// id makes practically impossible).
func submitTask(w http.ResponseWriter, r *http.Request, d *Deps, taskID string, p *rbac.VerifiedPrincipal, fn async.TaskFunc) {
	task, err := d.Tasks.Submit(taskID, taskOwner(p), fn)
	if err != nil {
		WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
		return
	}
	write202Pending(w, task)
}

// filterTaskResults returns the subset of results visible to the
// caller. Open-mode RBAC (no _rbac.yaml) returns the input as-is
// (OrgAllowedRead short-circuits). Org-aware read (ADR-027 / LD-6 P4c):
// mirrors filterAccessibleMembers / filterAccessiblePRs via the shared
// filterByRBAC generic. tenantOrg may be nil (nil-receiver-safe → unlabeled).
func filterTaskResults(rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, p *rbac.VerifiedPrincipal, results []async.TaskResult) []async.TaskResult {
	return filterByRBAC(rbacMgr, tenantOrg, p, results, tenantIDFromTaskResult, rbac.PermRead)
}

// tenantIDFromTaskResult is the per-element extractor for filterByRBAC
// over async.TaskResult slices.
func tenantIDFromTaskResult(r async.TaskResult) string { return r.TenantID }
