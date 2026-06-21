package federation

import (
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"time"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/federation/token"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/handler"
	"github.com/vencil/tenant-api/internal/rbac"
)

// CreateFederationTokenRequest is the body of POST /api/v1/federation/tokens.
//
// Capability selects the federation plane (ADR-021): "metrics" (default,
// the ADR-020 behaviour — metrics audience, no account_id) or "logs" (logs
// audience + the tenant's account_id embedded). An ABSENT capability means
// metrics, so a pre-ADR-021 client body issues exactly the token it always
// did — full back-compat.
type CreateFederationTokenRequest struct {
	TenantID    string `json:"tenant_id" validate:"required,min=1,max=128"`
	Description string `json:"description" validate:"max=256"`
	Capability  string `json:"capability" validate:"omitempty,oneof=metrics logs"`
}

// FederationTokenRecord is the listable metadata for one issued token.
// The signed JWT is never included — it is returned exactly once, by
// CreateFederationToken, and is not stored server-side.
//
// Capability + AccountID (ADR-021) are omitempty: a metrics-plane record
// renders exactly as the pre-ADR-021 shape, and AccountID surfaces only
// on a logs-plane token.
type FederationTokenRecord struct {
	TokenID     string `json:"token_id"`
	TenantID    string `json:"tenant_id"`
	IssuedBy    string `json:"issued_by"`
	Description string `json:"description,omitempty"`
	Capability  string `json:"capability,omitempty"`
	AccountID   uint32 `json:"account_id,omitempty"`
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

func toFederationTokenRecord(r token.Record) FederationTokenRecord {
	out := FederationTokenRecord{
		TokenID:     r.TokenID,
		TenantID:    r.TenantID,
		IssuedBy:    r.IssuedBy,
		Description: r.Description,
		AccountID:   r.AccountID,
		IssuedAt:    r.IssuedAt.UTC().Format(time.RFC3339),
		ExpiresAt:   r.ExpiresAt.UTC().Format(time.RFC3339),
	}
	// Surface capability ONLY for a logs-plane record. A metrics record (the
	// default / absent capability) keeps the pre-ADR-021 shape: with Capability
	// left empty, `omitempty` drops the field, so the metrics token response is
	// byte-identical to what a pre-ADR-021 client received. Echoing
	// Capability:"metrics" here would break that back-compat contract (struct
	// docstring L34-36). AccountID is already 0 for metrics → omitempty drops it.
	if r.Capability == token.CapLogs {
		out.Capability = string(r.Capability)
	}
	return out
}

// CreateFederationToken handles POST /api/v1/federation/tokens.
//
// Issues a short-lived RS256 JWT (ADR-020) that the named tenant
// presents to the label-injection proxy to pull its own metrics. The
// caller must hold `admin` on the target tenant — federation is data
// egress, a higher bar than config write. The tenant ID is in the
// body, so that check is here rather than in route middleware.
//
// capability=logs (ADR-021) issues a LOGS-plane token instead: it resolves
// (allocating if needed) the tenant's monotonic account_id, embeds it, and
// binds the token to the logs audience. capability defaults to metrics, so
// a body without the field issues the unchanged ADR-020 metrics token.
//
// @Summary     Issue a tenant federation token
// @Description Mints a short-lived RS256 JWT for the named tenant. capability=metrics (default, ADR-020) → metrics audience, no account_id. capability=logs (ADR-021) → logs audience with the tenant's monotonic account_id embedded (allocated on first use). Requires admin permission on the tenant. The signed token is returned once and is not retrievable afterwards.
// @Tags        federation
// @Accept      json
// @Produce     json
// @Param       body body     CreateFederationTokenRequest true "Token request"
// @Success     201  {object} CreateFederationTokenResponse
// @Failure     400  {object} map[string]string
// @Failure     403  {object} map[string]string
// @Failure     409  {object} map[string]string
// @Failure     429  {object} map[string]string
// @Failure     500  {object} map[string]string
// @Failure     503  {object} map[string]string
// @Router      /api/v1/federation/tokens [post]
func CreateFederationToken(d *handler.Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		body, err := io.ReadAll(io.LimitReader(r.Body, d.MaxBody()))
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, "failed to read request body: "+err.Error())
			return
		}

		var req CreateFederationTokenRequest
		if err := json.Unmarshal(body, &req); err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, "invalid JSON: "+err.Error())
			return
		}
		if violations := handler.ValidateStructTags(&req); len(violations) > 0 {
			handler.WriteValidationErrors(w, r, violations)
			return
		}
		// Reject path-traversal / non-simple tenant IDs — the same gate
		// the other tenant-scoped handlers use, applied here for
		// consistency and defence-in-depth (the RBAC check below is the
		// real bar).
		if err := handler.ValidateTenantID(req.TenantID); err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		// Federation token issuance is data egress — require admin on
		// the target tenant (ADR-020 Wave-0 decision 5).
		if !d.RBAC.HasPermission(rbac.RequestGroups(r), req.TenantID, rbac.PermAdmin) {
			handler.WriteJSONErrorWithCode(w, r, http.StatusForbidden, handler.CodeForbidden,
				"admin permission required on tenant "+req.TenantID+" to issue a federation token")
			return
		}

		email := rbac.RequestEmail(r)
		// capability defaults to metrics when absent — back-compat (ADR-021).
		capability := token.Capability(req.Capability)
		if capability == "" {
			capability = token.CapMetrics
		}

		var (
			jwt string
			rec token.Record
		)
		switch capability {
		case token.CapLogs:
			// Resolve (allocate-if-missing) the tenant's account_id BEFORE
			// signing. This commits to the GitOps registry; a degraded forge
			// or overloaded write plane surfaces as 503 so the client retries
			// rather than getting a token with a bogus id.
			if d.Accounts == nil {
				handler.WriteJSONError(w, r, http.StatusServiceUnavailable,
					"logs federation is not configured on this server")
				return
			}
			accountID, aerr := d.Accounts.EnsureAccountID(r.Context(), req.TenantID, email)
			if aerr != nil {
				if errors.Is(aerr, gitops.ErrWriteOverloaded) || errors.Is(aerr, gitops.ErrForgeDegraded) {
					handler.WriteOverloaded(w, r)
					return
				}
				if errors.Is(aerr, gitops.ErrConflict) {
					handler.WriteJSONError(w, r, http.StatusConflict, aerr.Error())
					return
				}
				handler.WriteJSONError(w, r, http.StatusInternalServerError,
					"allocate account id: "+aerr.Error())
				return
			}
			jwt, rec, err = d.Federation.IssueLogs(req.TenantID, email, req.Description, accountID)
		default:
			jwt, rec, err = d.Federation.Issue(req.TenantID, email, req.Description)
		}
		if err != nil {
			switch {
			case errors.Is(err, token.ErrTokenLimitReached):
				handler.WriteJSONErrorWithCode(w, r, http.StatusConflict, handler.CodeConflict, err.Error())
			case errors.Is(err, token.ErrMintRateLimited):
				handler.WriteJSONErrorWithCode(w, r, http.StatusTooManyRequests, handler.CodeRateLimited, err.Error())
			default:
				handler.WriteJSONError(w, r, http.StatusInternalServerError, "issue federation token: "+err.Error())
			}
			return
		}

		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_ = json.NewEncoder(w).Encode(CreateFederationTokenResponse{
			Token:  jwt,
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
// @Failure     500 {object} map[string]string
// @Router      /api/v1/federation/tokens [get]
func ListFederationTokens(d *handler.Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := r.URL.Query().Get("tenant_id")
		if tenantID == "" {
			handler.WriteJSONError(w, r, http.StatusBadRequest, "query parameter tenant_id is required")
			return
		}
		if err := handler.ValidateTenantID(tenantID); err != nil {
			handler.WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		if !d.RBAC.HasPermission(rbac.RequestGroups(r), tenantID, rbac.PermAdmin) {
			handler.WriteJSONErrorWithCode(w, r, http.StatusForbidden, handler.CodeForbidden,
				"admin permission required on tenant "+tenantID)
			return
		}

		recs, err := d.Federation.List(tenantID)
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusInternalServerError, "list federation tokens: "+err.Error())
			return
		}
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
// Revokes the token (ADR-020 Posture B): the bookkeeping record is
// removed and the token id is added to the revoked set the API gateway
// consults. Revocation is eventually consistent — it propagates to the
// gateway within the ConfigMap projected-volume sync window (~1-2 min).
//
// @Summary     Revoke a federation token
// @Description Revokes a federation token — removes its record and adds it to the gateway revoked set. NOTE: revocation is eventually consistent and propagates to the gateway within ~1-2 minutes (ADR-020 Posture B). Requires admin permission on the token's tenant.
// @Tags        federation
// @Produce     json
// @Param       id path string true "Token ID"
// @Success     200 {object} map[string]string
// @Failure     403 {object} map[string]string
// @Failure     404 {object} map[string]string
// @Failure     500 {object} map[string]string
// @Router      /api/v1/federation/tokens/{id} [delete]
func DeleteFederationToken(d *handler.Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tokenID := chi.URLParam(r, "id")

		rec, ok, err := d.Federation.Get(tokenID)
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusInternalServerError, "look up federation token: "+err.Error())
			return
		}
		if !ok {
			handler.WriteJSONError(w, r, http.StatusNotFound, "federation token not found: "+tokenID)
			return
		}

		if !d.RBAC.HasPermission(rbac.RequestGroups(r), rec.TenantID, rbac.PermAdmin) {
			handler.WriteJSONErrorWithCode(w, r, http.StatusForbidden, handler.CodeForbidden,
				"admin permission required on tenant "+rec.TenantID)
			return
		}

		deleted, err := d.Federation.Delete(tokenID, rec.ExpiresAt)
		if err != nil {
			handler.WriteJSONError(w, r, http.StatusInternalServerError, "revoke federation token: "+err.Error())
			return
		}
		if !deleted {
			handler.WriteJSONError(w, r, http.StatusNotFound, "federation token not found: "+tokenID)
			return
		}

		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(map[string]string{
			"status":   "revocation_initiated",
			"token_id": tokenID,
			"detail":   "revocation propagates to the gateway within ~2 minutes",
		})
	}
}
