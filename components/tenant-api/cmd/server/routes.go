package main

// Router construction (ADR-027 / LD-6 P4b §5b).
//
// Extracted verbatim from main() — wire.go::wirePRBackend precedent: main()
// shows the wiring shape (flag-parse → managers → middleware config →
// buildRouter → servers) while the full route table lives here, where the
// route-manifest test (routes_test.go) can build it with stubbed conditional
// dependencies and chi.Walk every write-method route against the
// hand-maintained org-gate manifest. ZERO behavior change: middleware order,
// route patterns, and the two conditional registrations (PR tracking,
// federation tokens) are byte-identical to the pre-P4b inline block.

import (
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/handler/federation"
	"github.com/vencil/tenant-api/internal/rbac"
)

// routerDeps bundles everything buildRouter needs beyond the handler Deps
// container — mirroring prBackendFlags so the call site in main.go stays a
// single readable literal.
type routerDeps struct {
	Deps *handler.Deps // handler dependency container (also drives the
	// conditional registrations: Deps.PRTracker / Deps.Federation)
	RBAC   *rbac.Manager    // route-level RBAC middleware factory
	Events http.HandlerFunc // SSE hub endpoint (eventHub.ServeHTTP)

	// dev-auth-bypass (ADR-022, Layer 1 = default off).
	DevBypass       bool
	DevBypassEmail  string
	DevBypassGroups string

	// RateLimit is the per-caller rate-limit middleware, built in main()
	// (handler.RateLimit) because its sweeper goroutine is tied to the
	// server-lifetime stopCh.
	RateLimit func(http.Handler) http.Handler
}

