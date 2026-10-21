package handler

import (
	"errors"
	"log/slog"
	"net/http"
	"strings"

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
	// Code is TENANT_GIT_MANAGED when the verdict is PUT's refusal of a
	// git-managed tenant (#3064), or TENANT_FILE_DIRTY when it is PUT's
	// refusal of a tenant file that differs from HEAD in this server's
	// repository (#1535, direct mode); warnings carries the text. A client
	// can tell "change this tenant in git" / "commit or discard the change
	// in the repository" from a body to fix. Absent otherwise.
	Code string `json:"code,omitempty" enums:"TENANT_GIT_MANAGED,TENANT_FILE_DIRTY"`
}

// ValidateTenant handles POST /api/v1/tenants/{id}/validate
//
// Dry-run without writing: the verdict is PUT's own refusal sequence (#3062,
// planPut and gitops.PreviewPut, tenant_put_plan.go) — the write scope, the
// placeholder, then in direct mode the steps PUT runs on the tree
// (resolution, the git-managed refusal of #3064, validation, #2124), in PR
// mode only the body-only pre-flight (#1718). Routing and the domain policy are not part
// of it: the route generator judges those on the write (PUT's 422
// GENERATOR_FINDINGS, #2903), and the dry-run does not call it. A refusal
// of the request itself (the id, the body's size) is this route's 400 /
// 413; every other refusal is valid: false with PUT's text. Body checks
// belong in gitops, never here.
//
// @Summary     Validate tenant config
// @Description Dry-run validation of a tenant YAML without writing to disk.
// @Description When conf.d holds an entry that cannot be read other than the tenant's file (#2939), the answer is
// @Description valid: false with the text PUT /api/v1/tenants/{id} returns in its 409 TENANT_TREE_UNREADABLE (a
// @Description symlink that cannot be resolved) or its 500 TENANT_STORE_FAULT (an entry of tenant-api's copy the server cannot read);
// @Description when the tenant's own file cannot be read, with PUT's 409 TENANT_CONFIG_NOT_LOADABLE text.
// @Description The body is judged by PUT's own refusal sequence, in PUT's order (#3062): the write scope on {id} as the file
// @Description holds it and as the body proposes it, a masked-credential placeholder, the body's own shape (it must declare
// @Description tenants.{id} alone, API-shaped, #3064), the tenant file's resolution, a git-managed tenant (#3064: valid: false
// @Description with PUT's 409 TENANT_GIT_MANAGED text and code: TENANT_GIT_MANAGED), the tenant file differing from HEAD
// @Description in this server's copy (#1535: valid: false with PUT's 409 TENANT_FILE_DIRTY text and code: TENANT_FILE_DIRTY), a tenant another conf.d file also
// @Description declares, validation. A refusal is valid: false with the text PUT refuses with (a validation error
// @Description listed bare, one per warning). In PR write mode PUT runs the steps from the tenant file's resolution on the latest
// @Description base branch, which this server's copy may lag: they are not run here, only the body-only pre-flight (#1718).
// @Description This route needs read permission only, but the verdict is the write's: a caller who may read the tenant and not write
// @Description it gets valid: false with the write gate's text, as its PUT is refused (#3062; before, such a caller got a verdict on
// @Description the body alone). POST /api/v1/tenants/{id}/diff shows that caller a masked preview instead.
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
		readBody := func(w http.ResponseWriter, r *http.Request) (string, bool) {
			body, ok := readLimitedBody(w, r, d)
			return string(body), ok
		}
		// #3062: PUT's refusal sequence (tenant_put_plan.go). A refusal of
		// the request itself (the id, the body's size) is this route's own
		// 400 / 413; every other refusal is the verdict — valid: false with
		// the text PUT answers it with.
		verdict := &verdictCapture{}
		plan, ok := planPut(w, verdict, r, d, endpointValidate, readBody)
		if !ok {
			if verdict.status != 0 {
				writeJSON(w, http.StatusOK, ValidateResponse{Valid: false, Warnings: verdict.validateWarnings()})
			}
			return
		}

		var errs, notices []string
		var err error
		if d.prWritePath() {
			// The local tree may lag the base in PR mode (#1718), so only the
			// body-only pre-flight WritePR itself runs before the PR is cut;
			// PUT runs the rest on the fresh base, under the writer lock.
			errs, err = gitops.DryRunValidateBodyOnly(plan.tenantID, plan.body)
		} else {
			var res gitops.PutResolution
			res, errs, err = plan.preview(d)
			notices = res.Notices
		}
		var code string
		if err != nil {
			errs = []string{dryRunRefusalMessage(err, d != nil && treePathsVisible(d.RBAC, rbac.RequestPrincipal(r)))}
			switch {
			case errors.Is(err, gitops.ErrTenantGitManaged):
				code = CodeTenantGitManaged
			case errors.Is(err, gitops.ErrTenantFileDirty):
				code = CodeTenantFileDirty
			}
		}
		writeJSON(w, http.StatusOK, ValidateResponse{
			Valid:    len(errs) == 0,
			Warnings: errs,
			Notices:  notices,
			Code:     code,
		})
	}
}

