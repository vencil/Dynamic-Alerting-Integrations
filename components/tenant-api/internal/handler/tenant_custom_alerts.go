package handler

// ============================================================
// PUT /api/v1/tenants/{id}/custom-alerts — v2.9.0 (ADR-024 §S6b-2, #741)
// ============================================================
//
// Server-side, comment-preserving write of a tenant's `_custom_alerts`
// list. The portal RecipeBuilder modal sends the desired full recipe
// array as JSON; the server merges it into the (human-authored)
// tenant.yaml via yaml.Node AST surgery (preserving comments — Reef 1),
// validates with the same S5 Go validator as every other write boundary,
// and commits via the existing gitops.Writer.
//
// Why a sub-resource endpoint (not the full PUT /{id}): PUT is full-
// overlay and the portal has no YAML serializer, so the client would
// have to reconstruct + re-serialise the whole tenant doc (the blocker).
// Here the client sends only a JSON array; the server owns the robust
// YAML round-trip. See ADR-024 §S6b-2.

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"os"
	"path/filepath"

	"github.com/go-chi/chi/v5"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"

	"github.com/vencil/tenant-api/internal/customalerts"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

// PutCustomAlertsRequest is the body of PUT /tenants/{id}/custom-alerts.
type PutCustomAlertsRequest struct {
	// CustomAlerts is the desired FULL recipe list (collection-replace —
	// the client owns the array, having loaded it via GET). A POINTER so an
	// ABSENT field (nil) is rejected — a truncated/buggy request must NOT
	// be read as "delete every recipe" (self-review F1, a data-loss
	// footgun). An explicit empty array `[]` IS the intentional delete-all
	// (Reef 2).
	CustomAlerts *[]map[string]any `json:"custom_alerts"`
	// BaseHash is the source_hash the client got from GET. REQUIRED —
	// optimistic concurrency is the safe default, not opt-in: an empty
	// base_hash would silently disable clobber protection (self-review F2).
	BaseHash string `json:"base_hash"`
}

// PutCustomAlertsResponse is the success body.
type PutCustomAlertsResponse struct {
	Status     string `json:"status"`
	TenantID   string `json:"tenant_id"`
	SourceHash string `json:"source_hash"` // new hash post-write (for the next edit)
}

