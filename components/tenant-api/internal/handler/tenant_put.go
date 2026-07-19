package handler

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/gitops"
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
// Accepts a tenant-only YAML document — the conf.d/{id}.yaml shape, a
// `tenants:` block containing the {id} section (NOT the platform defaults).
// The body is committed verbatim, so any top-level key other than `tenants`
// (e.g. a stray `defaults:` / `state_filters:` / `profiles:`, or a typo) is
// rejected with 400 — mirroring tenant-config.schema.json's
// additionalProperties:false (#705). Validation merges the on-disk
// _defaults.yaml so a tenant-only body's metric keys resolve against the
// inherited defaults — identical to GET /{id} and POST /{id}/validate
// (ADR-024 PR4 / #704). Writes to configDir/{id}.yaml and commits to git.
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
// @Param       X-DA-Write-Source header string false "Attribute the PR to a non-UI write source. Allowlisted: threshold-governance (#656). Omit for tenant-manager UI."
// @Success     200   {object} PutTenantResponse
// @Failure     400   {object} ErrorResponse
// @Failure     403   {object} ErrorResponse
// @Failure     409   {object} ErrorResponse
// @Failure     500   {object} ErrorResponse
// @Failure     503   {object} ErrorResponse
// @Router      /api/v1/tenants/{id} [put]
func PutTenant(d *Deps) http.HandlerFunc {
	return func(rw http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			WriteJSONError(rw, r, http.StatusBadRequest, err.Error())
			return
		}
		// ADR-027 / LD-6 P4b: org-scope-aware write gate, top of handler —
		// BEFORE the body is read, so a denied caller triggers no side
		// effect on either write path (direct commit or PR mode) and learns
		// nothing from policy-violation details.
		if !RequireOrgWrite(rw, r, d, tenantID, rbac.PermWrite) {
			return
		}
		email := rbac.RequestEmail(r)

		body, ok := readLimitedBody(rw, r, d)
		if !ok {
			return
		}

		// v2.5.0: Domain policy enforcement before write
		if d.Policy != nil {
			patch := extractPatchKeys(body, tenantID)
			if violations := d.Policy.CheckWrite(tenantID, patch); len(violations) > 0 {
				writePolicyViolation(rw, r, violations)
				return
			}
		}

		// v2.6.0: PR-based write-back mode (ADR-011) — supports GitHub + GitLab
		if d.WriteMode.IsPRMode() && d.PRClient != nil && d.PRTracker != nil {
			putTenantPRMode(d, rw, r, tenantID, email, string(body))
			return
		}

		// Default: direct commit-on-write (ADR-009)
		if err := d.Writer.Write(r.Context(), tenantID, email, string(body)); err != nil {
			if errors.Is(err, gitops.ErrWriteOverloaded) {
				WriteOverloaded(rw, r)
				return
			}
			if errors.Is(err, gitops.ErrConflict) {
				WriteJSONError(rw, r, http.StatusConflict, err.Error())
				return
			}
			WriteJSONError(rw, r, http.StatusBadRequest, err.Error())
			return
		}

		writeJSON(rw, http.StatusOK, PutTenantResponse{
			Status:   "ok",
			TenantID: tenantID,
		})
	}
}

// putTenantPRMode handles a single-tenant PUT in PR write-back mode (ADR-011):
// atomically claim the tenant (409 on a pending/in-flight PR), write the config
// to a feature branch, then open the PR/MR and register it. Split out of
// PutTenant (Cycle 10 refactor) to keep the handler readable — behavior is
// unchanged. The caller must have verified IsPRMode && PRClient != nil &&
// PRTracker != nil. Always writes a response. The deferred ReleaseClaim fires on
// this function's return, which is immediately before the caller returns — same
// timing as when the defer lived in the handler.
func putTenantPRMode(d *Deps, rw http.ResponseWriter, r *http.Request, tenantID, email, yamlContent string) {
	// Atomically claim the tenant. Returns false if a PR/MR is
	// already pending OR another request is mid-creation — both map
	// to 409. The claim (not the async poll cache) is what makes two
	// concurrent same-tenant writes safe; see Tracker.ClaimTenant.
	//
	// #644: if the claim fails BECAUSE the byTenant cache says a PR is
	// open (HasPendingPR is true — vs the in-flight-claim case where
	// HasPendingPR is false), the cache may be up to ~30 s stale after a
	// merge → spurious 409. Force a single bounded refresh and retry the
	// claim ONCE. The 2 s ctx stops a degraded forge from extending the
	// 409 response latency (a slower refresh continues in background and
	// populates the cache for the next request).
	claimed := d.PRTracker.ClaimTenant(tenantID)
	if !claimed && d.PRTracker.HasPendingPR(tenantID) {
		// Detached from r.Context() on purpose (#644): if the client cancelled
		// (browser close / TCP RST) r.Context() is already Done by the time we
		// get here → WithTimeout(r.Context(), …) returns an immediately-Done
		// ctx → RefreshNow would skip Sync → we'd return the stale 409 the fix
		// is meant to kill. Request-cancel-protection is NOT load-bearing here
		// (the 2 s bound + the in-background Sync continuation already cover a
		// degraded forge). context.Background() ensures the refresh actually
		// happens for the live-client case.
		refreshCtx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		d.PRTracker.RefreshNow(refreshCtx)
		cancel()
		claimed = d.PRTracker.ClaimTenant(tenantID)
	}
	if !claimed {
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
	result, err := d.Writer.WritePR(r.Context(), tenantID, email, yamlContent)
	if err != nil {
		// TRK-320 ErrWriteOverloaded / TRK-318 ErrForgeDegraded → canonical
		// retry-hinting 503s (shared with the batch path).
		if writeWriteFlowError(rw, r, err) {
			return
		}
		// #795 F1: malformed body is a CLIENT error → 400 (matches the
		// direct-write path), not a server 500.
		if errors.Is(err, gitops.ErrValidation) {
			WriteJSONError(rw, r, http.StatusBadRequest, err.Error())
			return
		}
		// Anything else is an unexpected git failure → generic 500.
		WriteJSONError(rw, r, http.StatusInternalServerError, "PR write failed: "+err.Error())
		return
	}

	// Create PR/MR via platform client + register in tracker.
	// PR-6/11: shared with BatchTenants via createPRAndRegister.
	// #656: attribute the PR to its declared write source (UI by default; an
	// allowlisted X-DA-Write-Source header routes automation writes like the
	// threshold governance loop onto their own label/title/Source channel).
	ws := resolveWriteSource(r)
	prTitle := ws.titleSingle(tenantID)
	prBody := fmt.Sprintf("**Operator:** %s\n**Source:** %s\n**Tenant:** %s", email, ws.sourceLine, tenantID)
	pr, err := createPRAndRegister(d,
		prTitle, prBody, result.BranchName,
		ws.labels(),
		[]string{tenantID},
	)
	if err != nil {
		// Claim is released by the deferred ReleaseClaim above. Forbidden →
		// clean 403 (never a 500, so da-portal shows a permission error),
		// circuit-open → sanitized 503, else generic 503. Shared with batch.
		writeForgeCreateError(rw, r, d.PRClient.ProviderName(), err)
		return
	}

	writeJSON(rw, http.StatusOK, PutTenantResponse{
		Status:   "pending_review",
		TenantID: tenantID,
		PRURL:    pr.WebURL,
		PRNumber: pr.Number,
		Message:  "PR/MR created. Configuration will take effect after merge.",
	})
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
