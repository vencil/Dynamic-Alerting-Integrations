package handler

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/platform"
	"github.com/vencil/tenant-api/internal/rbac"
	"gopkg.in/yaml.v3"
)

// PutTenantResponse is the response body for PUT /api/v1/tenants/{id}.
type PutTenantResponse struct {
	Status   string   `json:"status"`
	TenantID string   `json:"tenant_id"`
	PRURL    string   `json:"pr_url,omitempty"`
	PRNumber int      `json:"pr_number,omitempty"`
	Message  string   `json:"message,omitempty"`
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
// v2.6.0 Phase C: PR-based write-back (ADR-011) — when writeMode is PR,
// creates a feature branch and PR/MR instead of direct commit.
// Supports both GitHub PRs and GitLab MRs via platform.Client interface.
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
func PutTenant(d *Deps) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			WriteJSONError(rw, r,http.StatusBadRequest, err.Error())
			return
		}
		email := rbac.RequestEmail(r)

		body, err := io.ReadAll(io.LimitReader(r.Body, d.MaxBody()))
		if err != nil {
			WriteJSONError(rw, r,http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}

		// v2.5.0: Domain policy enforcement before write
		if d.Policy != nil {
			patch := extractPatchKeys(body, tenantID)
			if violations := d.Policy.CheckWrite(tenantID, patch); len(violations) > 0 {
				writePolicyViolation(rw, r,violations)
				return
			}
		}

		// v2.6.0: PR-based write-back mode (ADR-011) — supports GitHub + GitLab
		if d.WriteMode.IsPRMode() && d.PRClient != nil && d.PRTracker != nil {
			// Atomically claim the tenant. Returns false if a PR/MR is
			// already pending OR another request is mid-creation — both map
			// to 409. The claim (not the async poll cache) is what makes two
			// concurrent same-tenant writes safe; see Tracker.ClaimTenant.
			if !d.PRTracker.ClaimTenant(tenantID) {
				existingPR, _ := d.PRTracker.PendingPRForTenant(tenantID)
				WriteErrorEnvelope(rw, r, http.StatusConflict, ErrorResponse{
					Error: "pending_pr_exists",
					Code:  CodePendingPR,
					Extra: map[string]any{
						"existing_pr_url": existingPR.WebURL,
						"pr_number":       existingPR.Number,
						"message":         fmt.Sprintf("A pending PR/MR for %s already exists or is being created. Merge or close it first.", tenantID),
					},
				})
				return
			}
			// Release the claim on ANY exit that isn't a successful registration —
			// every failure return AND a recovered panic. On success RegisterPR
			// clears the in-flight claim and sets byTenant (the durable dedup), so
			// this deferred release then no-ops; on failure/panic it frees the
			// tenant for retry instead of leaving a zombie 409-until-pod-restart.
			defer d.PRTracker.ReleaseClaim(tenantID)

			// Create feature branch + commit
			result, err := d.Writer.WritePR(tenantID, email, string(body))
			if err != nil {
				WriteJSONError(rw, r, http.StatusInternalServerError, "PR write failed: "+err.Error())
				return
			}

			// Create PR/MR via platform client + register in tracker.
			// PR-6/11: shared with BatchTenants via createPRAndRegister.
			prTitle := fmt.Sprintf("[tenant-api] Update %s configuration", tenantID)
			prBody := fmt.Sprintf("**Operator:** %s\n**Source:** tenant-manager UI\n**Tenant:** %s", email, tenantID)
			pr, err := createPRAndRegister(d,
				prTitle, prBody, result.BranchName,
				[]string{"tenant-api", "auto-generated"},
				[]string{tenantID},
			)
			if err != nil {
				// Claim is released by the deferred ReleaseClaim above. A 403
				// from the forge means the token passed ValidateToken (/user)
				// but lacks write scope; surface it as a clean 403, never a 500
				// (so da-portal can trigger its permission-error UI rather than
				// a generic failure).
				provider := d.PRClient.ProviderName()
				if errors.Is(err, platform.ErrForbidden) {
					WriteJSONErrorWithCode(rw, r, http.StatusForbidden, CodeForbidden,
						fmt.Sprintf("insufficient %s permissions to open PR/MR — the configured token lacks write scope", provider))
					return
				}
				WriteJSONError(rw, r, http.StatusServiceUnavailable, fmt.Sprintf("%s PR/MR creation failed: %s", provider, err.Error()))
				return
			}

			rw.Header().Set("Content-Type", "application/json")
			_ = json.NewEncoder(rw).Encode(PutTenantResponse{
				Status:   "pending_review",
				TenantID: tenantID,
				PRURL:    pr.WebURL,
				PRNumber: pr.Number,
				Message:  "PR/MR created. Configuration will take effect after merge.",
			})
			return
		}

		// Default: direct commit-on-write (ADR-009)
		if err := d.Writer.Write(tenantID, email, string(body)); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				WriteJSONError(rw, r,http.StatusConflict, err.Error())
				return
			}
			WriteJSONError(rw, r,http.StatusBadRequest, err.Error())
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

// writePolicyViolation lives in errors.go (PR-9/11 unification).
