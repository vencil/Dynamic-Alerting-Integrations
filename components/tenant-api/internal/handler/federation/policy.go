package federation

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
	"sync"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/federation/fedpolicy"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"gopkg.in/yaml.v3"
)

// toViolations adapts federation schema failures to the handler's
// validation-error shape (both are {field, reason} — a plain copy).
func toViolations(pv []fedpolicy.PolicyViolation) []handler.Violation {
	out := make([]handler.Violation, len(pv))
	for i, v := range pv {
		out[i] = handler.Violation{Field: v.Field, Reason: v.Reason}
	}
	return out
}

// GetFederationPolicy handles GET /api/v1/federation/policy — returns
// the platform federation whitelist.
//
// @Summary     Get the platform federation whitelist
// @Tags        federation
// @Produce     json
// @Success     200 {object} fedpolicy.Config
// @Router      /api/v1/federation/policy [get]
func GetFederationPolicy(d *handler.Deps) http.HandlerFunc {
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
	Whitelist []fedpolicy.WhitelistEntry `json:"whitelist"`
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
func PutFederationPolicy(d *handler.Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		if !d.RBAC.HasPermission(rbac.RequestGroups(r), "*", rbac.PermAdmin) {
			handler.WriteJSONErrorWithCode(w, r, http.StatusForbidden, handler.CodeForbidden,
				"platform admin permission required to edit the federation whitelist")
			return
		}
		email := rbac.RequestEmail(r)

		body, err := io.ReadAll(io.LimitReader(r.Body, d.MaxBody()))
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}
		var req PutFederationPolicyRequest
		if err := json.Unmarshal(body, &req); err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		cfg := fedpolicy.Config{Whitelist: req.Whitelist}
		if cfg.Whitelist == nil {
			cfg.Whitelist = []fedpolicy.WhitelistEntry{}
		}
		if pv := fedpolicy.ValidateWhitelist(&cfg); len(pv) > 0 {
			handler.WriteValidationErrors(w, r, toViolations(pv))
			return
		}

		// Admission — metrics being newly ADDED (vs the current
		// whitelist) are checked for data-layer tenant-label enrichment.
		// Skipped entirely when no validator is configured.
		trailer := ""
		if d.AdmissionValidator != nil {
			added := addedFederationMetrics(d.FederationPolicy.Get(), &cfg)
			if len(added) > maxNewMetricsPerRequest {
				handler.WriteJSONError(w, r, http.StatusBadRequest, fmt.Sprintf(
					"too many new metrics in one request (%d; max %d) — split the change into smaller PUTs so admission validation stays within the request timeout",
					len(added), maxNewMetricsPerRequest))
				return
			}
			hard, soft := partitionAdmission(runAdmissionChecks(d, r.Context(), added))
			if len(hard) > 0 {
				handler.WriteErrorEnvelope(w, r, http.StatusBadRequest, handler.ErrorResponse{
					Error: "federation admission: hard block — metric(s) have data but no series carries the tenant label and cannot be whitelisted",
					Code:  handler.CodeInvalidBody,
					Extra: map[string]any{"admission": hard},
				})
				return
			}
			if len(soft) > 0 && !req.Force {
				handler.WriteErrorEnvelope(w, r, http.StatusBadRequest, handler.ErrorResponse{
					Error: "federation admission: soft warning(s) — re-submit with force=true and a reason to proceed",
					Code:  handler.CodeInvalidBody,
					Extra: map[string]any{"admission": soft},
				})
				return
			}
			if len(soft) > 0 { // force is true here
				if strings.TrimSpace(req.Reason) == "" {
					handler.WriteJSONError(w, r, http.StatusBadRequest, "force=true requires a non-empty reason")
					return
				}
				trailer = bypassTrailer(email, req.Reason, soft)
				slog.Warn("federation whitelist admission bypassed",
					"user", email, "reason", req.Reason, "metrics", admissionMetrics(soft))
			}
		}

		// Point of no return — the next call writes to git. If the
		// request context is already done (the server's 30s timeout
		// fired, or the client disconnected), bail: a 504 has already
		// gone back, and committing now would leave git state diverged
		// from what the caller believes happened (a zombie write).
		if r.Context().Err() != nil {
			return
		}

		yamlBytes, err := yaml.Marshal(&cfg)
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusInternalServerError, "marshal whitelist: "+err.Error())
			return
		}
		if err := d.Writer.WriteFederationPolicyFile(email, string(yamlBytes), trailer); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				handler.WriteJSONError(w, r, http.StatusConflict, err.Error())
				return
			}
			handler.WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
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

