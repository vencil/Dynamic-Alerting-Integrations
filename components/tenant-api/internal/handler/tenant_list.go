package handler

import (
	"fmt"
	"net/http"
	"time"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	"github.com/vencil/tenant-api/internal/tenantorg"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// TenantSummary is the list-view representation of a single tenant.
// v2.5.0: Extended with metadata fields for UI grouping and filtering.
type TenantSummary struct {
	ID string `json:"id"`
	// Raw `_silent_mode` value from the tenant's own config file, not the tenant's state (see config_derived); omitted when config_derived cannot be derived.
	SilentMode string `json:"silent_mode,omitempty"`
	// Raw `_state_maintenance` value from the tenant's own config file, not the tenant's state (see config_derived); omitted when config_derived cannot be derived.
	Maintenance string   `json:"maintenance,omitempty"`
	Profile     string   `json:"profile,omitempty"`
	Environment string   `json:"environment,omitempty"`
	Region      string   `json:"region,omitempty"`
	Tier        string   `json:"tier,omitempty"`
	Domain      string   `json:"domain,omitempty"`
	DBType      string   `json:"db_type,omitempty"`
	Owner       string   `json:"owner,omitempty"`
	Tags        []string `json:"tags,omitempty"`
	Groups      []string `json:"groups,omitempty"`

	// ConfigError is set on a DEGRADED row (#1680): the tenant's conf.d file
	// exists but is not usable, so every other field except ID is empty —
	// its metadata is UNKNOWN, not unlabeled. Absent on a healthy row.
	// Values: unreadable (stat/read failed, e.g. a dangling symlink or a
	// permission error), not_regular_file (e.g. a symlink to a directory),
	// malformed_yaml (not parseable as YAML), invalid_config (parses as YAML
	// at the syntax level but cannot be loaded as a tenant config — wrong
	// shape, errors the YAML library only detects on a typed decode, such
	// as duplicate keys, or a declared tenant id that is not valid UTF-8,
	// which threshold-exporter rejects the whole file for). The first three
	// come from confd.FileProblem; invalid_config is decided by this handler.
	ConfigError string `json:"config_error,omitempty" enums:"unreadable,not_regular_file,malformed_yaml,invalid_config"`
	// Silent-mode / maintenance state DERIVED FROM CONFIG (「依設定推算」) at request time — what threshold-exporter would emit for this conf.d, not a reading from Alertmanager. Absent when it cannot be derived: a degraded row (config_error), a file the exporter skips, or conf.d not loading (see config_derivation on the search response).
	ConfigDerived *ConfigDerivedState `json:"config_derived,omitempty"`

	// Set when the row's metadata is unknown: the conf.d root platform files could not be read (the metadata fields then carry only the tenant file's own `_metadata` values), or the tenant's `_metadata` does not decode (the metadata fields are then empty, as threshold-exporter reads them). The row's visibility is that of a degraded row (only callers whose matching RBAC rule does not restrict environments or domains see it); the search metadata filters do not match it. Absent otherwise.
	MetadataIncomplete bool `json:"metadata_incomplete,omitempty"`
}

// metadataIsUnknown reports whether the row's environment/domain (and the
// rest of its metadata) are unknown rather than read: a degraded row
// (ConfigError, #1680), a healthy row read while the root platform layer
// could not be (#2370), or one whose `_metadata` does not decode (#2830).
func (t TenantSummary) metadataIsUnknown() bool {
	return t.ConfigError != "" || t.MetadataIncomplete
}

// ListTenants handles GET /api/v1/tenants
//
// v2.5.0 Phase C: Permission-filtered — only returns tenants the user has
// access to based on RBAC group rules (tenant patterns + environments + domains).
//
// #1680: a tenant whose conf.d file is not usable is returned as a degraded
// row (ID + config_error) instead of being dropped. Its environment/domain
// are unknown, so it is visible ONLY to a caller whose matching rule places
// no restriction on either metadata axis (rbac.ScopeAllowedUnknownMetadata).
//
// #1988: served from the snapshot cache shared with SearchTenants, and each
// tenant carries config_derived — its silent-mode / maintenance state as
// threshold-exporter would compute it from the same conf.d (config.LoadDir).
// The list has no envelope: the files the exporter skips as unparseable are
// named on the search response (a degraded row's config_error says the same
// thing per tenant, for the files this list reads).
//
// @Summary     List tenants
// @Description Returns tenants visible to the authenticated user, filtered by RBAC.
// @Description config_derived is derived from config at request time (「依設定推算」), not observed from Alertmanager.
// @Description A tenant whose config file is not usable is returned as a degraded row carrying only `id` and `config_error`
// @Description (unreadable | not_regular_file | malformed_yaml | invalid_config — parses as YAML at the syntax level but cannot be
// @Description loaded as a tenant config: wrong shape, errors only a typed decode detects, such as duplicate keys, or a declared
// @Description tenant id that is not valid UTF-8 — threshold-exporter skips such a file whole).
// @Description Its environment/domain are unknown, so the
// @Description row is visible only to callers whose matching RBAC rule does not restrict environments or domains.
// @Description When the conf.d root platform files cannot be read, every other row's metadata carries only the tenant file's own `_metadata`
// @Description values and the row is marked `metadata_incomplete`; its visibility is that of a degraded row.
// @Description A row whose `_metadata` does not decode is marked `metadata_incomplete` the same way, with empty metadata fields.
// @Tags        tenants
// @Produce     json
// @Success     200 {array}  TenantSummary
// @Failure     500 {object} ErrorResponse
// @Router      /api/v1/tenants [get]
func ListTenants(d *Deps) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		p := rbac.RequestPrincipal(r)

		snap, err := d.SearchCache.snapshot(r.Context(), d.ConfigDir)
		if err != nil {
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}

		// v2.5.0: Filter by RBAC (tenant pattern + env/domain metadata).
		// P4: also feeds the org-scope axis from _tenant_orgs.yaml (d.TenantOrg).
		filtered := filterTenantsByRBAC(snap.summaries, d.RBAC, d.TenantOrg, p)

		writeJSON(w, http.StatusOK, snap.withConfigDerived(filtered, time.Now()))
	}
}

