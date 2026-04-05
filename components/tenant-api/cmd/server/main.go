// tenant-api: Tenant Management API server for the Dynamic Alerting Platform.
//
// Architecture: ADR-009 — commit-on-write GitOps + oauth2-proxy sidecar auth.
//
// Usage:
//
//	tenant-api --config-dir /conf.d --port 8080 --rbac /etc/rbac/_rbac.yaml
package main

import (
	"context"
	"flag"
	"log"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
)

func main() {
	// ── Flags ──────────────────────────────────────────────────────────────────
	configDir := flag.String("config-dir", envOrDefault("TA_CONFIG_DIR", "/conf.d"),
		"Path to conf.d/ directory containing tenant YAML files")
	gitDir := flag.String("git-dir", envOrDefault("TA_GIT_DIR", ""),
		"Git repository root for commit-on-write (defaults to config-dir)")
	rbacPath := flag.String("rbac", envOrDefault("TA_RBAC_PATH", ""),
		"Path to _rbac.yaml (leave empty for open-read mode)")
	listenAddr := flag.String("addr", envOrDefault("TA_ADDR", ":8080"),
		"HTTP listen address")
	reloadInterval := flag.Duration("reload-interval", 30*time.Second,
		"How often to check for RBAC config changes")
	flag.Parse()

	log.Printf("tenant-api starting — config-dir=%s addr=%s", *configDir, *listenAddr)

	// ── Dependencies ──────────────────────────────────────────────────────────
	rbacMgr, err := rbac.NewManager(*rbacPath)
	if err != nil {
		log.Fatalf("FATAL: rbac init: %v", err)
	}

	writer := gitops.NewWriter(*configDir, *gitDir)

	// ── RBAC hot-reload goroutine ─────────────────────────────────────────────
	stopCh := make(chan struct{})
	go rbacMgr.WatchLoop(*reloadInterval, stopCh)

	// ── Router ────────────────────────────────────────────────────────────────
	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(middleware.RealIP)
	r.Use(middleware.Logger)
	r.Use(middleware.Recoverer)
	r.Use(middleware.Timeout(30 * time.Second))
	r.Use(handler.MetricsMiddleware)

	// Health / readiness / metrics (no auth)
	r.Get("/health", handler.Health)
	r.Get("/ready", handler.Ready(*configDir))
	r.Get("/metrics", handler.MetricsHandler)

	// API v1 — all routes require identity headers (injected by oauth2-proxy)
	r.Route("/api/v1", func(r chi.Router) {
		// Tenant list (read permission, no specific tenant ID)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/tenants", handler.ListTenants(*configDir))

		// Per-tenant routes
		r.Route("/tenants/{id}", func(r chi.Router) {
			// Read endpoints
			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Get("/", handler.GetTenant(*configDir))

			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Post("/diff", handler.DiffTenant(writer))

			// Write endpoints
			r.With(rbacMgr.Middleware(rbac.PermWrite, handler.TenantIDFromPath)).
				Put("/", handler.PutTenant(writer))

			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Post("/validate", handler.ValidateTenant(*configDir))
		})

		// Batch operations — route-level middleware checks read (authenticated),
		// per-tenant write permission is enforced inside the handler.
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Post("/tenants/batch", handler.BatchTenants(writer, *configDir, rbacMgr))
	})

	// ── HTTP server with graceful shutdown ────────────────────────────────────
	srv := &http.Server{
		Addr:         *listenAddr,
		Handler:      r,
		ReadTimeout:  15 * time.Second,
		WriteTimeout: 30 * time.Second,
		IdleTimeout:  60 * time.Second,
	}

	// Graceful shutdown on SIGTERM / SIGINT
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGTERM, syscall.SIGINT)

	go func() {
		log.Printf("tenant-api listening on %s", *listenAddr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("FATAL: listen: %v", err)
		}
	}()

	<-quit
	log.Println("tenant-api shutting down...")

	close(stopCh)

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Printf("WARN: shutdown error: %v", err)
	}
	log.Println("tenant-api stopped")
}

// envOrDefault returns the environment variable value or the default if unset.
func envOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