// maxNewMetricsPerRequest caps how many metrics a single whitelist PUT
// may newly add. Each addition costs an admission check; without a cap
// a very large PUT could run past the server's request timeout, after
// which every still-pending check fails fast and degrades to a
// misleading Warn — training the operator to blanket-`--force` (Gemini
// timeout-avalanche review). 30 additions at admissionConcurrency=8
// stay well inside a 30s budget.
const maxNewMetricsPerRequest = 30

// admissionConcurrency caps how many admission checks run in parallel.
// Each check is two cheap /api/v1/series index queries; a bounded
// fan-out keeps a large whitelist PUT well inside the request timeout
// without opening an unreasonable number of connections to the backend.
const admissionConcurrency = 8

// addedFederationMetrics returns the metric names in proposed that are
// not already in current — the set a whitelist PUT newly introduces.
func addedFederationMetrics(current, proposed *fedpolicy.Config) []string {
	have := make(map[string]bool, len(current.Whitelist))
	for _, e := range current.Whitelist {
		have[e.Metric] = true
	}
	var added []string
	for _, e := range proposed.Whitelist {
		if !have[e.Metric] {
			added = append(added, e.Metric)
		}
	}
	return added
}

// runAdmissionChecks runs the admission validator over metrics
// concurrently (bounded by admissionConcurrency): a metric's check can
// take up to the validator's per-check timeout, so a batch must not run
// strictly sequentially. Returns nil when the validator is disabled or
// metrics is empty.
func runAdmissionChecks(d *handler.Deps, ctx context.Context, metrics []string) []fedpolicy.AdmissionResult {
	if d.AdmissionValidator == nil || len(metrics) == 0 {
		return nil
	}

	// Each goroutine writes its own distinct index of `results`, so the
	// slice needs no lock and the output order matches `metrics`.
	results := make([]fedpolicy.AdmissionResult, len(metrics))
	sem := make(chan struct{}, admissionConcurrency)
	var wg sync.WaitGroup
	for i, metric := range metrics {
		wg.Add(1)
		go func(i int, metric string) {
			defer wg.Done()
			sem <- struct{}{}
			defer func() { <-sem }()
			res, err := d.AdmissionValidator.Check(ctx, metric)
			if err != nil {
				// An indeterminate result (timeout / backend
				// unreachable) maps to the soft gate: a metric that
				// cannot be queried cannot be proven bad, so it warns
				// rather than hard-blocks.
				res = fedpolicy.AdmissionResult{
					Metric: metric,
					State:  fedpolicy.AdmissionWarn,
					Reason: "admission check could not be completed: " + err.Error(),
				}
			}
			results[i] = res
		}(i, metric)
	}
	wg.Wait()
	return results
}

// partitionAdmission splits admission results into hard blocks and soft
// warnings. A Pass carrying PII-looking label names is a soft warning
// (it wants reviewer judgement), not a clean pass.
func partitionAdmission(results []fedpolicy.AdmissionResult) (hard, soft []fedpolicy.AdmissionResult) {
	for _, res := range results {
		switch {
		case res.State == fedpolicy.AdmissionHardBlock:
			hard = append(hard, res)
		case res.State == fedpolicy.AdmissionWarn || len(res.PIILabels) > 0:
			soft = append(soft, res)
		}
	}
	return hard, soft
}