// filterTenantsByRBAC returns only the tenants the caller has scope access to
// (metadata env/domain axis + org axis). If RBAC is in open mode (empty config),
// all tenants are returned. tenantOrg supplies each tenant's org list for the
// org axis; a nil manager is tolerated (OrgsForTenant is nil-receiver-safe) and
// yields unlabeled orgs, which with no org-scoped rule is byte-identical to the
// pre-P4 metadata-only filter.
//
// A DEGRADED row (ConfigError != "", #1680) — and, alike, a healthy row whose
// metadata is unknown because the root platform layer could not be read
// (#2370, metadataIsUnknown) — is decided by ScopeAllowedUnknownMetadata
// instead of ScopeAllowed. ⛔ Passing its empty
// Environment/Domain to ScopeAllowed would be a leak: ScopeAllowed reads an
// empty value as UNLABELED, which shadow metadata mode lets through, so an
// environment-restricted caller would see a broken tenant whose real — merely
// unreadable — environment they may not be allowed. Unknown is not unlabeled.
// The org list still comes from _tenant_orgs.yaml, which the broken file does
// not affect, so the org axis is evaluated normally.
func filterTenantsByRBAC(tenants []TenantSummary, rbacMgr *rbac.Manager, tenantOrg *tenantorg.Manager, p *rbac.VerifiedPrincipal) []TenantSummary {
	cfg := rbacMgr.Get()
	if len(cfg.Groups) == 0 {
		// Path-less open mode only. A configured-but-empty _rbac.yaml never
		// reaches here: the PermRead route gate (main.go) fail-closes it with
		// 403 before this filter runs (ADR-027 MED-8).
		return tenants // open mode — no filtering
	}

	filtered := make([]TenantSummary, 0, len(tenants))
	for _, t := range tenants {
		orgs, _ := tenantOrg.OrgsForTenant(t.ID)
		if t.metadataIsUnknown() {
			if rbacMgr.ScopeAllowedUnknownMetadata(p, t.ID, orgs) {
				filtered = append(filtered, t)
			}
			continue
		}
		if rbacMgr.ScopeAllowed(p, t.ID, t.Environment, t.Domain, orgs) {
			filtered = append(filtered, t)
		}
	}
	return filtered
}

