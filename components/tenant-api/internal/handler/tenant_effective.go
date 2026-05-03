package handler

// ============================================================
// GET /api/v1/tenants/{id}/effective — v2.7.0 (ADR-017 + ADR-018)
// ============================================================
//
// Returns the *merged* effective config for one tenant: _defaults.yaml chain
// (L0..Ln) deep-merged with the tenant's own overrides, plus two SHA-256[:16]
// hashes for change detection.
//
// The handler is stateless — it re-scans `configDir` on every request. That's
// acceptable because (a) this endpoint is low-traffic (UI + support tooling,
// not a hot path like /metrics), and (b) the read-only, scan-each-call shape
// matches the existing GET /api/v1/tenants/{id} handler, avoiding any new
// shared-mutable-state surface area between tenant-api and the exporter.
//
// Parity: the merged_hash returned here is byte-identical to
// describe_tenant.py's computed hash — asserted by the 8-fixture golden
// parity test in tests/golden/ + tenant_effective_test.go.

import (
	"encoding/json"
	"errors"
	"net/http"

	"github.com/go-chi/chi/v5"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// GetTenantEffective handles GET /api/v1/tenants/{id}/effective.
//
// @Summary     Get tenant effective (merged) config
// @Description Returns the tenant config after merging the _defaults.yaml
// @Description chain from L0 (conf.d root) down to the tenant's own file.
// @Description Includes two SHA-256 hashes (truncated to 16 hex chars):
// @Description source_hash for raw file content and merged_hash for the
// @Description canonical-JSON of the merged dict. Parity target:
// @Description scripts/tools/dx/describe_tenant.py.
// @Tags        tenants
// @Produce     json
// @Param       id   path     string true "Tenant ID"
// @Success     200  {object} cfg.EffectiveConfig
// @Failure     400  {object} map[string]string
// @Failure     404  {object} map[string]string
// @Failure     500  {object} map[string]string
// @Router      /api/v1/tenants/{id}/effective [get]
func (d *Deps) GetTenantEffective() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			writeJSONError(w, http.StatusBadRequest, err.Error())
			return
		}

		ec, err := cfg.ResolveEffective(d.ConfigDir, tenantID)
		if err != nil {
			if errors.Is(err, cfg.ErrTenantNotFound) {
				writeJSONError(w, http.StatusNotFound, "tenant not found: "+tenantID)
				return
			}
			writeJSONError(w, http.StatusInternalServerError, err.Error())
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(ec)
	}
}
