package handler

import (
	"encoding/json"
	"net/http"
	"os"
)

// Health handles GET /health — always returns 200 OK.
func Health(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

// Ready handles GET /ready — returns 503 when the config dir is not
// stat-able (e.g. ConfigMap mount failed, PV detached). Returns 200
// only when the directory is readable, so K8s drains traffic away
// from a pod whose tenant data the app cannot serve.
func (d *Deps) Ready() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")

		info, err := os.Stat(d.ConfigDir)
		if err != nil {
			w.WriteHeader(http.StatusServiceUnavailable)
			_ = json.NewEncoder(w).Encode(map[string]string{
				"status":     "not_ready",
				"config_dir": d.ConfigDir,
				"error":      err.Error(),
			})
			return
		}
		if !info.IsDir() {
			w.WriteHeader(http.StatusServiceUnavailable)
			_ = json.NewEncoder(w).Encode(map[string]string{
				"status":     "not_ready",
				"config_dir": d.ConfigDir,
				"error":      "config_dir is not a directory",
			})
			return
		}

		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]string{
			"status":     "ready",
			"config_dir": d.ConfigDir,
		})
	}
}
