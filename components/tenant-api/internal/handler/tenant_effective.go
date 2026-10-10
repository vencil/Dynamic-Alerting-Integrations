package handler

// ============================================================
// GET /api/v1/tenants/{id}/effective — v2.7.0 (ADR-016 + ADR-017)
// ============================================================
//
// Returns the *merged* effective config for one tenant: _defaults.yaml chain
// (L0..Ln), then the root platform files' `tenants:` entries for it (#2019),
// then the tenant's own overrides, plus two SHA-256[:16] hashes for change
// detection.
//
// The handler is stateless — it re-scans `configDir` on every request. That's
// acceptable because (a) this endpoint is low-traffic (UI + support tooling,
// not a hot path like /metrics), and (b) the read-only, scan-each-call shape
// matches the existing GET /api/v1/tenants/{id} handler, avoiding any new
// shared-mutable-state surface area between tenant-api and the exporter.
//
// Parity: the merged_hash returned here is byte-identical to
// describe_tenant.py's computed hash for the shapes the golden fixtures
// cover. ⚠️ The assertion is not in this package — tenant_effective_test.go
// pins hash stability and shape, not cross-language equality (it says so
// itself); the byte-for-byte claim rests on threshold-exporter's
// app/config_golden_parity_test.go, which exercises the same pkg/config this
// handler imports. Chain discovery included: TestGoldenParity_ResolveEffective
// calls the same config.ResolveEffective this handler does on every golden
// fixture and compares chain, hashes and effective config with the Python
// capture (#1550). Before it, reversing the chain order in ResolveEffective
// moved the served merged_hash and left every golden assertion green.

import (
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"path/filepath"
	"sort"

	"github.com/go-chi/chi/v5"
	cfg "github.com/vencil/threshold-exporter/pkg/config"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/credmask"
)

// GetTenantEffective handles GET /api/v1/tenants/{id}/effective.
//
// @Summary     Get tenant effective (merged) config
// @Description Returns the tenant config after merging the _defaults.yaml
// @Description chain from L0 (conf.d root) down to the tenant's own file,
// @Description with the root platform files' per-tenant `tenants:` entries
// @Description applied between the chain and the tenant file (the tenant
// @Description file wins key by key); platform_overlay names the files and
// @Description keys that layer supplied and is omitted when it supplied none.
// @Description A tenant's _profile is expanded as /metrics expands it: the
// @Description profile (from the root platform files' profiles: blocks) fills
// @Description in keys neither the tenant file nor the platform entries set;
// @Description profile_overlay names the profile, the files and the keys it
// @Description supplied and is omitted when it supplied none.
// @Description Includes two SHA-256 hashes (truncated to 16 hex chars):
// @Description source_hash for raw file content and merged_hash for the
// @Description canonical-JSON of the merged dict. Parity target:
// @Description scripts/tools/dx/describe_tenant.py.
// @Description effective_config keeps every value as written; not_served
// @Description names each key whose shown value /metrics does not serve,
// @Description with the exporter's own reason (parse_failed,
// @Description root_defaults_unwrapped, value_rejected, value_unparsed,
// @Description value_unparsed_dropped, window_invalid, undeliverable,
// @Description root_null_undeclared, spelling_duplicate)
// @Description and the file of the value shown; spelling_duplicate names a
// @Description spelling of a threshold that the same mapping also writes
// @Description under the spelling /metrics serves (two spellings of one
// @Description dimensional key, or both #1231 names). Dimensional keys are
// @Description one threshold whatever their label order or quoting: every key
// @Description here is spelled as the layer that supplied its value wrote it.
// @Description chain_parse_failed lists the
// @Description defaults_chain files with a syntax error the exporter does not
// @Description read; they are read as empty and the tenant is still answered
// @Description (200). Both are omitted when empty.
// @Tags        tenants
// @Produce     json
// @Param       id   path     string true "Tenant ID"
// @Description A caller who may read the tenant but not write it gets every
// @Description receiver credential in effective_config (webhook / chat URLs,
// @Description PagerDuty keys, passwords, tokens — at any depth) replaced by
// @Description "<masked: write permission required>", with masked: true;
// @Description source_hash and merged_hash still describe the stored values.
// @Success     200  {object} TenantEffectiveResponse
// @Failure     400  {object} ErrorResponse
// @Failure     404  {object} ErrorResponse
// @Failure     409  {object} ErrorResponse "Conflict: ambiguous tenant file, or the tenant is declared by more than one conf.d file"
// @Failure     500  {object} ErrorResponse
// @Router      /api/v1/tenants/{id}/effective [get]
func GetTenantEffective(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		tenantID := chi.URLParam(r, "id")
		if err := ValidateTenantID(tenantID); err != nil {
			WriteJSONError(w, r, http.StatusBadRequest, err.Error())
			return
		}

		// ⛔ The walker's verdict decides, not a filename match (#2511
		// review): a sibling `<id>.yml` that declares nothing for the tenant
		// (`tenants: {}`, another tenant only, unparseable, a dangling
		// symlink, a symlink to a directory) is no second declaration — the
		// exporter serves the tenant — so only ResolveEffective's typed
		// *DuplicateTenantError turns a request into a 409.
		ec, err := cfg.ResolveEffective(d.ConfigDir, tenantID)
		if err != nil {
			var dup *cfg.DuplicateTenantError
			var decodeErr *cfg.DecodeError
			switch {
			case errors.Is(err, cfg.ErrTenantNotFound):
				WriteJSONError(w, r, http.StatusNotFound, "tenant not found: "+tenantID)
			case errors.As(err, &dup):
				// The typed error carries both files' ABSOLUTE paths; they go
				// to the log only (#2511: they used to reach the body as a 500).
				slog.Warn("tenant effective: tenant declared by more than one conf.d file",
					"tenant", tenantID, "error", err)
				WriteJSONError(w, r, http.StatusConflict, effectiveDuplicateMessage(d.ConfigDir, tenantID, dup))
			case errors.As(err, &decodeErr):
				// Its text is the decoder's own (`parse defaults[i]: …` /
				// `parse tenant: …`) and names no path — pinned by
				// TestGetTenantEffective_DecodeErrorTextNamesNoServerPath. A
				// chain file the exporter drops for a syntax error no longer
				// lands here (#2296: read as empty, ChainParseFailed); one the
				// exporter reads but the merge cannot decode still does.
				// Its own code, not INTERNAL_ERROR, so WriteErrorEnvelope
				// keeps the text (#1700): it tells the operator what to fix.
				//
				// #1560: except to a caller shown masked credentials — a yaml.v3
				// type error quotes the offending value, which can be one.
				msg := err.Error()
				if !canSeeCredentials(r, d, tenantID) {
					msg = msgEffectiveDecodeMasked
				}
				WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, CodeConfigDecode, msg)
			default:
				// Walker and read failures carry server paths (a missing root
				// answers `stat "/…/conf.d": …`): fixed text, full error logged.
				slog.Error("tenant effective: cannot resolve", "tenant", tenantID, "error", err)
				WriteJSONError(w, r, http.StatusInternalServerError, msgEffectiveUnresolved)
			}
			return
		}

		// A YAML `.inf` / `.nan` leaves a non-finite float in the tree, which
		// JSON cannot carry: send it as the text Python's json.dumps writes.
		// merged_hash was computed from the original tree, so it is unchanged.
		out := TenantEffectiveResponse{EffectiveConfig: *ec}
		out.EffectiveConfig.EffectiveConfig = cfg.NonFiniteAsText(ec.EffectiveConfig)
		// #1560: masked for a caller who cannot write the tenant. Every
		// layer is masked, the defaults chain and the platform entries
		// included — a value is masked by its key, wherever it came from.
		if !canSeeCredentials(r, d, tenantID) {
			out.EffectiveConfig.EffectiveConfig, _ = credmask.MaskValue(out.EffectiveConfig.EffectiveConfig).(map[string]any)
			out.Masked = true
		}
		writeJSON(w, http.StatusOK, out)
	}
}