// bypassTrailer renders the git commit-message trailer that records an
// admission `--force` bypass — operator, reason, and the metrics waved
// through — so the audit trail is bound to the commit itself.
//
// user and reason are sanitised first: a caller-supplied CR/LF in the
// reason would otherwise forge extra trailer lines (e.g. a fake
// `Approved-By:`) and break the integrity of the audit record.
func bypassTrailer(user, reason string, soft []fedpolicy.AdmissionResult) string {
	return fmt.Sprintf("[Bypass-Validator] Federation admission bypassed by %s.\nReason: %s\nMetrics: %s",
		sanitizeTrailerField(user), sanitizeTrailerField(reason),
		strings.Join(admissionMetrics(soft), ", "))
}

// sanitizeTrailerField collapses CR and LF in a value to spaces so it
// cannot inject newlines into a git commit-message trailer.
func sanitizeTrailerField(s string) string {
	return strings.NewReplacer("\r", " ", "\n", " ").Replace(strings.TrimSpace(s))
}

// admissionMetrics extracts the metric names from a result slice.
func admissionMetrics(results []fedpolicy.AdmissionResult) []string {
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
// @Success     200 {object} fedpolicy.Subset
// @Failure     400 {object} map[string]string
// @Router      /api/v1/tenants/{id}/federation [get]
func GetTenantFederation(d *handler.Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := handler.ValidateTenantID(tenantID); err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}
		subset, err := readFederationSubset(d, tenantID)
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusInternalServerError, "read federation subset: "+err.Error())
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(fedpolicy.EffectiveSubset(subset, d.FederationPolicy.Get()))
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
// @Param       body body fedpolicy.Subset true "Metric subset"
// @Success     200  {object} map[string]any
// @Failure     400  {object} map[string]string
// @Failure     403  {object} map[string]string
// @Failure     409  {object} map[string]string
// @Router      /api/v1/tenants/{id}/federation [put]
func PutTenantFederation(d *handler.Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := handler.ValidateTenantID(tenantID); err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}
		if !d.RBAC.HasPermission(rbac.RequestGroups(r), tenantID, rbac.PermAdmin) {
			handler.WriteJSONErrorWithCode(w, r, http.StatusForbidden, handler.CodeForbidden,
				"admin permission required on tenant "+tenantID+" to edit its federation subset")
			return
		}
		email := rbac.RequestEmail(r)

		body, err := io.ReadAll(io.LimitReader(r.Body, d.MaxBody()))
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}
		var subset fedpolicy.Subset
		if err := json.Unmarshal(body, &subset); err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if subset.Metrics == nil {
			subset.Metrics = []string{}
		}
		// 2-tier containment: the subset must not exceed the whitelist.
		if pv := fedpolicy.ValidateSubset(&subset, d.FederationPolicy.Get()); len(pv) > 0 {
			handler.WriteValidationErrors(w, r, toViolations(pv))
			return
		}

		yamlBytes, err := yaml.Marshal(&subset)
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusInternalServerError, "marshal subset: "+err.Error())
			return
		}
		if err := d.Writer.WriteFederationSubsetFile(tenantID, email, string(yamlBytes)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				handler.WriteJSONError(w, r, http.StatusConflict, err.Error())
				return
			}
			handler.WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
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
func readFederationSubset(d *handler.Deps, tenantID string) (*fedpolicy.Subset, error) {
	path := filepath.Join(d.ConfigDir, "_federation", tenantID+".yaml")
	data, err := os.ReadFile(path)
	if errors.Is(err, os.ErrNotExist) {
		return &fedpolicy.Subset{Metrics: []string{}}, nil
	}
	if err != nil {
		return nil, err
	}
	return fedpolicy.ParseSubset(data)
}
