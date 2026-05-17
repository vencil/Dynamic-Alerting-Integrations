package handler

// Federation policy handlers (ADR-020 IV-2e) — the 2-tier metric
// allowlist:
//
//   - GET/PUT /api/v1/federation/policy        — platform whitelist
//     (maintainer-managed; PUT requires a platform admin).
//   - GET/PUT /api/v1/tenants/{id}/federation  — one tenant's subset
//     (tenant-self-managed; PUT requires admin on that tenant, and the
//     subset is rejected if it exceeds the platform whitelist).

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/federation"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"gopkg.in/yaml.v3"
)

// toViolations adapts federation schema failures to the handler's
// validation-error shape (both are {field, reason} — a plain copy).
func toViolations(pv []federation.PolicyViolation) []Violation {
	out := make([]Violation, len(pv))
	for i, v := range pv {
		out[i] = Violation{Field: v.Field, Reason: v.Reason}
	}
	return out
}

// GetFederationPolicy handles GET /api/v1/federation/policy — returns
// the platform federation whitelist.
//
// @Summary     Get the platform federation whitelist
// @Tags        federation
// @Produce     json
// @Success     200 {object} federation.FederationPolicyConfig
// @Router      /api/v1/federation/policy [get]
func (d *Deps) GetFederationPolicy() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(d.FederationPolicy.Get())
	}
}

// PutFederationPolicy handles PUT /api/v1/federation/policy — replaces
// the platform federation whitelist.
//
// Platform-wide config: requires admin via a "*"-scoped RBAC group.
// HasPermission(groups, "*", admin) is true only when a rule's tenant
// list literally contains "*" — exactly a platform admin — because a
// tenant-scoped or prefix-scoped rule never matches the query "*".
//
// @Summary     Replace the platform federation whitelist
// @Tags        federation
// @Accept      json
// @Produce     json
// @Param       body body     federation.FederationPolicyConfig true "Whitelist"
// @Success     200  {object} map[string]any
// @Failure     400  {object} map[string]string
// @Failure     403  {object} map[string]string
// @Failure     409  {object} map[string]string
// @Router      /api/v1/federation/policy [put]
func (d *Deps) PutFederationPolicy() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !d.RBAC.HasPermission(rbac.RequestGroups(r), "*", rbac.PermAdmin) {
			writeJSONErrorWithCode(w, r, http.StatusForbidden, CodeForbidden,
				"platform admin permission required to edit the federation whitelist")
			return
		}
		email := rbac.RequestEmail(r)

		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			writeJSONError(w, r, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}
		var cfg federation.FederationPolicyConfig
		if err := json.Unmarshal(body, &cfg); err != nil {
			writeJSONError(w, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if cfg.Whitelist == nil {
			cfg.Whitelist = []federation.WhitelistEntry{}
		}
		if pv := federation.ValidateWhitelist(&cfg); len(pv) > 0 {
			writeValidationErrors(w, r, toViolations(pv))
			return
		}

		yamlBytes, err := yaml.Marshal(&cfg)
		if err != nil {
			writeJSONError(w, r, http.StatusInternalServerError, "marshal whitelist: "+err.Error())
			return
		}
		if err := d.Writer.WriteFederationPolicyFile(email, string(yamlBytes)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				writeJSONError(w, r, http.StatusConflict, err.Error())
				return
			}
			writeJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}
		_ = d.FederationPolicy.Reload()

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"status":          "ok",
			"whitelist_count": len(cfg.Whitelist),
		})
	}
}

// GetTenantFederation handles GET /api/v1/tenants/{id}/federation —
// returns one tenant's federation metric subset. A tenant with no
// subset file yet gets an empty subset.
//
// @Summary     Get a tenant's federation metric subset
// @Tags        federation
// @Produce     json
// @Param       id path string true "Tenant ID"
// @Success     200 {object} federation.FederationSubset
// @Failure     400 {object} map[string]string
// @Router      /api/v1/tenants/{id}/federation [get]
func (d *Deps) GetTenantFederation() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			writeJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}
		subset, err := d.readFederationSubset(tenantID)
		if err != nil {
			writeJSONError(w, r, http.StatusInternalServerError, "read federation subset: "+err.Error())
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(subset)
	}
}

// PutTenantFederation handles PUT /api/v1/tenants/{id}/federation —
// replaces one tenant's federation metric subset.
//
// Federation moves data past the platform boundary, so editing a
// subset requires admin on the tenant (matching token issuance, #509).
// The subset is rejected if any metric is absent from the platform
// whitelist — the 2-tier containment rule.
//
// @Summary     Replace a tenant's federation metric subset
// @Tags        federation
// @Accept      json
// @Produce     json
// @Param       id   path string true "Tenant ID"
// @Param       body body federation.FederationSubset true "Metric subset"
// @Success     200  {object} map[string]any
// @Failure     400  {object} map[string]string
// @Failure     403  {object} map[string]string
// @Failure     409  {object} map[string]string
// @Router      /api/v1/tenants/{id}/federation [put]
func (d *Deps) PutTenantFederation() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			writeJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}
		if !d.RBAC.HasPermission(rbac.RequestGroups(r), tenantID, rbac.PermAdmin) {
			writeJSONErrorWithCode(w, r, http.StatusForbidden, CodeForbidden,
				"admin permission required on tenant "+tenantID+" to edit its federation subset")
			return
		}
		email := rbac.RequestEmail(r)

		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			writeJSONError(w, r, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}
		var subset federation.FederationSubset
		if err := json.Unmarshal(body, &subset); err != nil {
			writeJSONError(w, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if subset.Metrics == nil {
			subset.Metrics = []string{}
		}
		// 2-tier containment: the subset must not exceed the whitelist.
		if pv := federation.ValidateSubset(&subset, d.FederationPolicy.Get()); len(pv) > 0 {
			writeValidationErrors(w, r, toViolations(pv))
			return
		}

		yamlBytes, err := yaml.Marshal(&subset)
		if err != nil {
			writeJSONError(w, r, http.StatusInternalServerError, "marshal subset: "+err.Error())
			return
		}
		if err := d.Writer.WriteFederationSubsetFile(tenantID, email, string(yamlBytes)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				writeJSONError(w, r, http.StatusConflict, err.Error())
				return
			}
			writeJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]any{
			"status":       "ok",
			"tenant_id":    tenantID,
			"metric_count": len(subset.Metrics),
		})
	}
}

// readFederationSubset loads conf.d/_federation/<tenantID>.yaml. A
// missing file is not an error — it means the tenant has selected no
// federation metrics yet, which yields an empty subset.
func (d *Deps) readFederationSubset(tenantID string) (*federation.FederationSubset, error) {
	path := filepath.Join(d.ConfigDir, "_federation", tenantID+".yaml")
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return &federation.FederationSubset{Metrics: []string{}}, nil
	}
	if err != nil {
		return nil, err
	}
	return federation.ParseSubset(data)
}
