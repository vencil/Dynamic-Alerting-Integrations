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
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strings"

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

// PutFederationPolicyRequest is the PUT body: the whitelist plus the
// admission-bypass fields. `force` + `reason` let a platform admin
// proceed past a *soft* admission verdict (no recent samples, or a
// PII-looking label name); a hard block — a metric whose series lack
// the tenant label — is never bypassable.
type PutFederationPolicyRequest struct {
	Whitelist []federation.WhitelistEntry `json:"whitelist"`
	Force     bool                        `json:"force"`
	Reason    string                      `json:"reason"`
}

// PutFederationPolicy handles PUT /api/v1/federation/policy — replaces
// the platform federation whitelist.
//
// Platform-wide config: requires admin via a "*"-scoped RBAC group.
// HasPermission(groups, "*", admin) is true only when a rule's tenant
// list literally contains "*" — exactly a platform admin.
//
// Metrics newly added to the whitelist run through the admission
// validator (ADR-020 IV-2e): a hard block (series lacking the tenant
// label) is rejected outright; a soft warning (no recent samples, or a
// PII-looking label name) is rejected unless force=true is set with a
// reason — which is recorded in the git commit message, the durable
// GitOps audit trail.
//
// @Summary     Replace the platform federation whitelist
// @Tags        federation
// @Accept      json
// @Produce     json
// @Param       body body     PutFederationPolicyRequest true "Whitelist + optional admission force/reason"
// @Success     200  {object} map[string]any
// @Failure     400  {object} map[string]any
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
		var req PutFederationPolicyRequest
		if err := json.Unmarshal(body, &req); err != nil {
			writeJSONError(w, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		cfg := federation.FederationPolicyConfig{Whitelist: req.Whitelist}
		if cfg.Whitelist == nil {
			cfg.Whitelist = []federation.WhitelistEntry{}
		}
		if pv := federation.ValidateWhitelist(&cfg); len(pv) > 0 {
			writeValidationErrors(w, r, toViolations(pv))
			return
		}

		// Admission — only metrics being newly ADDED (vs the current
		// whitelist) are checked for data-layer tenant-label enrichment.
		hard, soft := partitionAdmission(d.runFederationAdmission(r.Context(), &cfg))
		if len(hard) > 0 {
			writeErrorEnvelope(w, r, http.StatusBadRequest, ErrorResponse{
				Error: "federation admission: hard block — metric(s) have series without the tenant label and cannot be whitelisted",
				Code:  CodeInvalidBody,
				Extra: map[string]any{"admission": hard},
			})
			return
		}
		if len(soft) > 0 && !req.Force {
			writeErrorEnvelope(w, r, http.StatusBadRequest, ErrorResponse{
				Error: "federation admission: soft warning(s) — re-submit with force=true and a reason to proceed",
				Code:  CodeInvalidBody,
				Extra: map[string]any{"admission": soft},
			})
			return
		}
		trailer := ""
		if len(soft) > 0 { // force is true here
			if strings.TrimSpace(req.Reason) == "" {
				writeJSONError(w, r, http.StatusBadRequest, "force=true requires a non-empty reason")
				return
			}
			trailer = bypassTrailer(email, req.Reason, soft)
			slog.Warn("federation whitelist admission bypassed",
				"user", email, "reason", req.Reason, "metrics", admissionMetrics(soft))
		}

		yamlBytes, err := yaml.Marshal(&cfg)
		if err != nil {
			writeJSONError(w, r, http.StatusInternalServerError, "marshal whitelist: "+err.Error())
			return
		}
		if err := d.Writer.WriteFederationPolicyFile(email, string(yamlBytes), trailer); err != nil {
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

// runFederationAdmission checks each metric being newly added to the
// whitelist (relative to the current snapshot) through the admission
// validator. A nil validator — no --federation-prometheus-url
// configured — means admission is disabled; the write proceeds
// schema-checked only. Already-whitelisted metrics are not re-checked.
func (d *Deps) runFederationAdmission(ctx context.Context, proposed *federation.FederationPolicyConfig) []federation.AdmissionResult {
	if d.AdmissionValidator == nil {
		return nil
	}
	current := make(map[string]bool)
	for _, e := range d.FederationPolicy.Get().Whitelist {
		current[e.Metric] = true
	}
	var results []federation.AdmissionResult
	for _, e := range proposed.Whitelist {
		if current[e.Metric] {
			continue
		}
		res, err := d.AdmissionValidator.Check(ctx, e.Metric)
		if err != nil {
			// An indeterminate result (timeout / backend unreachable)
			// maps to the soft gate: a metric that cannot be queried
			// cannot be proven bad, so it warns rather than hard-blocks.
			res = federation.AdmissionResult{
				Metric: e.Metric,
				State:  federation.AdmissionWarn,
				Reason: "admission check could not be completed: " + err.Error(),
			}
		}
		results = append(results, res)
	}
	return results
}

// partitionAdmission splits admission results into hard blocks and soft
// warnings. A Pass carrying PII-looking label names is a soft warning
// (it wants reviewer judgement), not a clean pass.
func partitionAdmission(results []federation.AdmissionResult) (hard, soft []federation.AdmissionResult) {
	for _, res := range results {
		switch {
		case res.State == federation.AdmissionHardBlock:
			hard = append(hard, res)
		case res.State == federation.AdmissionWarn || len(res.PIILabels) > 0:
			soft = append(soft, res)
		}
	}
	return hard, soft
}

// bypassTrailer renders the git commit-message trailer that records an
// admission `--force` bypass — operator, reason, and the metrics waved
// through — so the audit trail is bound to the commit itself.
func bypassTrailer(user, reason string, soft []federation.AdmissionResult) string {
	return fmt.Sprintf("[Bypass-Validator] Federation admission bypassed by %s.\nReason: %s\nMetrics: %s",
		user, reason, strings.Join(admissionMetrics(soft), ", "))
}

// admissionMetrics extracts the metric names from a result slice.
func admissionMetrics(results []federation.AdmissionResult) []string {
	out := make([]string, len(results))
	for i, res := range results {
		out[i] = res.Metric
	}
	return out
}

// GetTenantFederation handles GET /api/v1/tenants/{id}/federation —
// returns one tenant's *effective* federation metric subset. A tenant
// with no subset file yet gets an empty subset.
//
// Read-repair: the response is the stored subset intersected with the
// live platform whitelist. The stored file can go stale when the
// whitelist shrinks; intersecting on read returns a subset that is
// always consistent with the current policy without rewriting the file
// (ADR-020 IV-2e).
//
// @Summary     Get a tenant's effective federation metric subset
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
		_ = json.NewEncoder(w).Encode(federation.EffectiveSubset(subset, d.FederationPolicy.Get()))
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
