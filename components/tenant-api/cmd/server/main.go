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
	"log/slog"
	"net/http"
	"os"
	"os/signal"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/views"
	"github.com/vencil/tenant-api/internal/ws"
)

// configureLogger wires slog's default to a JSON handler on stderr.
// Called before any other startup work so even early-init failures
// emit structured lines. Verbosity controlled by TA_LOG_LEVEL
// (debug / info / warn / error; default info).
func configureLogger() {
	level := slog.LevelInfo
	switch os.Getenv("TA_LOG_LEVEL") {
	case "debug":
		level = slog.LevelDebug
	case "warn":
		level = slog.LevelWarn
	case "error":
		level = slog.LevelError
	}
	h := slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{Level: level})
	slog.SetDefault(slog.New(h))
	// Bridge the legacy `log` package so any remaining log.Printf
	// (including from third-party libs) goes through slog at INFO.
	// log.Fatalf is preserved as a separate path — startup-fatal
	// errors should keep using log to retain the immediate exit
	// behavior; their messages still land on stderr.
	log.SetFlags(0)
	log.SetOutput(slogLogWriter{})
}

// slogLogWriter implements io.Writer by forwarding each Write to
// slog.Default().Info, stripping the trailing newline so the
// JSON `msg` field doesn't carry "\n". Used to bridge stdlib log
// callers (chi internals, log.Fatalf at startup) to the structured
// pipeline.
type slogLogWriter struct{}

