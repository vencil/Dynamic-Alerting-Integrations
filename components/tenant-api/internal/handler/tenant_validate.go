package handler

import (
	"errors"
	"log/slog"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/gitops"
)

// ValidateResponse is returned by POST /api/v1/tenants/{id}/validate.
//
// Two-channel contract (#1231 1b, mirroring cfg.KeyValidation): Warnings is
// the BLOCKING set — exactly what the write boundary (gitops.validate) would
// reject, and the only input to Valid. Notices is the advisory set
// (deprecated-key alias notices): a body carrying them still validates AND
// still writes; they tell the author what to migrate before the transition
// window closes.
type ValidateResponse struct {
	Valid    bool     `json:"valid"`
	Warnings []string `json:"warnings,omitempty"`
	Notices  []string `json:"notices,omitempty"`
}

// ValidateTenant handles POST /api/v1/tenants/{id}/validate
//
// Dry-run validation: run the write path's own pre-write checks
// (gitops.Writer.DryRunValidate) without writing.
//
// ⛔ THE VERDICT COMES ONLY FROM gitops.Writer.DryRunValidate (#2124). This
// handler used to re-assemble the write path's checks by hand, and answered
// `valid: true` for every body whose refusal it had not copied — invalid YAML
// among them. Do not add a check here: add it to gitops.validate, and both
// boundaries get it.
//
// @Summary     Validate tenant config
// @Description Dry-run validation of a tenant YAML without writing to disk.
// @Tags        tenants
// @Accept      application/yaml
// @Produce     json
// @Param       id    path     string true  "Tenant ID"
// @Param       body  body     string true  "Tenant YAML content"
// @Success     200   {object} ValidateResponse
// @Failure     400   {object} ErrorResponse
// @Router      /api/v1/tenants/{id}/validate [post]
func ValidateTenant(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		body, ok := readLimitedBody(w, r, d)
		if !ok {
			return
		}

		// Production always wires d.Writer (cmd/server/main.go builds it on the
		// same configDir). The fallback serves handler-test literals that set
		// only ConfigDir: it runs the same checks, and lacks only the production
		// Writer's shared stuck-walk breaker (see DryRunValidate).
		wr := d.Writer
		if wr == nil {
			wr = gitops.NewWriter(d.ConfigDir, "")
		}
		errs, notices, err := wr.DryRunValidate(tenantID, string(body))
		if err != nil {
			errs = []string{dryRunRefusalMessage(err)}
		}
		writeJSON(w, http.StatusOK, ValidateResponse{
			Valid:    len(errs) == 0,
			Warnings: errs,
			Notices:  notices,
		})
	}
}

// dryRunRefusalMessage turns a refusal that would come BEFORE validation (the
// write would not get as far as validate) into Warnings text. The two #2078
// placement errors get the same FIXED text PUT answers with: their own text
// names another conf.d file, which only the server log may see — and this
// route needs only read permission. Everything else (reserved id, ambiguous
// tenant file) is the text PUT already returns to its caller.
func dryRunRefusalMessage(err error) string {
	switch {
	case errors.Is(err, gitops.ErrTenantDeclaredElsewhere):
		slog.Warn("tenant dry-run: tenant declared by another conf.d file", "error", err)
		return msgTenantDeclaredElsewhere
	case errors.Is(err, gitops.ErrTenantTreeScan):
		slog.Error("tenant dry-run: conf.d scan failed", "error", err)
		return msgTenantTreeScan
	default:
		return err.Error()
	}
}