// TenantEffectiveResponse is GET /tenants/{id}/effective's answer: the
// effective config, plus Masked when its credentials were masked for the
// caller (#1560; see canSeeCredentials).
type TenantEffectiveResponse struct {
	cfg.EffectiveConfig
	// Masked is true when every receiver credential in effective_config was
	// replaced by the placeholder "<masked: write permission required>"
	// because the caller may read the tenant but not write it.
	Masked bool `json:"masked,omitempty"`
}

// msgEffectiveDecodeMasked replaces the decoder's message for a caller shown
// masked credentials: a yaml.v3 type error quotes the value it could not
// decode, which can be a credential.
const msgEffectiveDecodeMasked = "the tenant's effective config does not decode; " +
	"the decoder's message is shown to callers with write permission on the tenant"

// msgEffectiveUnresolved is the fixed client-facing text for a resolve
// failure other than not-found, a duplicate or a decode error: those errors
// carry server paths, which only the log may see.
const msgEffectiveUnresolved = "cannot resolve the tenant's effective config from conf.d; see the server log"

// effectiveDuplicateMessage is the 409 text for a *DuplicateTenantError.
//
// When both declaring files are the tenant's own top-level spellings
// (`<id>.yaml` beside `<id>.yml`, directly in conf.d), the text is
// GET /tenants/{id}'s ambiguity message — the same sentinel wording, base
// names only — since both names follow from the id the caller already holds.
// Any other pair (a subdirectory copy, including both spellings inside a
// subdirectory, or a shared file's `tenants:` entry) gets the fixed text the
// write plane uses: the other file's name or location is not this caller's
// to learn.
func effectiveDuplicateMessage(configDir, tenantID string, dup *cfg.DuplicateTenantError) string {
	a, okA := topLevelTenantFileName(configDir, tenantID, dup.PathA)
	b, okB := topLevelTenantFileName(configDir, tenantID, dup.PathB)
	if !okA || !okB {
		return msgTenantDeclaredElsewhere
	}
	names := []string{a, b}
	sort.Strings(names)
	return fmt.Errorf("%w: %q is claimed by %v", confd.ErrAmbiguousTenantFile, tenantID, names).Error()
}

// topLevelTenantFileName returns path's base name when path sits directly in
// configDir and that name classifies as tenantID's own file. The walker
// reports paths under its absolute, symlink-resolved root, so both forms of
// configDir are tried; anything that matches neither is not top-level here.
func topLevelTenantFileName(configDir, tenantID, path string) (string, bool) {
	parent := filepath.Dir(filepath.Clean(path))
	roots := []string{}
	if abs, err := filepath.Abs(configDir); err == nil {
		roots = append(roots, abs)
		if real, err := filepath.EvalSymlinks(abs); err == nil {
			roots = append(roots, real)
		}
	}
	for _, root := range roots {
		if parent != filepath.Clean(root) {
			continue
		}
		name := filepath.Base(path)
		if id, ok := confd.TenantIDFromFile(name); ok && id == tenantID {
			return name, true
		}
		return "", false
	}
	return "", false
}
