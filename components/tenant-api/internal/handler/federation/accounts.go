package federation

// AccountID backfill endpoint (ADR-021 / #609).
//
// Lazy allocation (CreateFederationToken with capability=logs) assigns an
// account_id the first time a tenant mints a logs token. Backfill is the
// one-shot companion: a platform admin POSTs once to assign ids to the
// WHOLE existing fleet up front, so log partitioning is in place before any
// tenant's first logs token rather than trickling in.
//
// It is an admin-only HTTP endpoint (not a separate cmd / startup
// reconcile) for three reasons: it reuses the live RBAC manager and the
// same commit-on-write GitOps audit trail as every other write; it runs
// against the running service with no pod restart; and a startup reconcile
// would commit on every replica boot and race sibling replicas. The single
// committed registry write is serialised by the gitops writer mutex, so
// concurrent backfill + lazy allocation cannot collide.

import (
	"context"
	"encoding/json"
	"net/http"

	"github.com/vencil/tenant-api/internal/federation/account"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
)

// BackfillAccountsResponse reports what a backfill pass did.
type BackfillAccountsResponse struct {
	Status string `json:"status"`
	// Allocated lists the tenants that received a NEW account_id, in
	// allocation (id-ascending) order.
	Allocated []string `json:"allocated"`
	// AllocatedCount is len(Allocated) — convenient for the operator.
	AllocatedCount int `json:"allocated_count"`
	// AlreadyPresent is how many scanned tenants already held an id (so a
	// re-run shows 0 newly allocated and every tenant already present).
	AlreadyPresent int `json:"already_present"`
}

// BackfillAccounts handles POST /api/v1/federation/accounts/backfill.
//
// Assigns a monotonic account_id to every tenant in conf.d that does not
// already have one, in a single committed registry write. Idempotent: a
// second call allocates nothing. Requires PLATFORM admin (a "*"-scoped
// RBAC group) — it touches the whole fleet's partitioning, the same bar as
// editing the platform federation whitelist.
//
// @Summary     Backfill account IDs for all existing tenants
// @Description Assigns a monotonic account_id (ADR-021) to every conf.d tenant lacking one, in one committed registry write. Idempotent. Requires platform admin.
// @Tags        federation
// @Produce     json
// @Success     200 {object} BackfillAccountsResponse
// @Failure     403 {object} handler.ErrorResponse
// @Failure     409 {object} handler.ErrorResponse
// @Failure     500 {object} handler.ErrorResponse
// @Failure     503 {object} handler.ErrorResponse
// @Router      /api/v1/federation/accounts/backfill [post]
func BackfillAccounts(d *handler.Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		// Platform-wide change — require admin via a "*"-scoped group, the
		// same gate as PutFederationPolicy.
		if !d.RBAC.Allowed(rbac.RequestPrincipal(r), "*", rbac.PermAdmin) {
			handler.WriteJSONErrorWithCode(w, r, http.StatusForbidden, handler.CodeForbidden,
				"platform admin permission required to backfill account IDs")
			return
		}
		if d.Accounts == nil {
			handler.WriteJSONError(w, r, http.StatusServiceUnavailable,
				"logs federation is not configured on this server")
			return
		}

		tenantIDs, err := account.ListTenantIDs(d.ConfigDir)
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusInternalServerError,
				"enumerate tenants: "+err.Error())
			return
		}

		// Run the GitOps backfill on a context DETACHED from the request's
		// deadline (the global chi middleware.Timeout caps requests at 30s).
		// Backfill enumerates the whole fleet and does a single committed
		// registry write — on a large fleet / slow forge that can exceed 30s
		// before the operator-tuned --write-timeout would. WithoutCancel keeps
		// the request's values (request_id for log correlation) while dropping
		// its deadline+cancellation; we then bound it by d.BackfillTimeout()
		// (wired from --write-timeout) so the write still can't run unbounded.
		// The gitops writer's own per-command timeout + admission control remain
		// the inner safety net. A client disconnect no longer aborts a half-done
		// allocation — acceptable here: Backfill is idempotent (a re-run
		// allocates nothing) and a severed commit would otherwise leave a dirty
		// tree, mirroring the writer's acquireWrite once-committed boundary.
		ctx, cancel := context.WithTimeout(context.WithoutCancel(r.Context()), d.BackfillTimeout())
		defer cancel()

		res, err := d.Accounts.Backfill(ctx, tenantIDs, rbac.RequestEmail(r))
		if err != nil {
			writeFederationGitError(w, r, err, "backfill account ids: ")
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(BackfillAccountsResponse{
			Status:         "ok",
			Allocated:      res.Allocated,
			AllocatedCount: len(res.Allocated),
			AlreadyPresent: res.AlreadyPresent,
		})
	}
}
