package handler

import (
	"errors"
	"log/slog"
	"net/http"
	"os"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"

	"github.com/go-chi/chi/v5"

	"github.com/vencil/tenant-api/internal/boundedcall"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/customalerts"
)

// TenantDetail is the full tenant representation returned by GET /api/v1/tenants/{id}.
type TenantDetail struct {
	ID       string                  `json:"id"`
	RawYAML  string                  `json:"raw_yaml"`
	Resolved []cfg.ResolvedThreshold `json:"resolved_thresholds"`
	// Warnings is the BLOCKING validation set (KeyValidation.Errors) — what a
	// write of this exact file would be rejected on. It judges only the keys
	// the tenant's own file writes: a problem in a root platform file's
	// `tenants:` entry for this tenant is never here (#2208). Notices is the
	// advisory set, which never blocks a write: deprecated-key alias notices
	// (#1231 — the file keeps resolving and writing, but carries a spelling
	// the author should migrate), and one notice per problem in a root
	// platform file's entry for this tenant, naming that file ("platform file
	// <name>, entry tenants.<id>: …") — the platform operator fixes those
	// there. Split fields so a client never has to text-parse severity out
	// of one list.
	Warnings []string `json:"validation_warnings,omitempty"`
	Notices  []string `json:"validation_notices,omitempty"`
	// SourceHash is SHA-256[:16] of the raw tenant file. Clients echo it
	// back as `base_hash` on PUT .../custom-alerts for optimistic-
	// concurrency (ADR-024 §S6b-2): the write 409s if the file changed
	// underneath them.
	SourceHash string `json:"source_hash"`
	// CustomAlerts is the tenant's `_custom_alerts` recipes as structured
	// JSON (ADR-024 §S6b-2). The portal recipe modal reads this directly so
	// the client never parses YAML — the backend owns the round-trip on both
	// read and write. Empty slice when the tenant has none.
	CustomAlerts []map[string]any `json:"custom_alerts"`
}

// GetTenant handles GET /api/v1/tenants/{id}
//
// @Summary     Get tenant config
// @Description Returns the raw YAML and resolved thresholds for a single tenant.
// @Tags        tenants
// @Produce     json
// @Param       id   path     string true "Tenant ID"
// @Success     200  {object} TenantDetail
// @Failure     400  {object} ErrorResponse
// @Failure     404  {object} ErrorResponse
// @Failure     409  {object} ErrorResponse
// @Failure     500  {object} ErrorResponse
// @Router      /api/v1/tenants/{id} [get]
func GetTenant(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		// #1673: resolve the tenant's ACTUAL file rather than assuming the
		// `.yaml` spelling. A tenant stored as `<id>.yml` is listed by
		// GET /tenants (the enumerator accepts both) but used to 404 here.
		filePath, err := confd.ResolveTenantFile(d.ConfigDir, tenantID)
		switch {
		case errors.Is(err, confd.ErrTenantFileNotFound):
			WriteJSONError(w, r, http.StatusNotFound, "tenant not found: "+tenantID)
			return
		case errors.Is(err, confd.ErrAmbiguousTenantFile):
			WriteJSONError(w, r, http.StatusConflict, err.Error())
			return
		case err != nil:
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}
		data, err := os.ReadFile(filePath)
		if os.IsNotExist(err) {
			// Lost a race with a delete between resolve and read.
			WriteJSONError(w, r, http.StatusNotFound, "tenant not found: "+tenantID)
			return
		}
		if err != nil {
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}

		// Merge over the root platform surface: the defaults carrier and
		// the platform files' per-tenant `tenants:` layer (#2208).
		merged, err := d.loadMergedConfig(tenantID, data)
		if err != nil {
			slog.Error("tenant GET: conf.d root platform read failed", "tenant", tenantID, "error", err)
			WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeInternal, msgRootPlatformRead)
			return
		}

		// #1231 1b: two-channel split — validation_warnings stays Errors-only
		// (the blocking set), deprecation notices get their own field.
		kv := merged.ValidateTenantKeys()
		resolved := merged.ResolveAt(time.Now())

		// Filter to only this tenant's thresholds. Initialized non-nil so a
		// tenant with zero thresholds serializes as [] (the spec declares an
		// array; nil would serialize as null — caught by the contract fuzz,
		// same class as the /me empty-slice normalization in #245).
		tenantResolved := make([]cfg.ResolvedThreshold, 0)
		for _, rt := range resolved {
			if rt.Tenant == tenantID {
				tenantResolved = append(tenantResolved, rt)
			}
		}

		customAlerts, err := customalerts.Extract(string(data), tenantID)
		if err != nil {
			// A parse error here means the tenant file is not valid YAML; surface
			// it rather than returning a 200 with silently-empty custom_alerts.
			// Keep the raw parser error (which can echo file contents) in the
			// server log only; return a stable, non-sensitive message to clients.
			slog.Error("failed to parse tenant custom alerts", "tenant", tenantID, "err", err)
			WriteJSONError(w, r, http.StatusInternalServerError, "failed to parse tenant custom alerts")
			return
		}

		detail := TenantDetail{
			ID:           tenantID,
			RawYAML:      string(data),
			Resolved:     tenantResolved,
			Warnings:     kv.Errors,
			Notices:      kv.Notices,
			SourceHash:   cfg.ComputeSourceHash(data),
			CustomAlerts: customAlerts,
		}

		writeJSON(w, http.StatusOK, detail)
	}
}

