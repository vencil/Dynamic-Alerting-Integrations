package handler

// ============================================================
// GET /api/v1/tenants/{id}/effective — v2.7.0 (ADR-016 + ADR-017)
// ============================================================
//
// Returns the *merged* effective config for one tenant: _defaults.yaml chain
// (L0..Ln), then the root platform files' `tenants:` entries for it (#2019),
// then the tenant's own overrides, plus two SHA-256[:16] hashes for change
// detection.
//
// The handler is stateless — it re-scans `configDir` on every request. That's
// acceptable because (a) this endpoint is low-traffic (UI + support tooling,
// not a hot path like /metrics), and (b) the read-only, scan-each-call shape
// matches the existing GET /api/v1/tenants/{id} handler, avoiding any new
// shared-mutable-state surface area between tenant-api and the exporter.
//
// Parity: the merged_hash returned here is byte-identical to
// describe_tenant.py's computed hash for the shapes the golden fixtures
// cover. ⚠️ The assertion is not in this package — tenant_effective_test.go
// pins hash stability and shape, not cross-language equality (it says so
// itself); the byte-for-byte claim rests on threshold-exporter's
// app/config_golden_parity_test.go, which exercises the same pkg/config this
// handler imports. Chain discovery included: TestGoldenParity_ResolveEffective
// calls the same config.ResolveEffective this handler does on every golden
// fixture and compares chain, hashes and effective config with the Python
// capture (#1550). Before it, reversing the chain order in ResolveEffective
// moved the served merged_hash and left every golden assertion green.

import (
	"errors"
	"log/slog"
	"net/http"

	"github.com/go-chi/chi/v5"
	cfg "github.com/vencil/threshold-exporter/pkg/config"

	"github.com/vencil/tenant-api/internal/confd"
)

// GetTenantEffective handles GET /api/v1/tenants/{id}/effective.
//
// @Summary     Get tenant effective (merged) config
// @Description Returns the tenant config after merging the _defaults.yaml
// @Description chain from L0 (conf.d root) down to the tenant's own file,
// @Description with the root platform files' per-tenant `tenants:` entries
// @Description applied between the chain and the tenant file (the tenant
// @Description file wins key by key); platform_overlay names the files and
// @Description keys that layer supplied and is omitted when it supplied none.
// @Description A tenant's _profile is expanded as /metrics expands it: the
// @Description profile (from the root platform files' profiles: blocks) fills
// @Description in keys neither the tenant file nor the platform entries set;
// @Description profile_overlay names the profile, the files and the keys it
// @Description supplied and is omitted when it supplied none.
// @Description Includes two SHA-256 hashes (truncated to 16 hex chars):
// @Description source_hash for raw file content and merged_hash for the
// @Description canonical-JSON of the merged dict. Parity target:
// @Description scripts/tools/dx/describe_tenant.py.
// @Tags        tenants
// @Produce     json
// @Param       id   path     string true "Tenant ID"
// @Success     200  {object} cfg.EffectiveConfig
// @Failure     400  {object} ErrorResponse
// @Failure     404  {object} ErrorResponse
// @Failure     409  {object} ErrorResponse "Conflict: ambiguous tenant file, or the tenant is declared by more than one conf.d file"
// @Failure     500  {object} ErrorResponse
// @Router      /api/v1/tenants/{id}/effective [get]
func GetTenantEffective(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		// #2511: `<id>.yaml` beside `<id>.yml` is answered exactly as
		// GET /tenants/{id} answers it — the same resolver, the same sentinel,
		// so 409 CONFLICT naming only the two base names. Before this, the
		// shape reached ResolveEffective's walker, whose *DuplicateTenantError
		// fell through to 500 with both ABSOLUTE paths in the body.
		//
		// Only the ambiguity verdict is taken from the resolver. Its other
		// answers are not this endpoint's: a tenant declared only in a
		// subdirectory or by a shared file's `tenants:` block has no top-level
		// `<id>.yaml`, yet /effective resolves it — ResolveEffective below
		// stays the authority for not-found and for every I/O failure.
		if _, rerr := confd.ResolveTenantFile(d.ConfigDir, tenantID); errors.Is(rerr, confd.ErrAmbiguousTenantFile) {
			WriteJSONError(w, r, http.StatusConflict, rerr.Error())
			return
		}

		ec, err := cfg.ResolveEffective(d.ConfigDir, tenantID)
		if err != nil {
			var dup *cfg.DuplicateTenantError
			switch {
			case errors.Is(err, cfg.ErrTenantNotFound):
				WriteJSONError(w, r, http.StatusNotFound, "tenant not found: "+tenantID)
			case errors.As(err, &dup):
				// #2511: the duplicates the top-level resolver cannot see (a
				// subdirectory copy, another file's `tenants:` entry). The
				// typed error carries both files' absolute paths, so it goes
				// to the log only; the body is the fixed text the write plane
				// already uses for this shape, which names no file — the
				// other declaring file may sit under a path this caller has
				// no business learning.
				slog.Warn("tenant effective: tenant declared by more than one conf.d file",
					"tenant", tenantID, "error", err)
				WriteJSONError(w, r, http.StatusConflict, msgTenantDeclaredElsewhere)
			default:
				WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			}
			return
		}

		// A YAML `.inf` / `.nan` leaves a non-finite float in the tree, which
		// JSON cannot carry: send it as the text Python's json.dumps writes.
		// merged_hash was computed from the original tree, so it is unchanged.
		out := *ec
		out.EffectiveConfig = cfg.NonFiniteAsText(ec.EffectiveConfig)
		writeJSON(w, http.StatusOK, out)
	}
}
