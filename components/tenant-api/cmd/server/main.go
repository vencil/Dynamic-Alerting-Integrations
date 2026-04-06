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
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/views"
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

	groupMgr := groups.NewManager(*configDir)

	// v2.5.0: Domain policy enforcement at API layer
	policyMgr := policy.NewManager(*configDir)

	// v2.5.0: Saved Views for tenant-manager UI
	viewMgr := views.NewManager(*configDir)

	// ── RBAC + policy hot-reload goroutines ───────────────────────────────────
	stopCh := make(chan struct{})
	go rbacMgr.WatchLoop(*reloadInterval, stopCh)
	go policyMgr.WatchLoop(*reloadInterval, stopCh)

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
		// Identity endpoint (no specific permission required, just authenticated)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/me", handler.Me(rbacMgr))

		// Tenant list (read permission, no specific tenant ID)
		// v2.5.0: RBAC-filtered — only returns tenants the user can access
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/tenants", handler.ListTenants(*configDir, rbacMgr))

		// Per-tenant routes
		r.Route("/tenants/{id}", func(r chi.Router) {
			// Read endpoints
			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Get("/", handler.GetTenant(*configDir))

			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Post("/diff", handler.DiffTenant(writer))

			// Write endpoints
			r.With(rbacMgr.Middleware(rbac.PermWrite, handler.TenantIDFromPath)).
				Put("/", handler.PutTenant(writer, policyMgr))

			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Post("/validate", handler.ValidateTenant(*configDir))
		})

		// Batch operations — route-level middleware checks read (authenticated),
		// per-tenant write permission is enforced inside the handler.
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Post("/tenants/batch", handler.BatchTenants(writer, *configDir, rbacMgr, policyMgr))

		// Group management (v2.5.0)
		// v2.5.0: RBAC-filtered — only returns groups with accessible members
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/groups", handler.ListGroups(groupMgr, rbacMgr))

		r.Route("/groups/{id}", func(r chi.Router) {
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Get("/", handler.GetGroup(groupMgr))

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Put("/", handler.PutGroup(groupMgr, writer))

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Delete("/", handler.DeleteGroup(groupMgr, writer))

			// Batch operations on group members
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Post("/batch", handler.GroupBatch(groupMgr, writer, *configDir, rbacMgr))
		})

		// Saved Views (v2.5.0 Phase C)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/views", handler.ListViews(viewMgr))

		r.Route("/views/{id}", func(r chi.Router) {
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Get("/", handler.GetView(viewMgr))

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Put("/", handler.PutView(viewMgr, writer))

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Delete("/", handler.DeleteView(viewMgr, writer))
		})
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