// PutTenantCustomAlerts handles PUT /api/v1/tenants/{id}/custom-alerts.
//
// @Summary     Replace a tenant's custom-alert recipes
// @Description Merges the supplied recipe array into the tenant's
// @Description `_custom_alerts` (comment-preserving AST edit), validates
// @Description (S5 Go validator), and commits. Optimistic concurrency via
// @Description base_hash (409 on drift). Empty array deletes the key.
// @Tags        tenants
// @Accept      json
// @Produce     json
// @Param       id    path     string                 true "Tenant ID"
// @Param       body  body     PutCustomAlertsRequest true "Desired recipe list + base_hash"
// @Success     200   {object} PutCustomAlertsResponse
// @Failure     400   {object} ErrorResponse
// @Failure     404   {object} ErrorResponse
// @Failure     409   {object} ErrorResponse
// @Failure     501   {object} ErrorResponse
// @Failure     503   {object} ErrorResponse
// @Router      /api/v1/tenants/{id}/custom-alerts [put]
func PutTenantCustomAlerts(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		// MVP scope (ADR-024 §S6b-2): the in-UI write targets DIRECT
		// write-back mode. In PR/MR mode the recipe author uses the
		// standalone recipe-builder + their normal PR flow (S6b-1).
		if d.WriteMode.IsPRMode() {
			WriteJSONError(w, r, http.StatusNotImplemented,
				"custom-alert editing via the UI is not yet supported in PR write-back mode; "+
					"use the standalone recipe builder and your GitOps PR flow")
			return
		}

		body, err := io.ReadAll(io.LimitReader(r.Body, d.MaxBody()))
		if err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}
		var req PutCustomAlertsRequest
		if err := json.Unmarshal(body, &req); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, "invalid JSON body: "+err.Error())
			return
		}
		// F1: an absent/null `custom_alerts` must not be read as delete-all.
		if req.CustomAlerts == nil {
			WriteJSONError(w, r, http.StatusBadRequest,
				"custom_alerts is required (send an explicit array; [] deletes all)")
			return
		}
		// F2: base_hash is required — optimistic concurrency is the safe
		// default. Clients GET the tenant (which returns source_hash) first.
		if req.BaseHash == "" {
			WriteJSONError(w, r, http.StatusBadRequest,
				"base_hash is required; GET the tenant first and echo its source_hash")
			return
		}

		// Load the current tenant file (must exist — recipes are authored
		// against an existing tenant).
		filePath := filepath.Join(d.ConfigDir, tenantID+".yaml")
		raw, err := os.ReadFile(filePath)
		if os.IsNotExist(err) {
			WriteJSONError(w, r, http.StatusNotFound, "tenant not found: "+tenantID)
			return
		}
		if err != nil {
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}

		// Optimistic concurrency (Reef 3): reject if the file moved under us.
		// NB (self-review F4): this check is outside the writer's lock, so a
		// narrow TOCTOU remains — two requests that load the SAME base_hash
		// and submit within the read→write window can still last-write-wins.
		// This catches the common case (a stale load) and is strictly safer
		// than the existing PutTenant (no OCC at all); full atomicity (re-
		// check under the writer lock) is a future hardening.
		currentHash := cfg.ComputeSourceHash(raw)
		if req.BaseHash != currentHash {
			WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{
				Error: "the tenant configuration was updated by someone else; refresh and retry",
				Code:  CodeConflict,
				Extra: map[string]any{"current_source_hash": currentHash},
			})
			return
		}

		// Comment-preserving AST merge (Reef 1 / Reef 2).
		merged, err := customalerts.MergeCustomAlerts(string(raw), tenantID, *req.CustomAlerts)
		if err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, "merge custom alerts: "+err.Error())
			return
		}

		// Pre-validate for STRUCTURED violations (Reef 4): the S5 validator
		// runs over the whole array, so a pre-existing bad recipe also
		// surfaces here — return all of them by name/index so the UI can
		// point at the offending rule, not just reject opaquely.
		var mcfg cfg.ThresholdConfig
		if err := yaml.Unmarshal([]byte(merged), &mcfg); err != nil {
			// The merge produces YAML via the encoder, so this should be
			// unreachable — if it fires, our merge logic is at fault (a
			// server bug), not the client's input. Fail-fast as 500, not a
			// misleading 400.
			WriteJSONError(w, r, http.StatusInternalServerError,
				"internal error: merged config is not parseable: "+err.Error())
			return
		}
		if viol := cfg.ValidateTenantCustomAlerts(tenantID, mcfg.Tenants[tenantID], cfg.MaxCustomRecipesDefault); len(viol) > 0 {
			violations := make([]Violation, 0, len(viol))
			for _, v := range viol {
				violations = append(violations, Violation{Field: "_custom_alerts", Reason: v})
			}
			WriteValidationErrors(w, r, violations)
			return
		}

		// Commit via the shared writer (re-validates schema + custom alerts,
		// attributes the commit to the operator).
		email := rbac.RequestEmail(r)
		if err := d.Writer.Write(tenantID, email, merged); err != nil {
			if errors.Is(err, gitops.ErrConflict) {
				WriteJSONError(w, r, http.StatusConflict, err.Error())
				return
			}
			// Forge degradation (TRK-318): the in-lock base fetch timed out;
			// the write never touched a stale base, so a retry is safe →
			// surface a retry-hinting 503, not a 400 (matches PutTenant).
			if errors.Is(err, gitops.ErrForgeDegraded) {
				writeForgeDegraded(w, r)
				return
			}
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(PutCustomAlertsResponse{
			Status:     "success",
			TenantID:   tenantID,
			SourceHash: cfg.ComputeSourceHash([]byte(merged)),
		})
	}
}
