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
	Events http.HandlerFunc // SSE endpoint (handler.EventStream: hub filtered per subscriber, #3123)

	// dev-auth-bypass (ADR-022, Layer 1 = default off).
	DevBypass       bool
	DevBypassEmail  string
	DevBypassGroups string

	// RateLimit is the per-caller rate-limit middleware, built in main()
	// (handler.RateLimit) because its sweeper goroutine is tied to the
	// server-lifetime stopCh.
	RateLimit func(http.Handler) http.Handler

	// RequestTimeout bounds every request except the SSE stream (see
	// timeoutExceptStream). Zero means defaultRequestTimeout; tests inject a
	// short one.
	RequestTimeout time.Duration
}

// defaultRequestTimeout is the per-request deadline (chi middleware.Timeout).
const defaultRequestTimeout = 30 * time.Second

// eventsPath is the SSE stream, the one long-lived route. Registered below as
// apiV1Prefix + eventsRoute; timeoutExceptStream exempts exactly that path.
const (
	apiV1Prefix = "/api/v1"
	eventsRoute = "/events"
	eventsPath  = apiV1Prefix + eventsRoute
)

// timeoutExceptStream is chi's middleware.Timeout for every request except
// GET eventsPath. Under the timeout a stream is cut at the deadline (its
// context is cancelled, the handler returns, and chi then writes a 504 onto
// the open stream). The stream is bounded instead by the hub: a heartbeat and
// a per-write deadline drop a dead client, and an optional max lifetime ends
// any connection. It sits where middleware.Timeout sat, so every other request
// keeps the same chain in the same order.
//
// The path is compared the way chi routes it (RawPath when set, else Path), so
// the exemption applies exactly to the requests chi hands the events handler.
func timeoutExceptStream(d time.Duration) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		timed := middleware.Timeout(d)(next)
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			path := r.URL.RawPath
			if path == "" {
				path = r.URL.Path
			}
			if r.Method == http.MethodGet && path == eventsPath {
				next.ServeHTTP(w, r)
				return
			}
			timed.ServeHTTP(w, r)
		})
	}
}

