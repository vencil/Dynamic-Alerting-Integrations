package main

// Boot wiring for the threshold-exporter binary. Three concerns,
// one per helper:
//
//   - loadEnvOverrides — apply CONFIG_PATH / CONFIG_DIR / LISTEN_ADDR
//                        env vars on top of flag.Parse()
//   - buildServer      — assemble the http.Server with hardened
//                        timeouts (Gosec G112 ReadHeaderTimeout)
//   - runUntilSignal   — block on SIGINT/SIGTERM, then close the
//                        watch loop, debounce timer, and graceful-
//                        shutdown the HTTP server with a 15s budget
//
// HTTP handlers live in handlers.go (read-only API) and
// handler_simulate.go (POST /api/v1/tenants/simulate). resolveConfigPath
// stays here because main_test.go binds to it directly via global
// configDir / configPath.

import (
	"context"
	"flag"
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

// shutdownTimeout caps how long the graceful HTTP shutdown will wait
// for in-flight requests to drain. Long enough for a /metrics scrape
// (typically <1s) to finish; short enough that a misbehaving client
// can't keep the Pod alive past the K8s terminationGracePeriodSeconds.
const shutdownTimeout = 15 * time.Second

func main() {
	flag.Parse()
	loadEnvOverrides()

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

	server := buildServer(listenAddr, buildMux(manager, collector))
	runUntilSignal(server, stopCh, manager)
}

// loadEnvOverrides applies env var overrides on top of flag values.
// Env wins over flag when set — convenient for K8s Pod templates that
// pass config via env without rebuilding the command line.
func loadEnvOverrides() {
	if v := os.Getenv("CONFIG_PATH"); v != "" {
		configPath = v
	}
	if v := os.Getenv("CONFIG_DIR"); v != "" {
		configDir = v
	}
	if v := os.Getenv("LISTEN_ADDR"); v != "" {
		listenAddr = v
	}
}

// buildMux wires the routing table for the read-only API + simulate
// primitive + Prometheus scrape endpoint. Kept separate from
// buildServer so tests can construct a mux without binding a port.
func buildMux(manager *ConfigManager, collector *ThresholdCollector) *http.ServeMux {
	mux := http.NewServeMux()
	mux.Handle("/metrics", collector.MetricsHandler())
	mux.HandleFunc("/health", healthHandler)
	mux.HandleFunc("/ready", readyHandler(manager))
	mux.HandleFunc("/api/v1/config", configViewHandler(manager))
	// v2.8.0 Phase .c C-7b: ephemeral simulate primitive. Stateless —
	// no shared writer to manager, no disk IO; safe to colocate with
	// the rest of the read-only API.
	mux.HandleFunc("/api/v1/tenants/simulate", simulateHandler())
	return mux
}

// buildServer constructs an http.Server with hardened defaults:
//   - ReadHeaderTimeout (Gosec G112) closes Slowloris-style attacks
//   - MaxHeaderBytes 8 KiB caps header memory per connection
//   - Read/Write/Idle timeouts pinned to values that work for our
//     workload (Prometheus scrape, manual debug, simulate POST)
func buildServer(addr string, handler http.Handler) *http.Server {
	return &http.Server{
		Addr:              addr,
		Handler:           handler,
		ReadTimeout:       5 * time.Second,
		ReadHeaderTimeout: 3 * time.Second,
		WriteTimeout:      10 * time.Second,
		IdleTimeout:       30 * time.Second,
		MaxHeaderBytes:    8192,
	}
}

// runUntilSignal blocks on SIGINT / SIGTERM, then runs an ordered
// shutdown:
//
//  1. close stopCh — terminates manager.WatchLoop goroutine
//  2. manager.Close — releases the debounce timer (v2.7.0 Phase 3,
//     §8.11.2 trap #12; safe no-op when debounceTimer is nil)
//  3. server.Shutdown — drains in-flight HTTP requests up to
//     shutdownTimeout, then force-closes
//
// Order matters: close stopCh first so WatchLoop doesn't race with
// manager.Close releasing the timer it might still arm.
func runUntilSignal(server *http.Server, stopCh chan struct{}, manager *ConfigManager) {
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		log.Printf("Listening on %s", server.Addr)
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
	ctx, cancel := context.WithTimeout(context.Background(), shutdownTimeout)
	defer cancel()
	if err := server.Shutdown(ctx); err != nil {
		log.Printf("HTTP server shutdown error: %v", err)
	}
	log.Println("Server stopped")
}

// resolveConfigPath determines the config path based on flags and auto-detection.
// Priority: -config-dir > -config > auto-detect default paths.
//
// Reads package-level configDir / configPath globals — pinned by
// main_test.go's resolveConfigPath unit tests, do not change shape.
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
