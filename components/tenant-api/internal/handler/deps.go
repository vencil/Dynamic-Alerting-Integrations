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
}