func (slogLogWriter) Write(p []byte) (int, error) {
	msg := string(p)
	for len(msg) > 0 && (msg[len(msg)-1] == '\n' || msg[len(msg)-1] == '\r') {
		msg = msg[:len(msg)-1]
	}
	slog.Info(msg)
	return len(p), nil
}

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

	// v2.8.0 Phase B Track C (B-6 hardening): per-caller rate limit.
	// Numeric requests-per-minute; "0" disables; default 100.
	rateLimitPerMin := flag.String("rate-limit-per-min", envOrDefault("TA_RATE_LIMIT_PER_MIN", ""),
		"Per-caller rate limit (requests / 60s rolling window). 0 disables. Default 100.")

	// PR-11/11: HTTP server timeouts. Pre-PR-11 these were
	// hard-coded (15s read / 30s write / 60s idle). Some long-tail
	// operations (PR-mode batch with N tenants × WritePR commit
	// latency) brushed up against the 30s write deadline; making
	// these tunable lets operators raise WriteTimeout for
	// deployments that need it without rebuilding.
	readTimeout := flag.Duration("read-timeout",
		parseDurationOrDefault(os.Getenv("TA_READ_TIMEOUT"), 15*time.Second),
		"HTTP server read timeout (default 15s; TA_READ_TIMEOUT)")
	writeTimeout := flag.Duration("write-timeout",
		parseDurationOrDefault(os.Getenv("TA_WRITE_TIMEOUT"), 30*time.Second),
		"HTTP server write timeout (default 30s; TA_WRITE_TIMEOUT)")
	idleTimeout := flag.Duration("idle-timeout",
		parseDurationOrDefault(os.Getenv("TA_IDLE_TIMEOUT"), 60*time.Second),
		"HTTP server idle timeout (default 60s; TA_IDLE_TIMEOUT)")
	flag.Parse()

	// PR-10/11: structured (JSON) logging via slog. Configure before
	// any Manager / Writer / Tracker construction so their initial-
	// load logs go through the same pipeline.
	configureLogger()

	slog.Info("tenant-api starting", "config_dir", *configDir, "addr", *listenAddr)

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

	// v2.6.0: PR-based write-back mode (ADR-011) — supports GitHub + GitLab.
	// PR-5/11: bootstrap logic lives in wire.go::wirePRBackend so main()
	// shows the wiring shape without 50 lines of switch-case noise.
	prClient, prTracker, wm := wirePRBackend(prBackendFlags{
		Mode:           *writeMode,
		GitHubRepo:     *ghRepo,
		GitHubBase:     *ghBaseBranch,
		GitLabProject:  *glProject,
		GitLabBranch:   *glTargetBranch,
		ReloadInterval: *reloadInterval,
	})

	// Wire all handler dependencies into a single struct (PR-4/11).
	// Every handler is now a method on *deps; pass-through positional
	// args are gone.
	deps := &handler.Deps{
		ConfigDir:   *configDir,
		Writer:      writer,
		RBAC:        rbacMgr,
		Policy:      policyMgr,
		Groups:      groupMgr,
		Views:       viewMgr,
		Tasks:       taskMgr,
		PRClient:    prClient,
		PRTracker:   prTracker,
		WriteMode:   wm,
		SearchCache: handler.NewTenantSnapshotCache(),
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
	r.Use(handler.RequestIDResponse) // v2.8.0 B-6 PR-1: echo X-Request-ID
	r.Use(middleware.RealIP)
	r.Use(handler.SlogRequestLogger) // PR-10/11: structured JSON request log w/ request_id
	r.Use(middleware.Recoverer)
	r.Use(middleware.Timeout(30 * time.Second))
	r.Use(handler.MetricsMiddleware)

	// v2.8.0 B-6 PR-1: per-caller rate limiter. Mounted AFTER
	// the chi standard chain so the limiter sees the caller
	// identity (X-Forwarded-Email, populated by oauth2-proxy
	// upstream of tenant-api).
	rlCfg, rlMalformed := handler.RateLimitConfigFromEnv(*rateLimitPerMin)
	if rlMalformed {
		slog.Warn("rate limit env malformed, falling back to default",
			"env_value", *rateLimitPerMin,
			"default_per_min", rlCfg.RequestsPerMinute)
	}
	if rlCfg.RequestsPerMinute > 0 {
		slog.Info("rate limiter enabled", "per_min_per_caller", rlCfg.RequestsPerMinute)
	} else {
		slog.Info("rate limiter disabled", "hint", "set TA_RATE_LIMIT_PER_MIN > 0 to enable")
	}
	// PR-11/11: stopCh terminates the limiter's bucket-sweeper
	// goroutine alongside the rbac/policy/tracker WatchLoops.
	r.Use(handler.RateLimit(rlCfg, stopCh))

	// Health / readiness / metrics (no auth)
	r.Get("/health", handler.Health)
	r.Get("/ready", deps.Ready())
	r.Get("/metrics", handler.MetricsHandler)

	// API v1 — all routes require identity headers (injected by oauth2-proxy)
	r.Route("/api/v1", func(r chi.Router) {
		// Identity endpoint (no specific permission required, just authenticated)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/me", deps.Me())

		// Tenant list (read permission, no specific tenant ID)
		// v2.5.0: RBAC-filtered — only returns tenants the user can access
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/tenants", deps.ListTenants())

		// v2.8.0 Phase .c C-1: server-side search / filter / pagination.
		// Snapshot cache (30s TTL) shared across requests via Deps.SearchCache.
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/tenants/search", deps.SearchTenants())

		// Per-tenant routes
		r.Route("/tenants/{id}", func(r chi.Router) {
			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Get("/", deps.GetTenant())

			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Post("/diff", deps.DiffTenant())

			r.With(rbacMgr.Middleware(rbac.PermWrite, handler.TenantIDFromPath)).
				Put("/", deps.PutTenant())

			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Post("/validate", deps.ValidateTenant())

			// v2.7.0 B-3 (ADR-017/018): merged effective config + dual hashes.
			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Get("/effective", deps.GetTenantEffective())
		})

		// Batch operations — route-level middleware checks read (authenticated),
		// per-tenant write permission is enforced inside the handler.
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Post("/tenants/batch", deps.BatchTenants())

		// Group management (v2.5.0) — RBAC-filtered list.
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/groups", deps.ListGroups())

		r.Route("/groups/{id}", func(r chi.Router) {
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Get("/", deps.GetGroup())

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Put("/", deps.PutGroup())

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Delete("/", deps.DeleteGroup())

			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Post("/batch", deps.GroupBatch())
		})

		// Saved Views (v2.5.0 Phase C)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/views", deps.ListViews())

		r.Route("/views/{id}", func(r chi.Router) {
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Get("/", deps.GetView())

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Put("/", deps.PutView())

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Delete("/", deps.DeleteView())
		})

		// Task polling (v2.6.0 — async batch operations)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/tasks/{id}", deps.GetTask())

		// PR/MR tracking (v2.6.0 Phase C — ADR-011 PR-based write-back)
		// Works for both GitHub PRs and GitLab MRs via platform.Tracker
		if prTracker != nil {
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Get("/prs", deps.ListPRs())
		}

		// Real-time event stream (v2.6.0 — SSE for config change notifications)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/events", eventHub.ServeHTTP)
	})

	// ── HTTP server with graceful shutdown ────────────────────────────────────
	// PR-11/11: timeouts now configurable via TA_{READ,WRITE,IDLE}_TIMEOUT.
	srv := &http.Server{
		Addr:         *listenAddr,
		Handler:      r,
		ReadTimeout:  *readTimeout,
		WriteTimeout: *writeTimeout,
		IdleTimeout:  *idleTimeout,
	}
	slog.Info("http server timeouts",
		"read", *readTimeout, "write", *writeTimeout, "idle", *idleTimeout)

	// Graceful shutdown on SIGTERM / SIGINT
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGTERM, syscall.SIGINT)

	go func() {
		slog.Info("tenant-api listening", "addr", *listenAddr)
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("FATAL: listen: %v", err)
		}
	}()

	<-quit
	slog.Info("tenant-api shutting down")

	taskMgr.Close()
	close(stopCh)

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		slog.Warn("shutdown error", "error", err)
	}
	slog.Info("tenant-api stopped")
}

// parseDurationOrDefault parses a Go duration string ("30s", "1m") and
// returns def on empty input or parse error. Errors are logged at WARN
// so misconfigured TA_*_TIMEOUT env vars don't silently fall back —
// the operator sees the rejected value and the chosen default.
func parseDurationOrDefault(s string, def time.Duration) time.Duration {
	if s == "" {
		return def
	}
	d, err := time.ParseDuration(s)
	if err != nil {
		slog.Warn("invalid duration env value, using default",
			"value", s, "default", def, "error", err)
		return def
	}
	return d
}

// envOrDefault returns the environment variable value or the default if unset.
func envOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}