// buildRouter assembles the chi router: the standard middleware chain and
// every tenant-api route. Conditional routes key off rd.Deps — PR tracking
// (/prs) registers only with a Tracker, federation token routes only with a
// token signer — exactly as main() previously keyed off the local variables
// those Deps fields are wired from.
func buildRouter(rd routerDeps) *chi.Mux {
	deps := rd.Deps
	rbacMgr := rd.RBAC

	r := chi.NewRouter()
	r.Use(middleware.RequestID)
	r.Use(handler.RequestIDResponse) // v2.8.0 B-6 PR-1: echo X-Request-ID
	// ADR-027: middleware.RealIP is deliberately NOT used. It unconditionally
	// overwrites r.RemoteAddr from the client-supplied X-Forwarded-For /
	// X-Real-IP headers, so anything keyed on RemoteAddr (rate limiting,
	// audit logs) would be forgeable by any caller. tenant-api sits behind a
	// same-pod oauth2-proxy (localhost) and a network 8080 port with no
	// trusted L7 proxy in front, so there is no legitimate reason to trust
	// those headers for the peer address; keep the true TCP peer.
	r.Use(handler.SlogRequestLogger) // PR-10/11: structured JSON request log w/ request_id
	r.Use(middleware.Recoverer)
	r.Use(middleware.Timeout(30 * time.Second))
	r.Use(handler.MetricsMiddleware)

	// dev-auth-bypass (ADR-022): mounted only when --dev-bypass-auth is set
	// (Layer 1 = default off). Placed before the rate limiter so the injected
	// X-Forwarded-Email is the bucketing key; downstream RBAC enforces normally.
	if rd.DevBypass {
		r.Use(rbac.DevBypassMiddleware(rd.DevBypassEmail, rd.DevBypassGroups))
	}

	// v2.8.0 B-6 PR-1: per-caller rate limiter. Mounted AFTER the chi
	// standard chain so the limiter sees the caller identity
	// (X-Forwarded-Email, populated by oauth2-proxy upstream of tenant-api).
	r.Use(rd.RateLimit)

	// Health / readiness / metrics (no auth)
	r.Get("/health", handler.Health)
	r.Get("/ready", handler.Ready(deps))
	r.Get("/metrics", handler.MetricsHandler)

	// API v1 — all routes require identity headers (injected by oauth2-proxy)
	r.Route("/api/v1", func(r chi.Router) {
		// Identity endpoint (no specific permission required, just authenticated)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/me", handler.Me(deps))

		// Tenant list (read permission, no specific tenant ID)
		// v2.5.0: RBAC-filtered — only returns tenants the user can access
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/tenants", handler.ListTenants(deps))

		// v2.8.0 Phase .c C-1: server-side search / filter / pagination.
		// Snapshot cache (30s TTL) shared across requests via Deps.SearchCache.
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/tenants/search", handler.SearchTenants(deps))

		// Per-tenant routes
		r.Route("/tenants/{id}", func(r chi.Router) {
			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Get("/", handler.GetTenant(deps))

			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Post("/diff", handler.DiffTenant(deps))

			r.With(rbacMgr.Middleware(rbac.PermWrite, handler.TenantIDFromPath)).
				Put("/", handler.PutTenant(deps))

			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Post("/validate", handler.ValidateTenant(deps))

			// v2.7.0 B-3 (ADR-016/017): merged effective config + dual hashes.
			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Get("/effective", handler.GetTenantEffective(deps))

			// v2.9.0 ADR-024 §S6 (#741): metric discovery catalog for the
			// portal recipe-authoring UX. Read-only Prometheus proxy;
			// route middleware enforces RBAC read on {id}.
			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Get("/metrics", handler.DiscoverMetrics(deps))

			// #657: lightweight RBAC read-probe for sibling services (the
			// recipe-preview would-fire service). 200 {allow:true} if the
			// caller may read {id}, 403 otherwise — reuses this exact read
			// middleware so the tenant-isolation decision is never
			// re-implemented elsewhere, and returns only the boolean (not the
			// tenant config). See §4.1 of the recipe-preview design.
			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Get("/access", handler.CheckTenantAccess())

			// v2.9.0 ADR-024 §S6b-2 (#741): comment-preserving write of a
			// tenant's _custom_alerts (RecipeBuilder modal). PermWrite —
			// this commits to GitOps.
			r.With(rbacMgr.Middleware(rbac.PermWrite, handler.TenantIDFromPath)).
				Put("/custom-alerts", handler.PutTenantCustomAlerts(deps))

			// Federation metric subset (v2.9.0 — ADR-020 IV-2e). PUT's
			// tenant-admin check is inside the handler (route middleware
			// only confirms authentication + read on the tenant).
			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Get("/federation", federation.GetTenantFederation(deps))
			r.With(rbacMgr.Middleware(rbac.PermRead, handler.TenantIDFromPath)).
				Put("/federation", federation.PutTenantFederation(deps))
		})

		// Batch operations — route-level middleware checks read (authenticated),
		// per-tenant write permission is enforced inside the handler.
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Post("/tenants/batch", handler.BatchTenants(deps))

		// Group management (v2.5.0) — RBAC-filtered list.
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/groups", handler.ListGroups(deps))

		r.Route("/groups/{id}", func(r chi.Router) {
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Get("/", handler.GetGroup(deps))

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Put("/", handler.PutGroup(deps))

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Delete("/", handler.DeleteGroup(deps))

			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Post("/batch", handler.GroupBatch(deps))
		})

		// Saved Views (v2.5.0 Phase C)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/views", handler.ListViews(deps))

		r.Route("/views/{id}", func(r chi.Router) {
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Get("/", handler.GetView(deps))

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Put("/", handler.PutView(deps))

			r.With(rbacMgr.Middleware(rbac.PermWrite, nil)).
				Delete("/", handler.DeleteView(deps))
		})

		// Task polling (v2.6.0 — async batch operations)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/tasks/{id}", handler.GetTask(deps))

		// PR/MR tracking (v2.6.0 Phase C — ADR-011 PR-based write-back)
		// Works for both GitHub PRs and GitLab MRs via platform.Tracker
		if deps.PRTracker != nil {
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Get("/prs", handler.ListPRs(deps))
		}

		// Real-time event stream (v2.6.0 — SSE for config change notifications)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/events", rd.Events)

		// Federation 2-tier policy — platform whitelist (v2.9.0 —
		// ADR-020 IV-2e). Always registered: the policy is independent
		// of token signing. PUT's platform-admin check is in the
		// handler; route-level middleware only confirms authentication.
		r.Route("/federation/policy", func(r chi.Router) {
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Get("/", federation.GetFederationPolicy(deps))
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Put("/", federation.PutFederationPolicy(deps))
		})

		// Reverse access-report audit endpoint (ADR-027 / LD-6 P6). /audit is
		// a deliberate NEW top-level segment — NOT under /tenants/{id} — so it
		// does not inherit the read-by-id org middleware (an org-scoped reader
		// must never unlock the platform-wide access map). Route middleware
		// only confirms authentication (federation-policy precedent above);
		// the real bar — PlatformAdminNonOrgScoped, tightened vs the bare
		// platform-"*" admin check — lives at the top of the handler with a
		// constant 403 (no tenant-enumeration oracle).
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/audit/tenants/{id}/access-report", handler.GetTenantAccessReport(deps))

		// P7 what-if dry-run: same /audit segment, same authentication-only
		// route middleware, same in-handler bar with the byte-identical
		// constant 403. POST because it carries a candidate _rbac.yaml body —
		// it computes reports and commits NOTHING (see the write-route
		// manifest's gateWriteOpPlatformAdmin entry).
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Post("/audit/tenants/{id}/access-report/dry-run", handler.DryRunTenantAccessReport(deps))

		// Federation token endpoint (v2.9.0 — ADR-020 IV-2d).
		// Registered only when a signing key is configured. Route-level
		// middleware checks authentication; per-tenant admin permission
		// is enforced inside each handler because the tenant ID is in
		// the body / query / token record, not the URL path.
		if deps.Federation != nil {
			r.Route("/federation/tokens", func(r chi.Router) {
				r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
					Post("/", federation.CreateFederationToken(deps))
				r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
					Get("/", federation.ListFederationTokens(deps))
				r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
					Delete("/{id}", federation.DeleteFederationToken(deps))
			})

			// v2.10.0 ADR-021 (#609): one-shot AccountID backfill for the
			// existing fleet. Route middleware confirms authentication; the
			// handler enforces platform-admin (the whole-fleet bar).
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Post("/federation/accounts/backfill", federation.BackfillAccounts(deps))
		}
	})

	return r
}
