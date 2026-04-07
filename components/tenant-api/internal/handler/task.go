package handler

import (
	"encoding/json"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/async"
)

// GetTask handles GET /api/v1/tasks/{id}
// Returns current task status for polling.
// If task not found (e.g., pod restarted), returns 404 with hint.
func GetTask(taskMgr *async.Manager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		taskID := chi.URLParam(r, "id")

		task, ok := taskMgr.Get(taskID)
		if !ok {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusNotFound)
			_ = json.NewEncoder(w).Encode(map[string]string{
				"error": "task_not_found",
				"hint":  "pod_may_have_restarted",
			})
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(task)
	}
}

// ListTasks handles GET /api/v1/tasks
// Returns all known tasks (limited to in-memory state).
func ListTasks(taskMgr *async.Manager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Simple: return message about in-memory nature
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"message": "task list endpoint — use GET /api/v1/tasks/{id} to poll specific tasks",
		})
	}
}
