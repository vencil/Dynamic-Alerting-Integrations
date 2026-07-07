// tenant-api: Tenant Management API server for the Dynamic Alerting Platform.
//
// Architecture: ADR-009 — commit-on-write GitOps + oauth2-proxy sidecar auth.
//
// Usage:
//
//	tenant-api --config-dir /conf.d --port 8080 --rbac /etc/rbac/_rbac.yaml
//
// @title          Tenant API
// @version        v2.8.0
// @description    Multi-tenant configuration management API for the Dynamic Alerting Platform.
// @description    Provides CRUD over per-tenant alert thresholds, RBAC-scoped writes, and async batch operations.
// @description    Auth is enforced by the oauth2-proxy sidecar and forwarded via X-Forwarded-Email / X-Forwarded-Groups.
// @license.name   See repository LICENSE
// @BasePath       /api/v1
package main

import (
	"context"
	"flag"
	"log"
	"log/slog"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/go-chi/chi/v5/middleware"
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/federation/orphan"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/handler/federation"
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
	rbacEmptyOpen := flag.Bool("rbac-empty-open", envBool("TA_RBAC_EMPTY_OPEN"),
		"MED-8 escape hatch: allow open-read when a --rbac path parses to zero groups (default false = fail closed)")
	rbacMetadataScopeEnforce := flag.Bool("rbac-metadata-scope-enforce", envBool("TA_RBAC_METADATA_SCOPE_ENFORCE"),
		"ADR-027/LD-6 P1: DENY an unlabeled tenant on an env/domain-restricted rule (fail-closed). Default false = shadow (still allow, but count tenant_api_scope_would_deny_total{axis=\"metadata\"}); flip only after that counter stops incrementing over the soak window (increase()==0 — it is a monotonic counter, not a gauge)")
	listenAddr := flag.String("addr", envOrDefault("TA_ADDR", ":8080"),
		"HTTP listen address")
	reloadInterval := flag.Duration("reload-interval", 30*time.Second,
		"How often to check for RBAC config changes")

	// LOCAL-DEV-ONLY auth bypass (ADR-022). Default off. When on, a dev
	// identity is injected for requests lacking an oauth2-proxy header so the
	// stack works without oauth2-proxy (try-local compose). RBAC still applies
	// to the injected group. Guarded by a runtime poison pill (panics in k8s)
	// + /metrics tripwire + #448 SAST (forbids the env var in manifests).
	devBypassAuth := flag.Bool("dev-bypass-auth", envBool("TA_DEV_BYPASS_AUTH"),
		"LOCAL DEV ONLY: inject a dev identity when no oauth2-proxy header is present (default off; panics if run in Kubernetes)")
	devBypassEmail := flag.String("dev-bypass-email", envOrDefault("TA_DEV_BYPASS_EMAIL", "dev@local"),
		"Identity email injected by --dev-bypass-auth")
	devBypassGroups := flag.String("dev-bypass-groups", envOrDefault("TA_DEV_BYPASS_GROUPS", "demo-admins"),
		"Comma-separated IdP groups injected by --dev-bypass-auth (must map to tenants in _rbac.yaml)")

	// v2.6.0: PR-based write-back mode (ADR-011)
	// Supports: "direct" (default), "pr" or "pr-github" (GitHub PRs), "pr-gitlab" (GitLab MRs)
	writeMode := flag.String("write-mode", envOrDefault("TA_WRITE_MODE", "direct"),
		"Write-back mode: 'direct' (commit-on-write), 'pr' or 'pr-github' (GitHub PR), 'pr-gitlab' (GitLab MR)")

	// GitHub flags
	ghRepo := flag.String("github-repo", envOrDefault("TA_GITHUB_REPO", ""),
		"GitHub repository in owner/repo format (required for pr/pr-github mode)")
	ghBaseBranch := flag.String("github-base-branch", envOrDefault("TA_GITHUB_BASE_BRANCH", "main"),
		"Target branch for GitHub PRs (default: main)")

	// LOCAL git base branch the gitops Writer branches from / returns to in PR mode
	// (#638). Forge-neutral — distinct from the forge's PR target above. Set to your
	// conf.d repo's default branch if it isn't "main" (e.g. "master").
	gitBaseBranch := flag.String("git-base-branch", envOrDefault("TA_GIT_BASE_BRANCH", "main"),
		"Local base branch the gitops Writer branches from in PR mode (default: main)")

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

	// #143: SSE (/api/v1/events) per-client liveness. Duration form (matches
	// TA_*_TIMEOUT) rather than the issue's original _SEC integer naming, for
	// consistency with the server-timeout knobs above. See ws.Config docs for
	// the heartbeat↔write-deadline dependency and the cost of disabling each.
	sseHeartbeat := flag.Duration("sse-heartbeat",
		parseDurationOrDefault(os.Getenv("TA_SSE_HEARTBEAT"), 25*time.Second),
		"SSE per-client heartbeat interval (default 25s; 0=disable, which re-opens the idle-stuck-client leak; must stay below the downstream proxy idle timeout; TA_SSE_HEARTBEAT)")
	sseWriteTimeout := flag.Duration("sse-write-timeout",
		parseDurationOrDefault(os.Getenv("TA_SSE_WRITE_TIMEOUT"), 10*time.Second),
		"SSE per-write deadline; a stuck client's write unblocks the goroutine after this (default 10s; 0=disable; TA_SSE_WRITE_TIMEOUT)")
	sseMaxLifetime := flag.Duration("sse-max-lifetime",
		parseDurationOrDefault(os.Getenv("TA_SSE_MAX_LIFETIME"), 0),
		"SSE hard max connection lifetime cap, defense-in-depth (default 0=disabled; TA_SSE_MAX_LIFETIME)")

	// #144: request body size cap. Pre-issue this was hardcoded to
	// 1<<20 in every write handler; now configurable via env so
	// operators with atypical payloads (e.g. tenants with deeply-
	// nested rule packs) can raise it without rebuilding.
	maxBodyBytesEnv := os.Getenv("TA_MAX_BODY_BYTES")

	// v2.9.0 ADR-020 IV-2d: tenant federation token endpoint.
	// An empty --federation-key disables the endpoint entirely.
	federationKey := flag.String("federation-key", envOrDefault("TA_FEDERATION_KEY", ""),
		"Path to the RS256 private key (PEM) for signing federation tokens. Empty disables the federation endpoint.")
	federationStore := flag.String("federation-store", envOrDefault("TA_FEDERATION_STORE", "tenant-federation-store"),
		"Name of the ConfigMap holding the federation token record store (ADR-020 Posture B). The Helm chart pre-creates it.")
	federationNamespace := flag.String("federation-namespace", envOrDefault("TA_FEDERATION_NAMESPACE", ""),
		"Namespace of the federation store ConfigMap. Empty uses the pod's own namespace.")
	federationTTL := flag.Duration("federation-token-ttl",
		parseDurationOrDefault(os.Getenv("TA_FEDERATION_TOKEN_TTL"), token.DefaultTTL),
		"Federation token lifetime (default 4h; ADR-020 §Token model)")
	// v2.9.0 ADR-020 IV-2e: federation admission validator backend.
	federationPrometheusURL := flag.String("federation-prometheus-url",
		envOrDefault("TA_FEDERATION_PROMETHEUS_URL", ""),
		"Base URL of the Prometheus/VictoriaMetrics backend the federation admission validator queries (Series API). Empty disables admission.")

	// ADR-027 PR-1b-i: machine-identity audit (KSA projected token +
	// TokenReview). Opt-in and AUDIT-ONLY — when enabled, tenant-api verifies
	// a caller's ServiceAccount token (if present) and records the outcome
	// (metric + log). It never changes authz (which stays header-driven) and
	// never fails the request; being a synchronous TokenReview it may add
	// bounded latency to a Bearer-carrying request. Enabling requires an
	// in-cluster config; a missing one is fatal (MED-7 fail-loud, no silent skip).
	machineIdentityAudit := flag.Bool("machine-identity-audit", envBool("TA_MACHINE_IDENTITY_AUDIT"),
		"ADR-027: audit-only verification of caller ServiceAccount tokens via TokenReview (verify+log+metric; never changes authz or fails the request; a synchronous review may add bounded latency to Bearer requests). Requires in-cluster config. Default off.")
	machineIdentityAudience := flag.String("machine-identity-audience", envOrDefault("TA_MACHINE_IDENTITY_AUDIENCE", "tenant-api"),
		"ADR-027 G4: audience bound into every TokenReview and required in the result. An empty audience would accept any SA token — the Helm chart enforces non-empty; this flag defaults to 'tenant-api'.")
	machineIdentityIssuer := flag.String("machine-identity-issuer", envOrDefault("TA_MACHINE_IDENTITY_ISSUER", ""),
		"ADR-027: comma-separated cluster-issuer allowlist for machine-identity audit dispatch. Empty (default) sends any issuer to TokenReview (the apiserver is the sole verifier). Reserved for keypool isolation when the human JWT-A path lands.")

	// ADR-027 D2-B: human-plane Unix domain socket. When set, tenant-api serves
	// the SAME router on a SECOND http.Server bound to this pod-internal UDS, in
	// addition to the network TCP --addr. The same-pod oauth2-proxy points its
	// --upstream at this socket, so human (browser) traffic never touches the
	// network 8080 plane. Each listener stamps its identity (tcp|uds) into the
	// request context via ConnContext — a connection-derived trust signal the
	// request cannot forge — which the machine-identity audit uses to keep the
	// UDS human plane OUT of its denominator. Empty (default) = single TCP
	// listener, byte-identical to pre-D2-B behavior.
	humanSocket := flag.String("human-socket", envOrDefault("TA_HUMAN_SOCKET", ""),
		"ADR-027 D2-B: path to a pod-internal Unix domain socket for the human (oauth2-proxy) plane. When set, the same router is ALSO served here and this listener is tagged as the trusted human hop (excluded from the machine-identity audit). Empty (default) = TCP-only, unchanged behavior.")

	flag.Parse()

	// PR-10/11: structured (JSON) logging via slog. Configure before
	// any Manager / Writer / Tracker construction so their initial-
	// load logs go through the same pipeline.
	configureLogger()

	slog.Info("tenant-api starting", "config_dir", *configDir, "addr", *listenAddr)

	// ── dev-auth-bypass safety (ADR-022) ──────────────────────────────────────
	// Layer 3: runtime poison pill — refuse (panic) to start with the bypass
	// inside a Kubernetes cluster. Layer 2: loud WARN + /metrics tripwire gauge.
	if *devBypassAuth {
		rbac.DevBypassK8sGuard(os.Getenv, pathExists)
		handler.SetDevBypassActive(true)
		slog.Warn("⚠️  DEV AUTH BYPASS ACTIVE — a dev identity is injected for requests without an oauth2-proxy header. LOCAL DEV ONLY; never enable in production.",
			"inject_email", *devBypassEmail, "inject_groups", *devBypassGroups)
	}

	// ── Dependencies ──────────────────────────────────────────────────────────
	rbacMgr, err := rbac.NewManager(*rbacPath)
	if err != nil {
		log.Fatalf("FATAL: rbac init: %v", err)
	}
	// MED-8: a configured --rbac path fails closed on an empty policy by
	// default. --rbac-empty-open restores the legacy open-read behavior.
	if *rbacEmptyOpen {
		rbacMgr.AllowOpenReadOnEmpty()
		log.Printf("WARN: --rbac-empty-open set: an empty _rbac.yaml grants open read to all authenticated identities (MED-8 fail-closed disabled)")
	}
	if *rbacPath == "" {
		log.Printf("WARN: no --rbac path configured: running in open-read mode (all authenticated identities have read access)")
	}

	// ADR-027 / LD-6 P1: metadata (env/domain) scope filter fail-mode. The
	// would-deny recorder is wired unconditionally so tenant_api_scope_would_
	// deny_total is always present (0-series). Default is SHADOW — an unlabeled
	// tenant on a restricted rule still passes (byte-identical to the legacy
	// fail-open) but is counted. --rbac-metadata-scope-enforce flips to
	// fail-closed AFTER the counter stops incrementing over the soak window
	// (it is a monotonic counter: the flip signal is a zero rate/increase, not
	// an absolute zero value).
	rbacMgr.SetScopeAuditor(handler.NewScopeWouldDenyRecorder())
	if *rbacMetadataScopeEnforce {
		rbacMgr.EnableMetadataScopeEnforce()
		log.Printf("INFO: --rbac-metadata-scope-enforce set: unlabeled tenants on env/domain-restricted rules are DENIED (metadata scope fail-closed)")
	} else {
		log.Printf("INFO: metadata scope filter in SHADOW mode: unlabeled tenants still pass; watch increase(tenant_api_scope_would_deny_total{axis=\"metadata\"}[<soak-window>]) and flip --rbac-metadata-scope-enforce once it stays 0 across the window (monotonic counter — its rate, not its value, is the signal)")
	}

	// ADR-027 PR-1b-i: machine-identity audit side-channel. Built here so an
	// in-cluster-config failure is fatal (MED-7 fail-loud) rather than a
	// silently-skipped verification. Disabled (nil) by default; when enabled
	// it is installed on the RBAC manager and observes every request WITHOUT
	// affecting the authorization decision.
	machineAuditor, err := wireMachineAuditor(machineAuditorFlags{
		Enabled:     *machineIdentityAudit,
		Audience:    *machineIdentityAudience,
		IssuerAllow: splitCSV(*machineIdentityIssuer),
	})
	if err != nil {
		log.Fatalf("FATAL: machine-identity audit init: %v", err)
	}
	if machineAuditor != nil {
		rbacMgr.SetMachineAuditor(machineAuditor)
		slog.Info("machine-identity audit enabled (ADR-027; audit-only, does not affect authz)",
			"audience", *machineIdentityAudience,
			"issuer_allowlist", splitCSV(*machineIdentityIssuer))
	}

	// v2.6.0: WebSocket/SSE hub for real-time config change notifications.
	// #143: per-client liveness (heartbeat + per-write deadline) from TA_SSE_* env.
	eventHub := ws.NewHubWithConfig(ws.Config{
		HeartbeatInterval: *sseHeartbeat,
		WriteTimeout:      *sseWriteTimeout,
		MaxLifetime:       *sseMaxLifetime,
	})

	writer := gitops.NewWriter(*configDir, *gitDir)
	writer.SetBaseBranch(*gitBaseBranch) // #638: explicit PR-mode base (forge-neutral)
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

	// v2.9.0 ADR-020 IV-2e: federation 2-tier policy — the platform
	// metric whitelist (`_federation_policy.yaml`). Per-tenant subsets
	// live in separate files, read on demand by the handler.
	federationPolicyMgr := fedpolicy.NewManager(*configDir)

	// v2.9.0 ADR-020 IV-2e: federation admission validator. nil when
	// --federation-prometheus-url is unset — admission is then disabled
	// and whitelist edits are schema-checked only.
	federationValidator := fedpolicy.NewAdmissionValidator(*federationPrometheusURL)
	if federationValidator == nil {
		slog.Info("federation admission validator disabled — set --federation-prometheus-url to enable")
	}

	// v2.9.0 ADR-024 §S6 (#741): metric discovery catalog backing the
	// portal recipe-authoring UX. Shares the same Prometheus backend as
	// the admission validator; nil (→ HTTP 503) when the URL is unset.
	metricDiscoverer := fedpolicy.NewMetricDiscoverer(*federationPrometheusURL)
	if metricDiscoverer == nil {
		slog.Info("metric discovery disabled — set --federation-prometheus-url to enable")
	}

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

	// v2.9.0 ADR-020 IV-2d: federation token signer. Optional — when
	// --federation-key is unset wireFederation returns nil and the
	// /federation routes below stay unregistered. Posture B: token
	// records live in a Kubernetes ConfigMap so tenant-api stays
	// stateless and can run multi-replica.
	federationMgr, federationNS, err := wireFederation(federationFlags{
		KeyPath:       *federationKey,
		ConfigMapName: *federationStore,
		Namespace:     *federationNamespace,
		TTL:           *federationTTL,
	})
	if err != nil {
		log.Fatalf("FATAL: federation init: %v", err)
	}
	// v2.10.0 ADR-021 (#609): monotonic AccountID allocator for log
	// federation. Shares federation's --federation-key gate — it is only
	// consulted for capability=logs token issuance and the backfill
	// endpoint. Persisted commit-on-write into conf.d/_account_registry.yaml
	// via the same gitops Writer (no external stateful DB).
	var accountAllocator *account.Allocator
	if federationMgr != nil {
		accountAllocator = account.NewAllocator(writer)
		// #609 (Gemini #2) fail-loud startup guard: if the AccountID registry is
		// blank/missing BUT conf.d already holds tenants, the ledger was lost
		// (truncated mount / interrupted write) and booting would silently
		// re-issue ids from the floor → cross-tenant log leak. Day-0 (no tenants
		// yet) with a blank registry is fine and proceeds. Refuse to start
		// otherwise — before any token can be issued against the reset registry.
		if err := account.VerifyRegistryNotResetWithFleet(*configDir); err != nil {
			log.Fatalf("FATAL: %v", err)
		}
		slog.Info("federation token endpoint enabled",
			"token_ttl", federationMgr.TTL(),
			"store_configmap", *federationStore, "store_namespace", federationNS,
			"account_registry", account.RegistryFileName)
		if len(rbacMgr.Get().Groups) == 0 {
			if rbacMgr.FailClosedOnEmpty() {
				// MED-8: a configured --rbac path resolved to zero groups → ALL
				// access is denied (not just federation); "supply --rbac" would
				// be wrong advice since a path is already set.
				slog.Warn("federation endpoint enabled but RBAC fails closed on an empty configured policy — ALL access is denied; fix the _rbac.yaml referenced by --rbac")
			} else {
				slog.Warn("federation endpoint enabled but RBAC is in open mode — every token issuance will be denied (admin permission required); supply --rbac")
			}
		}
	}

	// Wire all handler dependencies into a single struct (PR-4/11).
	// Every handler is now a method on *deps; pass-through positional
	// args are gone.
	deps := &handler.Deps{
		ConfigDir:          *configDir,
		Writer:             writer,
		RBAC:               rbacMgr,
		Policy:             policyMgr,
		Groups:             groupMgr,
		Views:              viewMgr,
		Federation:         federationMgr,
		Accounts:           accountAllocator,
		FederationPolicy:   federationPolicyMgr,
		AdmissionValidator: federationValidator,
		MetricDiscoverer:   metricDiscoverer,
		Tasks:              taskMgr,
		PRClient:           prClient,
		PRTracker:          prTracker,
		WriteMode:          wm,
		HumanSocketPath:    *humanSocket,
		SearchCache:        handler.NewTenantSnapshotCache(),
		// #609 CodeRabbit: the fleet-wide AccountID backfill must be bounded by
		// the operator's --write-timeout, NOT the global 30s request Timeout
		// middleware — the handler detaches from the request deadline and uses
		// this instead (see federation.BackfillAccounts).
		BackfillTimeoutDur: *writeTimeout,
	}

	// ── RBAC + policy hot-reload goroutines ───────────────────────────────────
	stopCh := make(chan struct{})
	go rbacMgr.WatchLoop(*reloadInterval, stopCh)
	go policyMgr.WatchLoop(*reloadInterval, stopCh)
	go federationPolicyMgr.WatchLoop(*reloadInterval, stopCh)
	if prTracker != nil {
		go prTracker.WatchLoop(stopCh)
	}

	// v2.9.0 ADR-020 #521: federation offboarding orphan detector.
	// Warn-only — flags zombie tokens / stale subset files for the
	// offboarding runbook; never auto-revokes (a transient conf.d
	// glitch must not nuke live tenants' tokens).
	if federationMgr != nil {
		go orphan.NewDetector(*configDir, federationMgr.ListAllRecords).
			Run(*reloadInterval, stopCh)
	}

	// ── Router ────────────────────────────────────────────────────────────────
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
	if *devBypassAuth {
		r.Use(rbac.DevBypassMiddleware(*devBypassEmail, *devBypassGroups))
	}

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

	// #144: parse TA_MAX_BODY_BYTES. Empty / valid → no warn;
	// malformed (negative, zero, non-numeric) → WARN + fallback.
	maxBodyBytes, mbbMalformed := handler.MaxBodyBytesFromEnv(maxBodyBytesEnv)
	if mbbMalformed {
		slog.Warn("max body bytes env malformed, falling back to default",
			"env_value", maxBodyBytesEnv,
			"default_bytes", maxBodyBytes)
	}
	deps.MaxBodyBytes = maxBodyBytes
	slog.Info("request body size limit", "max_bytes", maxBodyBytes)
	if rlCfg.RequestsPerMinute > 0 {
		slog.Info("rate limiter enabled", "per_min_per_caller", rlCfg.RequestsPerMinute)
	} else {
		slog.Info("rate limiter disabled", "hint", "set TA_RATE_LIMIT_PER_MIN > 0 to enable")
	}
	// PR-11/11: stopCh terminates the limiter's bucket-sweeper
	// goroutine alongside the rbac/policy/tracker WatchLoops.
	// The second return value (limiter handle) is for tests;
	// /metrics finds the limiter via activeLimiter, set inside
	// RateLimit().
	rlMw, _ := handler.RateLimit(rlCfg, stopCh)
	r.Use(rlMw)

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
		if prTracker != nil {
			r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
				Get("/prs", handler.ListPRs(deps))
		}

		// Real-time event stream (v2.6.0 — SSE for config change notifications)
		r.With(rbacMgr.Middleware(rbac.PermRead, nil)).
			Get("/events", eventHub.ServeHTTP)

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

		// Federation token endpoint (v2.9.0 — ADR-020 IV-2d).
		// Registered only when a signing key is configured. Route-level
		// middleware checks authentication; per-tenant admin permission
		// is enforced inside each handler because the tenant ID is in
		// the body / query / token record, not the URL path.
		if federationMgr != nil {
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

	// ── HTTP server(s) with graceful shutdown ─────────────────────────────────
	// PR-11/11: timeouts now configurable via TA_{READ,WRITE,IDLE}_TIMEOUT.
	//
	// ADR-027 D2-B: the SAME router `r` is served by up to two http.Servers —
	// the network TCP listener (--addr, machine/relay plane) and, when
	// --human-socket is set, a pod-internal Unix domain socket (human plane
	// fronted by the same-pod oauth2-proxy). Each server's ConnContext stamps
	// its listener identity (tcp|uds) onto every request context at accept time.
	// This is CONNECTION-derived: it reflects which socket accepted the
	// connection, so a request cannot forge or strip it (contrast a header). The
	// machine-identity audit reads it to keep the UDS human plane out of its
	// denominator (ADR-027 §2.3). Absence of the stamp defaults to TCP
	// (rbac.ListenerFromContext) — the fail-safe direction.
	srvTCP := &http.Server{
		Addr:         *listenAddr,
		Handler:      r,
		ReadTimeout:  *readTimeout,
		WriteTimeout: *writeTimeout,
		IdleTimeout:  *idleTimeout,
		ConnContext: func(ctx context.Context, _ net.Conn) context.Context {
			return rbac.WithListener(ctx, rbac.ListenerTCP)
		},
	}
	slog.Info("http server timeouts",
		"read", *readTimeout, "write", *writeTimeout, "idle", *idleTimeout)

	// Human-plane UDS server (optional). Built here so a bind failure is fatal
	// (MED-7 fail-loud) rather than a silent fallback to TCP-only, which would
	// route human traffic back over the network 8080 plane D2-B exists to drain.
	var srvUDS *http.Server
	var udsListener net.Listener
	if *humanSocket != "" {
		ln, err := listenUnix(*humanSocket)
		if err != nil {
			log.Fatalf("FATAL: human socket: %v", err)
		}
		udsListener = ln
		// Emit the tenant_api_human_socket_up gauge (the Ready self-dial drives
		// its value). Only when the socket is configured, so it's never a false 0.
		handler.SetHumanSocketConfigured(true)
		srvUDS = &http.Server{
			Handler:      r,
			ReadTimeout:  *readTimeout,
			WriteTimeout: *writeTimeout,
			IdleTimeout:  *idleTimeout,
			ConnContext: func(ctx context.Context, _ net.Conn) context.Context {
				return rbac.WithListener(ctx, rbac.ListenerUDS)
			},
		}
		slog.Info("human-plane Unix socket enabled (ADR-027 D2-B)", "path", *humanSocket)
	}

	// Graceful shutdown on SIGTERM / SIGINT
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGTERM, syscall.SIGINT)

	go func() {
		slog.Info("tenant-api listening", "addr", *listenAddr)
		if err := srvTCP.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("FATAL: listen: %v", err)
		}
	}()

	if srvUDS != nil {
		go func() {
			slog.Info("tenant-api listening (human plane)", "socket", *humanSocket)
			// Serve on the already-bound UDS listener. A non-ErrServerClosed
			// error means the human plane died — fatal, so the kubelet restarts
			// the pod rather than the process limping along serving only TCP
			// (which would silently push humans back onto the 8080 plane).
			if err := srvUDS.Serve(udsListener); err != nil && err != http.ErrServerClosed {
				log.Fatalf("FATAL: human socket serve: %v", err)
			}
		}()
	}

	<-quit
	slog.Info("tenant-api shutting down")

	if err := taskMgr.Close(); err != nil {
		slog.Warn("task manager close error", "error", err)
	}
	close(stopCh)

	// #675: gracefully tear down SSE before srv.Shutdown. SSE streams are never
	// idle, so without this srv.Shutdown blocks the full 15s grace period waiting
	// for them to drain, then severs them abruptly. Hub.Shutdown broadcasts a
	// "server_shutdown" hint (so clients back off with jitter instead of
	// stampeding the new pod) and closes the streams, letting Shutdown finish in
	// milliseconds. 2s reconnect hint: long enough to cover a typical new-pod
	// readiness gap, short enough not to feel like an outage to a watching client.
	eventHub.Shutdown(2 * time.Second)

	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	// Shut down BOTH servers on the same deadline. The UDS server first so the
	// human plane drains before the TCP plane; either Shutdown returning is
	// logged but non-fatal (we're already exiting). srvUDS.Shutdown also closes
	// the underlying listener, which unlinks the socket file (Go UnixListener
	// semantics) — the successor pod's unlink-before-bind covers an unclean exit.
	if srvUDS != nil {
		if err := srvUDS.Shutdown(ctx); err != nil {
			slog.Warn("human socket shutdown error", "error", err)
		}
	}
	if err := srvTCP.Shutdown(ctx); err != nil {
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

// envBool returns true when the env var is a truthy string ("true"/"1"/"yes",
// case-insensitive). Used for the --dev-bypass-auth flag default.
func envBool(key string) bool {
	switch strings.ToLower(strings.TrimSpace(os.Getenv(key))) {
	case "true", "1", "yes", "on":
		return true
	}
	return false
}

// pathExists reports whether a filesystem path exists (used by the
// dev-bypass Kubernetes poison-pill guard to probe the SA token mount).
func pathExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

// splitCSV splits a comma-separated flag value into trimmed, non-empty items.
// An empty string yields nil (used for --machine-identity-issuer, where nil
// means "no allowlist — send any issuer to TokenReview").
func splitCSV(s string) []string {
	var out []string
	for _, part := range strings.Split(s, ",") {
		part = strings.TrimSpace(part)
		if part != "" {
			out = append(out, part)
		}
	}
	return out
}