// configErrorInvalidConfig is the one TenantSummary.ConfigError reason the
// handler decides rather than package confd: the bytes parse as YAML at the
// syntax level (confd.ReadTenantFile's generic yaml.Node parse calls the file
// usable — confd deliberately knows nothing about the threshold-exporter
// schema) but they cannot be loaded as a tenant config by the typed decode
// into cfg.ThresholdConfig. That covers a wrong shape (e.g. `tenants:`
// holding a list instead of a map) AND errors the YAML library only detects
// on a typed decode, such as a duplicate mapping key (`tenants:` twice).
// Same stability contract as the confd.FileProblem values.
//
// #2373: the typed decode is cfg.ParseTenantFile — the exact verdict
// threshold-exporter gives a tenant file — so it also covers a file that
// declares a tenant id that is not valid UTF-8 (#2266), which the exporter,
// /effective and da-guard reject WHOLE. A plain yaml.Unmarshal (the
// ParseConfigFile judgement) accepted it, so the list showed the file's other
// tenants as healthy although nothing serves them.
const configErrorInvalidConfig = "invalid_config"

// tenantConfigError is the config_error for a tenant file's bytes that were
// read successfully: malformed_yaml, invalid_config, or "" when the file is a
// usable tenant config. GET /tenants/{id} uses it; loadAllTenants reaches the
// same verdict through confd.ReadTenantFile + cfg.ParseTenantFile (one parse
// fewer per file on the snapshot rebuild).
func tenantConfigError(data []byte) string {
	if p := confd.YAMLProblem(data); p != confd.ProblemNone {
		return string(p)
	}
	if _, err := cfg.ParseTenantFile(data); err != nil {
		return configErrorInvalidConfig
	}
	return ""
}

// tenantFileNotLoadableError refuses a PARTIAL write (PUT .../custom-alerts,
// the tenant and group batch patches) into an existing tenant file that
// tenantConfigError rejects (#2373 review F1). Those writes merge the client's
// change into the file's current content, and GET cannot vouch for that
// content — it answers such a file with config_error and without the derived
// fields — so a client editing from GET (the portal's custom-alerts modal
// reads the missing list as []) would write its partial view back over the
// real one. The whole-file PUT /tenants/{id} does not go through here.
//
// The message names the whole-file PUT as the repair path, which holds for
// every config_error this refusal carries (#2405): that PUT replaces rather
// than merges, and its end-of-life guard treats a current file it cannot parse
// as having no end-of-life usage — so the replacement is not refused merely
// because the file it replaces is broken (a body adding an end-of-life recipe
// is still refused, as is one failing any other check). Pinned by
// TestPutTenant_RepairsFileExporterRejects.
type tenantFileNotLoadableError struct {
	TenantID string
	Reason   string
}

func (e *tenantFileNotLoadableError) Error() string {
	return fmt.Sprintf("tenant %s: its config file cannot be loaded as a tenant config (config_error: %s), "+
		"so threshold-exporter skips it and a partial update is refused; repair the tenant file itself first "+
		"(a whole-file PUT /api/v1/tenants/{id} replaces it)",
		e.TenantID, e.Reason)
}

// Unwrap lets package gitops recognise the refusal (errors.Is) without
// importing this package: WritePRBatch's pre-flight tolerates it.
func (e *tenantFileNotLoadableError) Unwrap() error { return gitops.ErrMergeBaseNotLoadable }

// checkPartialWriteBase returns a *tenantFileNotLoadableError when existing —
// tenantID's file that a partial write would merge into — is not a usable
// tenant config. Empty bytes (no file yet) are not refused: that is a new
// tenant.
func checkPartialWriteBase(tenantID string, existing []byte) error {
	if reason := tenantConfigError(existing); reason != "" {
		return &tenantFileNotLoadableError{TenantID: tenantID, Reason: reason}
	}
	return nil
}

// writeTenantFileNotLoadable answers a refused partial write: 409 with its own
// code (not CONFLICT: a refresh-and-retry cannot succeed), naming the tenant
// and the config_error.
func writeTenantFileNotLoadable(w http.ResponseWriter, r *http.Request, e *tenantFileNotLoadableError) {
	WriteErrorEnvelope(w, r, http.StatusConflict, ErrorResponse{
		Error: e.Error(),
		Code:  CodeTenantConfigNotLoadable,
		Extra: map[string]any{"tenant_id": e.TenantID, "config_error": e.Reason},
	})
}