// buildRouter assembles the chi router: the standard middleware chain and
// every tenant-api route. Conditional routes key off rd.Deps — PR tracking
// (/prs) registers only with a Tracker, federation token routes only with a
// token signer — exactly as main() previously keyed off the local variables
// those Deps fields are wired from.
func buildRouter(rd routerDeps) *chi.Mux {
	deps := rd.Deps
	rbacMgr := rd.RBAC

	// The two gates for routes whose URL names no tenant (#2520):
	//
	//   - anyTenantRead / anyTenantWrite (MiddlewareAnyTenant): the caller holds
	//     the permission on at least one tenant (rbac.GrantsSomeTenant). For
	//     routes whose HANDLER decides visibility per tenant — every one of
	//     them is listed with its filter in readRouteManifest /
	//     writeRouteManifest (routes_test.go). A caller granted only ["db-a"]
	//     or ["db-*"] passes and sees its own tenants; before #2520 these
	//     routes demanded a literal "*" rule.
	//   - platformRead / platformWrite (Middleware(perm, nil)): the permission
	//     on the platform wildcard "*", i.e. a rule listing "*". For routes
	//     whose handler bar is a NON-org-scoped "*" rule
	//     (rbac.PlatformNonOrgScoped): saved views — read for GET (open mode
	//     reads too), write for PUT/DELETE — and, at admin level
	//     (PlatformAdminNonOrgScoped), federation policy PUT, account backfill
	//     and the /audit surface (#3160) — there the any-tenant gate would
	//     admit no caller the handler does not then refuse, so they keep the
	//     narrower gate. The route gate is only a coarse first layer: an
	//     org-scoped "*" rule passes it and is refused by the handler bar.
	//
	// Both keep MED-8: a configured _rbac.yaml with zero groups denies all.
	anyTenantRead := rbacMgr.MiddlewareAnyTenant(rbac.PermRead)
	anyTenantWrite := rbacMgr.MiddlewareAnyTenant(rbac.PermWrite)
	platformRead := rbacMgr.Middleware(rbac.PermRead, nil)
	platformWrite := rbacMgr.Middleware(rbac.PermWrite, nil)

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
	timeout := rd.RequestTimeout
	if timeout <= 0 {
		timeout = defaultRequestTimeout
	}
	r.Use(timeoutExceptStream(timeout)) // every route but the SSE stream
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
	r.Route(apiV1Prefix, func(r chi.Router) {
		// Identity endpoint: the caller's own rules (RulesMatching) and
		// nothing else — authentication only, like OIDC userinfo (#2520; it
		// used to demand read on "*"). A caller with no grant gets 200 with
		// empty grants, so it can tell "not configured for me" from an error.
		r.With(rbacMgr.MiddlewareAuthenticated()).
			Get("/me", handler.Me(deps))

		// Tenant list (read permission, no specific tenant ID)
		// v2.5.0: RBAC-filtered — only returns tenants the user can access
		r.With(anyTenantRead).
			Get("/tenants", handler.ListTenants(deps))

		// v2.8.0 Phase .c C-1: server-side search / filter / pagination.
		// Snapshot cache shared with /tenants via Deps.SearchCache.
		r.With(anyTenantRead).
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

		// Batch operations — route-level middleware checks read on some tenant;
		// per-tenant write permission is enforced inside the handler.
		r.With(anyTenantRead).
			Post("/tenants/batch", handler.BatchTenants(deps))

		// Group management (v2.5.0) — RBAC-filtered list. Every group route
		// decides per member (groupVisible; PUT/DELETE also need write on every
		// member), so the write routes take the any-tenant WRITE gate (#2520).
		r.With(anyTenantRead).
			Get("/groups", handler.ListGroups(deps))

		r.Route("/groups/{id}", func(r chi.Router) {
			r.With(anyTenantRead).
				Get("/", handler.GetGroup(deps))

			r.With(anyTenantWrite).
				Put("/", handler.PutGroup(deps))

			r.With(anyTenantWrite).
				Delete("/", handler.DeleteGroup(deps))

			r.With(anyTenantRead).
				Post("/batch", handler.GroupBatch(deps))
		})

		// Saved Views (v2.5.0 Phase C). ⛔ Platform "*" gate on purpose (#2520):
		// a view has no owner, no org and no per-tenant filter — the handlers
		// return and overwrite every view as stored — so the any-tenant gate
		// would hand every tenant-scoped caller the whole set. The real bar is
		// in each handler (#3160): a "*" rule with NO org-scope
		// (rbac.PlatformNonOrgScoped) — read for GET, where open mode reads
		// too, write for PUT/DELETE. An org-scoped "*" rule passes this route
		// gate and is refused there, in enforce and shadow mode alike.
		r.With(platformRead).
			Get("/views", handler.ListViews(deps))

		r.Route("/views/{id}", func(r chi.Router) {
			r.With(platformRead).
				Get("/", handler.GetView(deps))

			r.With(platformWrite).
				Put("/", handler.PutView(deps))

			r.With(platformWrite).
				Delete("/", handler.DeleteView(deps))
		})

		// Task polling (v2.6.0 — async batch operations)
		r.With(anyTenantRead).
			Get("/tasks/{id}", handler.GetTask(deps))

		// PR/MR tracking (v2.6.0 Phase C — ADR-011 PR-based write-back)
		// Works for both GitHub PRs and GitLab MRs via platform.Tracker
		if deps.PRTracker != nil {
			r.With(anyTenantRead).
				Get("/prs", handler.ListPRs(deps))
		}

		// Real-time event stream (v2.6.0 — SSE for config change notifications).
		// The only route outside the request timeout (timeoutExceptStream).
		r.With(anyTenantRead).
			Get(eventsRoute, rd.Events)

		// Federation 2-tier policy — platform whitelist (v2.9.0 —
		// ADR-020 IV-2e). Always registered: the policy is independent
		// of token signing. GET is the metric whitelist a tenant admin needs
		// to choose its subset (PUT /tenants/{id}/federation) — platform
		// config naming no tenant — so any tenant reader may read it. PUT's
		// bar — a non-org-scoped platform admin (PlatformAdminNonOrgScoped,
		// #3160) — is in the handler; its route keeps the "*" gate, which
		// every such admin passes anyway.
		r.Route("/federation/policy", func(r chi.Router) {
			r.With(anyTenantRead).
				Get("/", federation.GetFederationPolicy(deps))
			r.With(platformRead).
				Put("/", federation.PutFederationPolicy(deps))
		})

		// Reverse access-report audit endpoint (ADR-027 / LD-6 P6). /audit is
		// a deliberate NEW top-level segment — NOT under /tenants/{id} — so it
		// does not inherit the read-by-id org middleware (an org-scoped reader
		// must never unlock the platform-wide access map). Route middleware
		// checks read on "*" (federation-policy precedent above; #2520 left it
		// there — the handler bar below implies it, so a wider gate would admit
		// no one); the real bar — PlatformAdminNonOrgScoped, tightened vs the bare
		// platform-"*" admin check (the federation PUT and backfill bars were
		// tightened to it too, #3160) — lives at the top of the handler with a
		// constant 403 (no tenant-enumeration oracle).
		r.With(platformRead).
			Get("/audit/tenants/{id}/access-report", handler.GetTenantAccessReport(deps))

		// P7 what-if dry-run: same /audit segment, same route middleware, same in-handler bar with the byte-identical
		// constant 403. POST because it carries a candidate _rbac.yaml body —
		// it computes reports and commits NOTHING (see the write-route
		// manifest's gateWriteOpPlatformAdmin entry).
		r.With(platformRead).
			Post("/audit/tenants/{id}/access-report/dry-run", handler.DryRunTenantAccessReport(deps))

		// Federation token endpoint (v2.9.0 — ADR-020 IV-2d).
		// Registered only when a signing key is configured. Route-level
		// middleware checks read on some tenant; per-tenant admin permission
		// is enforced inside each handler because the tenant ID is in
		// the body / query / token record, not the URL path.
		if deps.Federation != nil {
			r.Route("/federation/tokens", func(r chi.Router) {
				r.With(anyTenantRead).
					Post("/", federation.CreateFederationToken(deps))
				r.With(anyTenantRead).
					Get("/", federation.ListFederationTokens(deps))
				r.With(anyTenantRead).
					Delete("/{id}", federation.DeleteFederationToken(deps))
			})

			// v2.10.0 ADR-021 (#609): one-shot AccountID backfill for the
			// existing fleet. Route middleware checks read on "*"; the
			// handler enforces the whole-fleet bar, a non-org-scoped platform
			// admin (PlatformAdminNonOrgScoped, #3160).
			r.With(platformRead).
				Post("/federation/accounts/backfill", federation.BackfillAccounts(deps))
		}
	})

	return r
}
