package main

// HTTP handlers for the read-only API surface (`/health`, `/ready`,
// `/api/v1/config`). The simulate handler lives in handler_simulate.go;
// metrics scraping goes through ThresholdCollector.MetricsHandler().
//
// Handler signatures are pinned by main_test.go:
//
//   - healthHandler — bare http.HandlerFunc shape, called directly
//   - readyHandler(*ConfigManager) — closure, captures the manager
//   - configViewHandler(*ConfigManager) — closure, captures the manager
//
// Keeping these in their own file isolates the transport layer from
// boot wiring (main.go) and config logic (config.go / config_resolve.go).

import (
	"fmt"
	"net/http"
	"sort"
	"strings"
	"time"
)

// healthHandler answers the K8s liveness probe. Always 200 once the
// process has started — readiness is the gate that depends on config
// load state, not liveness.
func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	fmt.Fprintln(w, "ok")
}

// readyHandler returns 503 until the first Load() succeeds. K8s
// readiness probes use this to gate Service endpoint inclusion — a Pod
// without loaded config would emit an empty /metrics, which would
// look like every threshold suddenly disappeared from the platform.
func readyHandler(manager *ConfigManager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if manager.IsLoaded() {
			w.WriteHeader(http.StatusOK)
			fmt.Fprintln(w, "ready")
		} else {
			w.WriteHeader(http.StatusServiceUnavailable)
			fmt.Fprintln(w, "config not loaded")
		}
	}
}

// configViewHandler dumps the resolved config + tenant list as a
// human-readable text/plain page. Used for ad-hoc debugging — the
// machine-consumable shape lives at /api/v1/tenants/simulate (POST).
//
// `?at=<RFC3339>` overrides the resolve clock for inspecting future
// scheduled-override windows without waiting wall-clock time.
func configViewHandler(manager *ConfigManager) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/plain")
		fmt.Fprintf(w, "Config loaded: %v\n", manager.IsLoaded())
		fmt.Fprintf(w, "Last reload:   %s\n", manager.LastReload().Format(time.RFC3339))
		fmt.Fprintf(w, "Config mode:   %s\n", manager.Mode())

		cfg := manager.GetConfig()
		if cfg == nil {
			fmt.Fprintln(w, "No config loaded")
			return
		}

		// Determine resolve time: ?at=2006-01-02T15:04:05Z for debugging scheduled overrides
		resolveTime := time.Now()
		if atParam := r.URL.Query().Get("at"); atParam != "" {
			if parsed, err := time.Parse(time.RFC3339, atParam); err == nil {
				resolveTime = parsed
				fmt.Fprintf(w, "Resolve at:    %s (overridden)\n", resolveTime.Format(time.RFC3339))
			} else {
				fmt.Fprintf(w, "Resolve at:    now (invalid ?at= param: %v)\n", err)
			}
		}

		fmt.Fprintf(w, "\nDefaults (%d metrics):\n", len(cfg.Defaults))
		for k, v := range cfg.Defaults {
			fmt.Fprintf(w, "  %s: %.0f\n", k, v)
		}

		fmt.Fprintf(w, "\nTenants (%d):\n", len(cfg.Tenants))
		for tenant, metrics := range cfg.Tenants {
			fmt.Fprintf(w, "  %s:\n", tenant)
			for k, v := range metrics {
				if len(v.Overrides) > 0 {
					fmt.Fprintf(w, "    %s: %s (+ %d time overrides)\n", k, v.Default, len(v.Overrides))
				} else {
					fmt.Fprintf(w, "    %s: %s\n", k, v.Default)
				}
			}
		}

		// Show silent mode status
		silentModes := cfg.ResolveSilentModes()
		if len(silentModes) > 0 {
			fmt.Fprintf(w, "\nSilent modes (%d):\n", len(silentModes))
			for _, sm := range silentModes {
				fmt.Fprintf(w, "  tenant=%s target_severity=%s\n", sm.Tenant, sm.TargetSeverity)
			}
		}

		// Show resolved state at the determined time
		fmt.Fprintf(w, "\nResolved thresholds:\n")
		resolved := cfg.ResolveAt(resolveTime)
		for _, t := range resolved {
			// Format label pairs for display (exact + regex)
			var pairs []string
			if len(t.CustomLabels) > 0 {
				keys := make([]string, 0, len(t.CustomLabels))
				for k := range t.CustomLabels {
					keys = append(keys, k)
				}
				sort.Strings(keys)
				for _, k := range keys {
					pairs = append(pairs, fmt.Sprintf("%s=%q", k, t.CustomLabels[k]))
				}
			}
			if len(t.RegexLabels) > 0 {
				keys := make([]string, 0, len(t.RegexLabels))
				for k := range t.RegexLabels {
					keys = append(keys, k)
				}
				sort.Strings(keys)
				for _, k := range keys {
					pairs = append(pairs, fmt.Sprintf("%s=~%q", k, t.RegexLabels[k]))
				}
			}
			if len(pairs) > 0 {
				fmt.Fprintf(w, "  tenant=%s metric=%s{%s} value=%.0f severity=%s component=%s\n",
					t.Tenant, t.Metric, strings.Join(pairs, ", "), t.Value, t.Severity, t.Component)
			} else {
				fmt.Fprintf(w, "  tenant=%s metric=%s value=%.0f severity=%s component=%s\n",
					t.Tenant, t.Metric, t.Value, t.Severity, t.Component)
			}
		}
	}
}
