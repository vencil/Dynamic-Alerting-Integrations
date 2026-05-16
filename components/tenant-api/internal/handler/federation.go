package handler

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/federation"
	"github.com/vencil/tenant-api/internal/rbac"
)

// CreateFederationTokenRequest is the body of POST /api/v1/federation/tokens.
type CreateFederationTokenRequest struct {
	TenantID    string `json:"tenant_id" validate:"required,min=1,max=128"`
	Description string `json:"description" validate:"max=256"`
}

// FederationTokenRecord is the listable metadata for one issued token.
// The signed JWT is never included — it is returned exactly once, by
// CreateFederationToken, and is not stored server-side.
type FederationTokenRecord struct {
	TokenID     string `json:"token_id"`
	TenantID    string `json:"tenant_id"`
	IssuedBy    string `json:"issued_by"`
	Description string `json:"description,omitempty"`
	IssuedAt    string `json:"issued_at"`
	ExpiresAt   string `json:"expires_at"`
}

// CreateFederationTokenResponse is the body of a successful POST. The
// `token` field is the compact JWT and is shown only here — it cannot
// be retrieved again.
type CreateFederationTokenResponse struct {
	Token  string                `json:"token"`
	Record FederationTokenRecord `json:"record"`
}

func toFederationTokenRecord(r federation.Record) FederationTokenRecord {
	return FederationTokenRecord{
		TokenID:     r.TokenID,
		TenantID:    r.TenantID,
		IssuedBy:    r.IssuedBy,
		Description: r.Description,
		IssuedAt:    r.IssuedAt.UTC().Format(time.RFC3339),
		ExpiresAt:   r.ExpiresAt.UTC().Format(time.RFC3339),
	}
}

// CreateFederationToken handles POST /api/v1/federation/tokens.
//
// Issues a short-lived RS256 JWT (ADR-020) that the named tenant
// presents to the label-injection proxy to pull its own metrics. The
// caller must hold `admin` on the target tenant — federation is data
// egress, a higher bar than config write. The tenant ID is in the
// body, so that check is here rather than in route middleware.
//
// @Summary     Issue a tenant federation token
// @Description Mints a short-lived RS256 JWT for the named tenant (ADR-020). Requires admin permission on the tenant. The signed token is returned once and is not retrievable afterwards.
// @Tags        federation
// @Accept      json
// @Produce     json
// @Param       body body     CreateFederationTokenRequest true "Token request"
// @Success     201  {object} CreateFederationTokenResponse
// @Failure     400  {object} map[string]string
// @Failure     403  {object} map[string]string
// @Failure     409  {object} map[string]string
// @Failure     500  {object} map[string]string
// @Router      /api/v1/federation/tokens [post]
func (d *Deps) CreateFederationToken() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
		if err != nil {
			writeJSONError(w, r, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}

		var req CreateFederationTokenRequest
		if err := json.Unmarshal(body, &req); err != nil {
			writeJSONError(w, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if violations := validateStructTags(&req); len(violations) > 0 {
			writeValidationErrors(w, r, violations)
			return
		}
		// Reject path-traversal / non-simple tenant IDs — the same gate
		// the other tenant-scoped handlers use, applied here for
		// consistency and defence-in-depth (the RBAC check below is the
		// real bar).
		if err := ValidateTenantID(req.TenantID); err != nil {
			writeJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		// Federation token issuance is data egress — require admin on
		// the target tenant (ADR-020 Wave-0 decision 5).
		if !d.RBAC.HasPermission(rbac.RequestGroups(r), req.TenantID, rbac.PermAdmin) {
			writeJSONErrorWithCode(w, r, http.StatusForbidden, CodeForbidden,
				"admin permission required on tenant "+req.TenantID+" to issue a federation token")
			return
		}

		token, rec, err := d.Federation.Issue(req.TenantID, rbac.RequestEmail(r), req.Description)
		if err != nil {
			if errors.Is(err, federation.ErrTokenLimitReached) {
				writeJSONErrorWithCode(w, r, http.StatusConflict, CodeConflict, err.Error())
				return
			}
			writeJSONError(w, r, http.StatusInternalServerError, "issue federation token: "+err.Error())
			return
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(CreateFederationTokenResponse{
			Token:  token,
			Record: toFederationTokenRecord(rec),
		})
	}
}

// ListFederationTokens handles GET /api/v1/federation/tokens?tenant_id=X.
//
// @Summary     List a tenant's federation tokens
// @Description Returns the non-expired federation token records for the tenant named by the tenant_id query parameter. Requires admin permission on that tenant. The signed JWTs themselves are not returned.
// @Tags        federation
// @Produce     json
// @Param       tenant_id query string true "Tenant ID"
// @Success     200 {array}  FederationTokenRecord
// @Failure     400 {object} map[string]string
// @Failure     403 {object} map[string]string
// @Router      /api/v1/federation/tokens [get]
func (d *Deps) ListFederationTokens() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := r.URL.Query().Get("tenant_id")
		if tenantID == "" {
			writeJSONError(w, r, http.StatusBadRequest, "query parameter tenant_id is required")
			return
		}
		if err := ValidateTenantID(tenantID); err != nil {
			writeJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		if !d.RBAC.HasPermission(rbac.RequestGroups(r), tenantID, rbac.PermAdmin) {
			writeJSONErrorWithCode(w, r, http.StatusForbidden, CodeForbidden,
				"admin permission required on tenant "+tenantID)
			return
		}

		recs := d.Federation.List(tenantID)
		resp := make([]FederationTokenRecord, 0, len(recs))
		for _, rec := range recs {
			resp = append(resp, toFederationTokenRecord(rec))
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(resp)
	}
}

// DeleteFederationToken handles DELETE /api/v1/federation/tokens/{id}.
//
// Removes the bookkeeping record for a token. Per ADR-020 there is no
// server-side revocation list: a still-valid JWT keeps working until
// its expiry even after this call. DELETE exists so operators can tidy
// the listing and so a future revocation feature has a hook.
//
// @Summary     Delete a federation token record
// @Description Removes a federation token's bookkeeping record. NOTE: per ADR-020 there is no server-side revocation — a still-valid JWT remains usable until it expires. Requires admin permission on the token's tenant.
// @Tags        federation
// @Produce     json
// @Param       id path string true "Token ID"
// @Success     200 {object} map[string]string
// @Failure     403 {object} map[string]string
// @Failure     404 {object} map[string]string
// @Failure     500 {object} map[string]string
// @Router      /api/v1/federation/tokens/{id} [delete]
func (d *Deps) DeleteFederationToken() http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tokenID := chi.URLParam(r, "id")

		rec, ok := d.Federation.Get(tokenID)
		if !ok {
			writeJSONError(w, r, http.StatusNotFound, "federation token not found: "+tokenID)
			return
		}

		if !d.RBAC.HasPermission(rbac.RequestGroups(r), rec.TenantID, rbac.PermAdmin) {
			writeJSONErrorWithCode(w, r, http.StatusForbidden, CodeForbidden,
				"admin permission required on tenant "+rec.TenantID)
			return
		}

		deleted, err := d.Federation.Delete(tokenID)
		if err != nil {
			writeJSONError(w, r, http.StatusInternalServerError, "delete federation token: "+err.Error())
			return
		}
		if !deleted {
			writeJSONError(w, r, http.StatusNotFound, "federation token not found: "+tokenID)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"status":   "ok",
			"token_id": tokenID,
		})
	}
}
