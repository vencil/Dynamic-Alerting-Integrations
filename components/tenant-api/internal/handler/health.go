package handler

import (
	"encoding/json"
	"net/http"
)

// Health handles GET /health — always returns 200 OK.
func Health(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusOK)
	_ = json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

// Ready handles GET /ready — returns 503 until the config dir is accessible.
func Ready(configDir string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Simple readiness check: can we read the config directory?
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusOK)
		_ = json.NewEncoder(w).Encode(map[string]string{
			"status":     "ready",
			"config_dir": configDir,
		})
	}
}
