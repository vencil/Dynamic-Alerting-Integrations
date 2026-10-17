package handler

import (
	"errors"
	"log/slog"
	"net/http"

	"github.com/go-chi/chi/v5"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

// ValidateResponse is returned by POST /api/v1/tenants/{id}/validate.
//
// Two-channel contract (#1231 1b, mirroring cfg.KeyValidation): Warnings is
// the BLOCKING set and the only input to Valid. Notices is the advisory set
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
// Dry-run validation without writing: the verdict comes from the same gitops
// validation function PUT runs in the configured write mode (#2124) — the full
// pre-write sequence in direct mode, the body-only pre-flight in PR mode.
// Authorization is not part of it, and neither are routing and the domain
// policy: the route generator judges those on the write (PUT's 422
// GENERATOR_FINDINGS, #2903), and the dry-run does not call it. Body checks
// belong in gitops, never here.
//
// @Summary     Validate tenant config
// @Description Dry-run validation of a tenant YAML without writing to disk.
// @Description When conf.d holds an entry that cannot be read other than the tenant's file (#2939), the answer is
// @Description valid: false with the text PUT /api/v1/tenants/{id} returns in its 409 TENANT_TREE_UNREADABLE (a
// @Description symlink that cannot be resolved) or its 500 TENANT_STORE_FAULT (an entry of tenant-api's copy the server cannot read);
// @Description when the tenant's own file cannot be read, with PUT's 409 TENANT_CONFIG_NOT_LOADABLE text.
// @Description The tenants the body declares besides {id} are judged as PUT judges them (#3062): one the caller may not write, or a
// @Description tenants mapping with a merge key (<<), a key that does not decode to a string or one id twice, is valid: false with
// @Description PUT's 403 / 400 text, in both write modes.
// @Tags        tenants
// @Accept      application/yaml
// @Produce     json
// @Param       id    path     string true  "Tenant ID"
// @Param       body  body     string true  "Tenant YAML content"
// @Success     200   {object} ValidateResponse
// @Failure     400   {object} ErrorResponse
// @Failure     413   {object} ErrorResponse
// @Router      /api/v1/tenants/{id}/validate [post]
func ValidateTenant(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateWritableTenantID(tenantID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		body, ok := readLimitedBody(w, r, d)
		if !ok {
			return
		}

		// #3062: the tenants the body declares besides {id}, judged as PUT
		// judges them (declaredTenantsJudge); PUT's refusal text is the
		// verdict, as the other refusals below are.
		if _, status, msg := declaredTenantsJudge(r, d, tenantID, string(body)); status != 0 {
			writeJSON(w, http.StatusOK, ValidateResponse{Valid: false, Warnings: []string{msg}})
			return
		}

		var errs, notices []string
		var err error
		if d.prWritePath() {
			// The local tree may lag the base in PR mode (#1718), so only the
			// body-only pre-flight WritePR itself runs before the PR is cut.
			errs, err = gitops.DryRunValidateBodyOnly(tenantID, string(body))
		} else {
			// Production always wires d.Writer (cmd/server/main.go builds it on
			// the same configDir). The fallback serves handler-test literals
			// that set only ConfigDir: same checks, without the production
			// Writer's shared read walk and its stuck-walk breaker (see
			// DryRunValidate).
			wr := d.Writer
			if wr == nil {
				wr = gitops.NewWriter(d.ConfigDir, "")
			}
			errs, notices, err = wr.DryRunValidate(withTenantNames(withReplaceUnparseable(r, d), d).Context(), tenantID, string(body))
		}
		if err != nil {
			errs = []string{dryRunRefusalMessage(err, d != nil && treePathsVisible(d.RBAC, rbac.RequestPrincipal(r)))}
		}
		writeJSON(w, http.StatusOK, ValidateResponse{
			Valid:    len(errs) == 0,
			Warnings: errs,
			Notices:  notices,
		})
	}
}

// msgTenantFileUnresolved is the fixed text for any other pre-validation
// failure: those errors (e.g. the resolver's ReadDir) carry server paths.
const msgTenantFileUnresolved = "cannot resolve the tenant's config file in conf.d; see the server log"

// dryRunRefusalMessage turns a refusal that would come BEFORE validation into
// Warnings text. This route needs only read permission, so only two errors
// pass through verbatim — the reserved-id and invalid-id refusals (the latter
// names only the id and the rule) and ErrAmbiguousTenantFile,
// whose text names only this tenant's own files and which PUT already returns
// as is. Everything else gets fixed text; the full error goes to the log.
// A TENANT_TREE_UNREADABLE / TENANT_STORE_FAULT refusal (#2939) is the text
// PUT's 409 / 500 carries, naming the entries only when treeVisible
// (treePathsVisible), and a tenant file that cannot be read gets PUT's
// TENANT_CONFIG_NOT_LOADABLE text — the same verdict and the same
// disclosure as the write this previews.
func dryRunRefusalMessage(err error, treeVisible bool) string {
	var te *gitops.TreeUnreadableError
	if errors.As(err, &te) {
		logTreeUnreadable("tenant dry-run: conf.d holds unreadable entries", "", err)
		return treeUnreadableText(te, treeVisible)
	}
	var tu *gitops.TargetUnreadableError
	if errors.As(err, &tu) {
		return targetUnreadableText(tu)
	}
	switch {
	case errors.Is(err, gitops.ErrReservedTenantID), errors.Is(err, gitops.ErrInvalidTenantID),
		errors.Is(err, confd.ErrAmbiguousTenantFile):
		return err.Error()
	case errors.Is(err, gitops.ErrTenantDeclaredElsewhere):
		slog.Warn("tenant dry-run: tenant declared by another conf.d file", "error", err)
		return msgTenantDeclaredElsewhere
	case errors.Is(err, gitops.ErrTenantTreeScan):
		slog.Error("tenant dry-run: conf.d scan failed", "error", err)
		return msgTenantTreeScan
	default:
		slog.Error("tenant dry-run: cannot resolve tenant file", "error", err)
		return msgTenantFileUnresolved
	}
}
