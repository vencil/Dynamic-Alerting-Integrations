package handler

// Deps is the dependency container shared by every tenant-api HTTP
// handler. Pre-PR-4 each handler constructor took 1–8 positional
// arguments; the worst (BatchTenants) took 8 — adding a single new
// dependency required editing every call site. This struct collapses
// the surface to one place.
//
// All fields are wired once at startup in cmd/server/main.go. Every
// handler method on *Deps reads them as needed; nothing on Deps gets
// mutated after wiring (the embedded managers do their own internal
// concurrency control).
//
// Note on PRClient / PRTracker / WriteMode:
//
//	When tenant-api runs in `direct` write-back mode (ADR-009 default)
//	these three are zero-valued. Handlers that branch on PR mode
//	(PutTenant, BatchTenants) MUST gate with `d.WriteMode.IsPRMode()`
//	AND the nil-checks for client/tracker so the direct path stays
//	allocation-free.

import (
	"github.com/vencil/tenant-api/internal/async"
	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/groups"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/views"
)

// WriteMode represents the tenant-api write-back mode (ADR-011).
// Lives here (not in tenant_put.go) because every handler that does
// writes branches on it via Deps.
type WriteMode string

const (
	// WriteModeDirect is the default commit-on-write mode (ADR-009).
	WriteModeDirect WriteMode = "direct"
	// WriteModePR creates a GitHub PR instead of committing directly (ADR-011).
	WriteModePR WriteMode = "pr"
	// WriteModePRGitHub is an explicit alias for GitHub PR mode.
	WriteModePRGitHub WriteMode = "pr-github"
	// WriteModePRGitLab creates a GitLab MR instead of committing directly (ADR-011).
	WriteModePRGitLab WriteMode = "pr-gitlab"
)

// IsPRMode returns true if the write mode is any PR/MR-based mode.
func (wm WriteMode) IsPRMode() bool {
	return wm == WriteModePR || wm == WriteModePRGitHub || wm == WriteModePRGitLab
}

// Deps wires every dependency every handler might need. Construct
// once at startup; pass `*Deps` (NOT `Deps`) — handlers expect a
// stable address so middleware closures can capture it.
type Deps struct {
	// ConfigDir is the directory containing tenant YAML files +
	// _rbac.yaml / _groups.yaml / _views.yaml / _domain_policy.yaml.
	ConfigDir string

	// Writer commits configuration changes to the GitOps repo.
	Writer *gitops.Writer

	// RBAC enforces tenant-scoped permissions on every routed handler.
	// Open-mode (no _rbac.yaml) returns "allow all" from HasPermission.
	RBAC *rbac.Manager

	// Policy enforces domain-level write policy. Optional — handlers
	// that touch it MUST nil-check (`if d.Policy != nil { ... }`).
	Policy *policy.Manager

	// Groups manages the `_groups.yaml` view of tenant groupings.
	Groups *groups.Manager

	// Views manages saved filter views (`_views.yaml`).
	Views *views.Manager

	// Federation signs tenant federation tokens (ADR-020 IV-2d).
	// Optional — nil when no signing key is configured, in which case
	// main.go leaves the /federation/tokens routes unregistered, so
	// handlers reading it are never reached with a nil value.
	Federation *token.Manager

	// Accounts allocates monotonic per-tenant AccountIDs for log
	// federation (ADR-021 / #609), persisted commit-on-write into
	// conf.d/_account_registry.yaml via Writer. Wired alongside
	// Federation (same --federation-key gate) — it is only consulted when
	// a logs-plane (capability=logs) token is requested, so it shares the
	// federation feature's nil-when-disabled lifecycle.
	Accounts *account.Allocator

	// FederationPolicy holds the platform federation whitelist
	// (ADR-020 IV-2e, `_federation_policy.yaml`). Always wired — the
	// 2-tier policy is independent of token signing.
	FederationPolicy *fedpolicy.Manager

	// AdmissionValidator runs the data-layer label-enrichment check
	// when a metric is added to the federation whitelist (ADR-020
	// IV-2e). Optional — nil when --federation-prometheus-url is unset,
	// in which case PutFederationPolicy skips admission and the
	// whitelist edit is schema-checked only.
	AdmissionValidator *fedpolicy.AdmissionValidator

	// MetricDiscoverer backs GET /tenants/{id}/metrics — the stateless
	// metric-discovery catalog for the portal recipe-authoring UX
	// (ADR-024 §S6, #741). Optional — nil when --federation-prometheus-url
	// is unset (shares the same Prometheus backend as AdmissionValidator),
	// in which case DiscoverMetrics returns HTTP 503.
	MetricDiscoverer *fedpolicy.MetricDiscoverer

	// Tasks runs async batch operations behind a goroutine pool.
	Tasks *async.Manager

	// PRClient / PRTracker are populated only in PR/MR write-back
	// modes. In `direct` mode both are nil; handlers that read them
	// MUST also check WriteMode.IsPRMode().
	PRClient  platform.Client
	PRTracker platform.Tracker

	// WriteMode selects the write-back path:
	//   direct      — commit-on-write (ADR-009)
	//   pr          — alias for pr-github
	//   pr-github   — GitHub PR (ADR-011)
	//   pr-gitlab   — GitLab MR (ADR-011)
	WriteMode WriteMode

	// SearchCache is the snapshot cache used by SearchTenants.
	// Shared across requests so the 30s TTL has effect — see
	// tenant_search.go for design notes.
	SearchCache *tenantSnapshotCache

	// MaxBodyBytes caps the request body every write handler will
	// read via `io.LimitReader`. Wired from `TA_MAX_BODY_BYTES`
	// (default 1 MiB; see DefaultMaxBodyBytes / MaxBodyBytesFromEnv
	// in middleware.go). Read via d.MaxBody() so a zero value (e.g.
	// in tests that construct Deps literally) falls back to the
	// default instead of rejecting every write.
	MaxBodyBytes int64
}

// MaxBody returns d.MaxBodyBytes with a fallback to
// DefaultMaxBodyBytes when unset (zero / negative). Handlers should
// call this rather than reading the field directly so test
// fixtures that build Deps without wiring MaxBodyBytes keep
// working unchanged.
func (d *Deps) MaxBody() int64 {
	if d.MaxBodyBytes <= 0 {
		return DefaultMaxBodyBytes
	}
	return d.MaxBodyBytes
}
