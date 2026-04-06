package handler

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/policy"
	"github.com/vencil/tenant-api/internal/rbac"
	"gopkg.in/yaml.v3"
)

// PutTenantResponse is the response body for PUT /api/v1/tenants/{id}.
type PutTenantResponse struct {
	Status   string   `json:"status"`
	TenantID string   `json:"tenant_id"`
	Warnings []string `json:"warnings,omitempty"`
}

// PutTenant handles PUT /api/v1/tenants/{id}
//
// Accepts a full ThresholdConfig YAML document (must contain tenants.{id} section).
// Validates, writes to configDir/{id}.yaml, and commits to git.
//
// v2.5.0 Phase C: Domain policy enforcement — writes that violate the
// tenant's domain policy return 403 with details.
//
// @Summary     Update tenant config
// @Description Validates, writes, and commits a tenant's YAML configuration.
// @Tags        tenants
// @Accept      application/yaml
// @Produce     json
// @Param       id    path     string true  "Tenant ID"
// @Param       body  body     string true  "Tenant YAML content"
// @Success     200   {object} PutTenantResponse
// @Failure     400   {object} map[string]string
// @Failure     403   {object} map[string]string
// @Failure     409   {object} map[string]string
// @Failure     500   {object} map[string]string
// @Router      /api/v1/tenants/{id} [put]
func PutTenant(w *gitops.Writer, policyMgr *policy.Manager) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			writeJSONError(rw, http.StatusBadRequest, err.Error())
			return
		}
		email := rbac.RequestEmail(r)

		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20)) // 1 MB limit
		if err != nil {
			writeJSONError(rw, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}

		// v2.5.0: Domain policy enforcement before write
		if policyMgr != nil {
			patch := extractPatchKeys(body, tenantID)
			if violations := policyMgr.CheckWrite(tenantID, patch); len(violations) > 0 {
				writePolicyViolation(rw, violations)
				return
			}
		}

		if err := w.Write(tenantID, email, string(body)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				writeJSONError(rw, http.StatusConflict, err.Error())
				return
			}
			writeJSONError(rw, http.StatusBadRequest, err.Error())
			return
		}

		rw.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(rw).Encode(PutTenantResponse{
			Status:   "ok",
			TenantID: tenantID,
		})
	}
}

// extractPatchKeys extracts flat key-value pairs from a tenant YAML body.
// Converts nested structures like _routing.receiver.type into flat keys.
// Also preserves the original key names for flat-format patches (e.g.,
// "_routing_receiver_type" used by batch operations).
func extractPatchKeys(body []byte, tenantID string) map[string]string {
	result := make(map[string]string)
	var raw struct {
		Tenants map[string]map[string]interface{} `yaml:"tenants"`
	}
	if err := yaml.Unmarshal(body, &raw); err != nil {
		return result
	}
	tenant, ok := raw.Tenants[tenantID]
	if !ok {
		return result
	}
	for k, v := range tenant {
		switch val := v.(type) {
		case string:
			result[k] = val
		case map[string]interface{}:
			// Flatten nested maps (e.g., _routing.receiver.type)
			flattenMap(k, val, result)
		default:
			result[k] = fmt.Sprintf("%v", val)
		}
	}
	return result
}

// flattenMap recursively flattens a nested map into dot-separated keys.
// maxDepth prevents stack overflow from maliciously nested YAML payloads.
func flattenMap(prefix string, m map[string]interface{}, out map[string]string) {
	flattenMapDepth(prefix, m, out, 0)
}

func flattenMapDepth(prefix string, m map[string]interface{}, out map[string]string, depth int) {
	if depth > 100 {
		out[prefix] = fmt.Sprintf("<nested too deep: %d levels>", depth)
		return
	}
	for k, v := range m {
		key := prefix + "." + k
		switch val := v.(type) {
		case string:
			out[key] = val
		case map[string]interface{}:
			flattenMapDepth(key, val, out, depth+1)
		default:
			out[key] = fmt.Sprintf("%v", val)
		}
	}
}

// writePolicyViolation writes a 403 response with domain policy violations.
func writePolicyViolation(w http.ResponseWriter, violations []policy.Violation) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(http.StatusForbidden)
	resp := map[string]interface{}{
		"error":      "domain policy violation",
		"violations": violations,
		"help":       "https://github.com/vencil/vibe-k8s-lab/blob/main/docs/scenarios/advanced-scenarios.md",
		"action":     "Review the _domain_policy.yaml constraints for this tenant's domain. Contact a platform admin to update the policy if this change is necessary.",
	}
	_ = json.NewEncoder(w).Encode(resp)
}