// msgTenantFileUnresolved is the fixed text for any other pre-validation
// failure: those errors (e.g. the resolver's ReadDir) carry server paths.
const msgTenantFileUnresolved = "cannot resolve the tenant's config file in conf.d; see the server log"

// msgGitDriverConfigured is the fixed text for a write or dry-run refused
// because the server's git config names a filter, diff or merge driver
// (#3175): a write answers it as 500 GIT_DRIVER_CONFIGURED (#3226), a
// dry-run as valid: false.
const msgGitDriverConfigured = "the server cannot check or write tenant files: its git configuration is refused; see the server log"

// msgGitConfigUnreadable is the fixed text for a write refused because the
// server's git config could not be read to check it for such a driver
// (#3226): 503, retry.
const msgGitConfigUnreadable = "the server could not read its git configuration to check it; nothing was written — please retry shortly"

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
	// #3064: a git-managed tenant — PUT's 409 TENANT_GIT_MANAGED text,
	// which names this tenant and a fixed reason code only.
	var gm *gitops.GitManagedError
	if errors.As(err, &gm) {
		return gm.Error()
	}
	switch {
	case errors.Is(err, gitops.ErrValidation):
		return strings.TrimPrefix(err.Error(), gitops.ErrValidation.Error()+": ")
	case errors.Is(err, gitops.ErrReservedTenantID), errors.Is(err, gitops.ErrInvalidTenantID),
		errors.Is(err, confd.ErrAmbiguousTenantFile):
		return err.Error()
	case errors.Is(err, gitops.ErrTenantDeclaredElsewhere):
		slog.Warn("tenant dry-run: tenant declared by another conf.d file", "error", err)
		return msgTenantDeclaredElsewhere
	case errors.Is(err, gitops.ErrTenantTreeScan):
		slog.Error("tenant dry-run: conf.d scan failed", "error", err)
		return msgTenantTreeScan
	case errors.Is(err, gitops.ErrTenantFileDirty):
		slog.Warn("tenant dry-run: tenant file differs from HEAD", "error", err)
		return msgTenantFileDirty
	case errors.Is(err, gitops.ErrGitDriverConfigured):
		// #3175: the server's git config, not the request: fixed text, the
		// keys in the log.
		slog.Error("tenant dry-run: refused, git config names a driver program", "error", err)
		return msgGitDriverConfigured
	case errors.Is(err, gitops.ErrGitConfigUnreadable):
		// #3226: PreviewPut skips the HEAD check on it today, so it does
		// not reach here; if it ever does, the fixed text, not the
		// resolver's.
		slog.Error("tenant dry-run: refused, the git config could not be read to check it for driver programs", "error", err)
		return msgGitConfigUnreadable
	default:
		slog.Error("tenant dry-run: cannot resolve tenant file", "error", err)
		return msgTenantFileUnresolved
	}
}
