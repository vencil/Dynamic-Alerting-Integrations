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
	"github.com/vencil/tenant-api/internal/async"
	gh "github.com/vencil/tenant-api/internal/github"
	gl "github.com/vencil/tenant-api/internal/gitlab"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/views"
	"github.com/vencil/tenant-api/internal/ws"
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

	// v2.6.0: PR-based write-back mode (ADR-011)
	// Supports: "direct" (default), "pr" or "pr-github" (GitHub PRs), "pr-gitlab" (GitLab MRs)
	writeMode := flag.String("write-mode", envOrDefault("TA_WRITE_MODE", "direct"),
		"Write-back mode: 'direct' (commit-on-write), 'pr' or 'pr-github' (GitHub PR), 'pr-gitlab' (GitLab MR)")

	// GitHub flags
	ghRepo := flag.String("github-repo", envOrDefault("TA_GITHUB_REPO", ""),
		"GitHub repository in owner/repo format (required for pr/pr-github mode)")
	ghBaseBranch := flag.String("github-base-branch", envOrDefault("TA_GITHUB_BASE_BRANCH", "main"),
		"Target branch for GitHub PRs (default: main)")

	// v2.6.0 Phase E: GitLab flags
	glProject := flag.String("gitlab-project", envOrDefault("TA_GITLAB_PROJECT", ""),
		"GitLab project path (group/project) or numeric ID (required for pr-gitlab mode)")
	glTargetBranch := flag.String("gitlab-target-branch", envOrDefault("TA_GITLAB_TARGET_BRANCH", "main"),
		"Target branch for GitLab MRs (default: main)")
	flag.Parse()

	log.Printf("tenant-api starting — config-dir=%s addr=%s", *configDir, *listenAddr)

	// ── Dependencies ──────────────────────────────────────────────────────────
	rbacMgr, err := rbac.NewManager(*rbacPath)
	if err != nil {
		log.Fatalf("FATAL: rbac init: %v", err)
	}

	// v2.6.0: WebSocket/SSE hub for real-time config change notifications
	eventHub := ws.NewHub()

	writer := gitops.NewWriter(*configDir, *gitDir)
	// v2.6.0: Register callback for real-time event broadcasting
	writer.SetOnWrite(func(tenantID string) {
		eventHub.Broadcast(ws.Event{
			Type:      "config_change",
			TenantID:  tenantID,
			Timestamp: time.Now(),
			Detail:    "tenant config updated",
		})
	})

	groupMgr := groups.NewManager(*configDir)

	// v2.5.0: Domain policy enforcement at API layer
	policyMgr := policy.NewManager(*configDir)

	// v2.5.0: Saved Views for tenant-manager UI
	viewMgr := views.NewManager(*configDir)

	// v2.6.0: Async task manager for batch operations
	taskMgr := async.NewManager(4) // 4 worker goroutines

	// v2.6.0: PR-based write-back mode (ADR-011) — supports GitHub + GitLab
	wm := handler.WriteMode(*writeMode)
	var prClient platform.Client
	var prTracker platform.Tracker

	switch wm {
	case handler.WriteModePR, handler.WriteModePRGitHub:
		// GitHub PR mode
		wm = handler.WriteModePR // normalize
		ghToken := os.Getenv("TA_GITHUB_TOKEN")
		if ghToken == "" {
			log.Fatalf("FATAL: TA_GITHUB_TOKEN is required when write-mode=pr/pr-github")
		}
		if *ghRepo == "" {
			log.Fatalf("FATAL: --github-repo (or TA_GITHUB_REPO) is required when write-mode=pr/pr-github")
		}
		ghClient, err := gh.NewClient(ghToken, *ghRepo, *ghBaseBranch)
		if err != nil {
			log.Fatalf("FATAL: github client: %v", err)
		}
		if gheURL := os.Getenv("TA_GITHUB_API_URL"); gheURL != "" {
			ghClient.SetBaseURL(gheURL)
		}
		if err := ghClient.ValidateToken(); err != nil {
			log.Printf("WARN: GitHub token validation failed: %v (PR operations may fail)", err)
		}
		ghTracker := gh.NewTracker(ghClient, *reloadInterval)
		prClient = ghClient
		prTracker = ghTracker
		log.Printf("tenant-api: GitHub PR write-back mode enabled (repo=%s, base=%s)", *ghRepo, *ghBaseBranch)

	case handler.WriteModePRGitLab:
		// GitLab MR mode
		glToken := os.Getenv("TA_GITLAB_TOKEN")
		if glToken == "" {
			log.Fatalf("FATAL: TA_GITLAB_TOKEN is required when write-mode=pr-gitlab")
		}
		if *glProject == "" {
			log.Fatalf("FATAL: --gitlab-project (or TA_GITLAB_PROJECT) is required when write-mode=pr-gitlab")
		}
		glClient, err := gl.NewClient(glToken, *glProject, *glTargetBranch)
		if err != nil {
			log.Fatalf("FATAL: gitlab client: %v", err)
		}
		if glURL := os.Getenv("TA_GITLAB_API_URL"); glURL != "" {
			glClient.SetBaseURL(glURL)
		}
		if err := glClient.ValidateToken(); err != nil {
			log.Printf("WARN: GitLab token validation failed: %v (MR operations may fail)", err)
		}
		glTracker := gl.NewTracker(glClient, *reloadInterval)
		prClient = glClient
		prTracker = glTracker
		log.Printf("tenant-api: GitLab MR write-back mode enabled (project=%s, target=%s)", *glProject, *glTargetBranch)

	default:
		log.Printf("tenant-api: direct write mode (commit-on-write)")
	}

	// ── RBAC + policy hot-reload goroutines ───────────────────────────────────
	stopCh := make(chan struct{})
	go rbacMgr.WatchLoop(*reloadInterval, stopCh)
	go policyMgr.WatchLoop(*reloadInterval, stopCh)
	if prTracker != nil {
		go prTracker.WatchLoop(stopCh)
	}

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
				Put("/", handler.PutTenant(writer, policyMgr, wm, prClient, prTracker))

			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Post("/validate", handler.ValidateTenant(*configDir))

			// v2.7.0 B-3 (ADR-017/018): merged effective config + dual hashes.
			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Get("/effective", handler.GetTenantEffective(*configDir))
		})

		// Batch operations — route-level middleware checks read (authenticated),
		// per-tenant write permission is enforced inside the handler.
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Post("/tenants/batch", handler.BatchTenants(writer, *configDir, rbacMgr, policyMgr, taskMgr, wm, prClient, prTracker))

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
				Post("/batch", handler.GroupBatch(groupMgr, writer, *configDir, rbacMgr, taskMgr))
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

		// Task polling (v2.6.0 — async batch operations)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/tasks/{id}", handler.GetTask(taskMgr))

		// PR/MR tracking (v2.6.0 Phase C — ADR-011 PR-based write-back)
		// Works for both GitHub PRs and GitLab MRs via platform.Tracker
		if prTracker != nil {
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Get("/prs", handler.ListPRs(prTracker))
		}

		// Real-time event stream (v2.6.0 — SSE for config change notifications)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/events", eventHub.ServeHTTP)
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

	taskMgr.Close()
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