// loadAllTenants scans configDir for tenant config files and extracts tenant
// summaries.
//
// Enumeration is confd.ListTenantFiles and usability is confd.ReadTenantFile
// — the one loop and the one classifier every conf.d caller shares (#1680).
// A file that is listed but not usable becomes a DEGRADED row (ID +
// ConfigError, no metadata) instead of being dropped. Dropping it used to make
// a tenant whose file broke vanish from GET /api/v1/tenants with a 200, while
// the federation planes, the startup guard and the write plane all still
// counted it — so the portal read "offboarded" for a tenant that was merely
// broken, and that no one could see needed repair.
func loadAllTenants(configDir string) ([]TenantSummary, error) {
	files, err := confd.ListTenantFiles(configDir)
	if err != nil {
		return nil, err
	}
	// #2370, #2830: a tenant's metadata is what /metrics reads for it
	// (cfg.MetadataResolver): the root platform files' `tenants.<id>._metadata`
	// merged per key under the tenant file's own, else the elected
	// profile's. One root read per
	// listing; when it cannot be read the rows carry the tenant files'
	// own `_metadata` alone and their metadata is unknown (MetadataIncomplete):
	// a key the platform layer would have set is missing, not unset — the
	// same as a degraded row's metadata (#1680).
	platform, platformKnown := platformMetadataOrNone(configDir, "list")
	// One resolver per listing: what every tenant shares (the profiles,
	// the carrier's optional_overrides) is computed once here (#2830).
	metadata := platform.MetadataResolver()

	summaries := []TenantSummary{}
	seen := make(map[string]string, len(files)) // tenant id → the file that claimed it

	for _, f := range files {
		tenantID, name := f.ID, f.Name

		// #1673: claim the id on the FILENAME alone, before any read or parse.
		// An unreadable or malformed sibling used to be skipped without ever
		// reserving its id, so a valid `<id>.yml` beside a broken `<id>.yaml`
		// was listed here while confd.ResolveTenantFile — which matches
		// names, not contents — answered 409 for the same tenant. Name-based
		// claiming keeps the two planes agreeing, which is the whole point of
		// this change.
		//
		// Refusing the whole listing rather than returning the tenant twice:
		// the two files can disagree on `_metadata` (so on env/domain scope)
		// and on thresholds, and a whole-file PUT silently drops whichever one
		// loses. threshold-exporter already hard-rejects this shape with a
		// typed *DuplicateTenantError.
		if prev, dup := seen[tenantID]; dup {
			return nil, fmt.Errorf("conf.d holds more than one config file for tenant %q: %s and %s", tenantID, prev, name)
		}
		seen[tenantID] = name

		data, problem := confd.ReadTenantFile(configDir, name)
		if problem != confd.ProblemNone {
			summaries = append(summaries, TenantSummary{ID: tenantID, ConfigError: string(problem)})
			continue
		}

		partial, err := cfg.ParseTenantFile(data)
		if err != nil {
			summaries = append(summaries, TenantSummary{ID: tenantID, ConfigError: configErrorInvalidConfig})
			continue
		}

		summary := TenantSummary{ID: tenantID}
		if overrides, ok := partial.Tenants[tenantID]; ok {
			if sv, exists := overrides["_silent_mode"]; exists {
				summary.SilentMode = sv.Default
			}
			if sv, exists := overrides["_state_maintenance"]; exists {
				summary.Maintenance = sv.Default
			}
			if sv, exists := overrides["_profile"]; exists {
				summary.Profile = sv.Default
			}
		}

		// v2.5.0: Extract _metadata fields for filtering and UI display.
		// partial is ParseConfigFile's decode (ParseTenantFile), the one
		// the resolver reads, so the file is not decoded a second time.
		// A `_metadata` that is written but does not decode reads as empty
		// fields (as on /metrics) and is marked metadata_incomplete, like an
		// unreadable root platform layer.
		decoded := true
		if overrides, ok := partial.Tenants[tenantID]; ok {
			var meta cfg.ResolvedMetadata
			meta, decoded = metadata.Resolve(tenantID, overrides)
			setMetadata(&summary, meta)
		}
		summary.MetadataIncomplete = !platformKnown || !decoded

		summaries = append(summaries, summary)
	}

	return summaries, nil
}

// setMetadata copies the metadata a cfg.MetadataResolver read for the tenant
// (#2830: /metrics' reading — the root platform files'
// `tenants.<id>._metadata` and the tenant document's own merged per key
// (#2370), else the elected profile's; the string form read like the mapping
// form; a non-string scalar read as its text) into summary's metadata fields.
func setMetadata(summary *TenantSummary, meta cfg.ResolvedMetadata) {
	summary.Environment = meta.Environment
	summary.Region = meta.Region
	summary.Tier = meta.Tier
	summary.Domain = meta.Domain
	summary.DBType = meta.DBType
	summary.Owner = meta.Owner
	if len(meta.Tags) > 0 {
		summary.Tags = meta.Tags
	}
	if len(meta.Groups) > 0 {
		summary.Groups = meta.Groups
	}
}