// msgRootPlatformRead is the fixed client-facing text for a GET whose read of
// the conf.d root platform files did not complete in time. The full error
// goes to the server log only.
const msgRootPlatformRead = "cannot read the conf.d root platform files in time; " +
	"the tenant's effective thresholds cannot be computed"

// defaultRootReadGuard is the process-wide bound behind Deps.RootReadGuard
// when that field is nil.
var defaultRootReadGuard = &boundedcall.Guard{}

// loadMergedConfig merges the tenant file over the conf.d root platform
// surface — the defaults carrier and the root platform files' per-tenant
// `tenants:` layer. Thin wrapper over the shared cfg.MergeTenantWithRootDefaults
// so the GET / validate / write-boundary paths all merge identically (the
// consolidation that closed the ADR-024 PR4 / #704 write-vs-read asymmetry).
//
// ⛔ BOUNDED (#2208). The merge reads every root `_*.yaml`, and a read that
// never returns (a FIFO, a hung mount) would hang this GET — before #2208
// only a file named like the defaults carrier could. The writer's paths are
// already bounded by their conf.d walk; this is the same mechanism
// (boundedcall), on its own guard so a stuck GET read does not fail writes.
func (d *Deps) loadMergedConfig(tenantID string, tenantData []byte) (cfg.TenantMerge, error) {
	guard := d.RootReadGuard
	if guard == nil {
		guard = defaultRootReadGuard
	}
	merge := d.mergeTenant
	if merge == nil {
		merge = loadMergedConfig
	}
	// A panic in the merge comes back as boundedcall.ErrPanicked (the merge
	// runs on boundedcall's goroutine, where chi's Recoverer cannot reach);
	// the caller logs it and answers with the same fixed 500.
	return boundedcall.Do(guard, func() cfg.TenantMerge {
		return merge(d.ConfigDir, tenantID, tenantData)
	})
}

// loadMergedConfig is the unbounded merge (see Deps.loadMergedConfig).
func loadMergedConfig(configDir, tenantID string, tenantData []byte) cfg.TenantMerge {
	return cfg.MergeTenantWithRootDefaults(configDir, tenantID, tenantData)
}

// tenantIDFromPath is a helper for chi URL param extraction used by middleware.
func tenantIDFromPath(r *http.Request) string {
	return chi.URLParam(r, "id")
}

// TenantIDFromPath is the exported version for use in router setup.
var TenantIDFromPath = tenantIDFromPath
