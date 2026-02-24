package main

import (
	"flag"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"
)

var (
	configPath     string
	configDir      string
	listenAddr     string
	reloadInterval time.Duration
)

func init() {
	flag.StringVar(&configPath, "config", "", "Path to single threshold config file (legacy mode)")
	flag.StringVar(&configDir, "config-dir", "", "Path to threshold config directory (multi-file mode)")
	flag.StringVar(&listenAddr, "listen", ":8080", "HTTP listen address")
	flag.DurationVar(&reloadInterval, "reload-interval", 30*time.Second, "Config reload interval")
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

	// Load initial config
	manager := NewConfigManager(resolvedPath)
	if err := manager.Load(); err != nil {
		log.Fatalf("Failed to load config: %v", err)
	}

	// Create metrics collector
	collector := NewThresholdCollector(manager)

	// Start config reload goroutine
	go manager.WatchLoop(reloadInterval)

	// HTTP handlers
	mux := http.NewServeMux()
	mux.Handle("/metrics", collector.MetricsHandler())
	mux.HandleFunc("/health", healthHandler)
	mux.HandleFunc("/ready", readyHandler(manager))
	mux.HandleFunc("/api/v1/config", configViewHandler(manager))

	server := &http.Server{
		Addr:         listenAddr,
		Handler:      mux,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 10 * time.Second,
	}

	// Graceful shutdown
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		log.Printf("Listening on %s", listenAddr)
		if err := server.ListenAndServe(); err != http.ErrServerClosed {
			log.Fatalf("HTTP server error: %v", err)
		}
	}()

	<-stop
	log.Println("Shutting down...")
	server.Close()
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

		fmt.Fprintf(w, "\nDefaults (%d metrics):\n", len(cfg.Defaults))
		for k, v := range cfg.Defaults {
			fmt.Fprintf(w, "  %s: %.0f\n", k, v)
		}

		fmt.Fprintf(w, "\nTenants (%d):\n", len(cfg.Tenants))
		for tenant, metrics := range cfg.Tenants {
			fmt.Fprintf(w, "  %s:\n", tenant)
			for k, v := range metrics {
				fmt.Fprintf(w, "    %s: %s\n", k, v)
			}
		}

		// Show resolved state
		fmt.Fprintf(w, "\nResolved thresholds:\n")
		resolved := cfg.Resolve()
		for _, t := range resolved {
			fmt.Fprintf(w, "  tenant=%s metric=%s value=%.0f severity=%s component=%s\n",
				t.Tenant, t.Metric, t.Value, t.Severity, t.Component)
		}
	}
}
