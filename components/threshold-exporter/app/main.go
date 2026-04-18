package main

import (
	"context"
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"sort"
	"strings"
	"syscall"
	"time"
)

var (
	configPath     string
	configDir      string
	listenAddr     string
	reloadInterval time.Duration
	scanDebounce   time.Duration
)

func init() {
	flag.StringVar(&configPath, "config", "", "Path to single threshold config file (legacy mode)")
	flag.StringVar(&configDir, "config-dir", "", "Path to threshold config directory (multi-file mode)")
	flag.StringVar(&listenAddr, "listen", ":8080", "HTTP listen address")
	flag.DurationVar(&reloadInterval, "reload-interval", 30*time.Second, "Config reload interval")
	// v2.7.0 (ADR-017/018): coalesce bursts of file changes into a single
	// hierarchical reload. Set to 0 to disable (synchronous reload per
	// detected diff, matching v2.6.0 behavior).
	flag.DurationVar(&scanDebounce, "scan-debounce", DefaultDebounceWindow, "Debounce window for hierarchical conf.d reload (0 disables)")
}

func main() {
	flag.Parse()

	// Allow env override
	if v := os.Getenv("CONFIG_PATH"); v != "" {
		configPath = v
	}
	if v := os.Getenv("CONFIG_DIR"); v != "" {
		configDir = v
	}
	if v := os.Getenv("LISTEN_ADDR"); v != "" {
		listenAddr = v
	}

	// Auto-detect mode: -config-dir takes precedence, then -config, then default
	resolvedPath := resolveConfigPath()

	log.Printf("threshold-exporter starting")
	log.Printf("  config:   %s", resolvedPath)
	log.Printf("  listen:   %s", listenAddr)
	log.Printf("  reload:   %s", reloadInterval)
	log.Printf("  debounce: %s", scanDebounce)

	// Load initial config
	manager := NewConfigManagerWithDebounce(resolvedPath, scanDebounce)
	if err := manager.Load(); err != nil {
		log.Fatalf("Failed to load config: %v", err)
	}

	// Create metrics collector
	collector := NewThresholdCollector(manager)

	// Start config reload goroutine with stop channel
	stopCh := make(chan struct{})
	go manager.WatchLoop(reloadInterval, stopCh)

	// HTTP handlers
	mux := http.NewServeMux()
	mux.Handle("/metrics", collector.MetricsHandler())
	mux.HandleFunc("/health", healthHandler)
	mux.HandleFunc("/ready", readyHandler(manager))
	mux.HandleFunc("/api/v1/config", configViewHandler(manager))

	server := &http.Server{
		Addr:              listenAddr,
		Handler:           mux,
		ReadTimeout:       5 * time.Second,
		ReadHeaderTimeout: 3 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    8192,
	}

	// Graceful shutdown
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		log.Printf("Listening on %s", listenAddr)
		if err := server.ListenAndServe(); err != http.ErrServerClosed {
			log.Fatalf("HTTP server error: %v", err)
		}
	}()

	<-sigCh
	log.Println("Shutting down...")

	// Stop WatchLoop goroutine
	close(stopCh)

	// Release debounce timer (v2.7.0 Phase 3, §8.11.2 trap #12). Safe
	// even on single-file mode — Close is a no-op when debounceTimer is nil.
	manager.Close()

	// Graceful HTTP shutdown with timeout
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		log.Printf("HTTP server shutdown error: %v", err)
	}
	log.Println("Server stopped")
}

// resolveConfigPath determines the config path based on flags and auto-detection.
// Priority: -config-dir > -config > auto-detect default paths.
func resolveConfigPath() string {
	if configDir != "" {
		return configDir
	}
	if configPath != "" {
		return configPath
	}

	// Auto-detect: check if default directory exists, otherwise fall back to file
	defaultDir := "/etc/threshold-exporter/conf.d"
	defaultFile := "/etc/threshold-exporter/config.yaml"

	if info, err := os.Stat(defaultDir); err == nil && info.IsDir() {
		log.Printf("Auto-detected config directory: %s", defaultDir)
		return defaultDir
	}
	log.Printf("Using legacy single-file config: %s", defaultFile)
	return defaultFile
}

func healthHandler(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
	fmt.Fprintln(w, "ok")
}

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
