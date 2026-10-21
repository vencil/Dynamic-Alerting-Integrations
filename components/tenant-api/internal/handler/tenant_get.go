package handler

import (
	"errors"
	"fmt"
	"io/fs"
	"log/slog"
	"net/http"
	"sync"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"

	"github.com/go-chi/chi/v5"

	"github.com/vencil/tenant-api/internal/boundedcall"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/credmask"
	"github.com/vencil/tenant-api/internal/customalerts"
	"github.com/vencil/tenant-api/internal/errcode"
	"github.com/vencil/tenant-api/internal/errkind"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
)

// TenantDetail is the full tenant representation returned by GET /api/v1/tenants/{id}.
type TenantDetail struct {
	ID       string                  `json:"id"`
	RawYAML  string                  `json:"raw_yaml"`
	Resolved []cfg.ResolvedThreshold `json:"resolved_thresholds"`
	// Warnings is the BLOCKING validation set (KeyValidation.Errors) — what a
	// write of this exact file would be rejected on. It judges only the keys
	// the tenant's own file writes: a problem in a root platform file's
	// `tenants:` entry for this tenant (#2208), or in the profile the tenant
	// elects (#1385), is never here. Notices is the advisory set, which
	// never blocks a write: deprecated-key alias notices (#1231 — the file
	// keeps resolving and writing, but carries a spelling the author should
	// migrate), one notice per problem in a root platform file's entry for
	// this tenant, naming that file ("platform file <name>, entry
	// tenants.<id>: …"), and one per problem in the part of the elected
	// profile that reaches this tenant, naming the file and the profile
	// ("platform file <name>, profile "<p>" …") — the platform operator
	// fixes those there. Split fields so a client never has to text-parse
	// severity out of one list.
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
	// ConfigError is set when the tenant's file cannot be loaded as a tenant
	// config (#2373) — the same reason GET /api/v1/tenants reports on the
	// tenant's row: malformed_yaml (not parseable as YAML) or invalid_config
	// (parses as YAML but cannot be loaded as a tenant config — wrong shape,
	// errors only a typed decode detects, or a declared tenant id that is not
	// valid UTF-8). threshold-exporter skips such a file whole, so nothing is
	// derived from it: raw_yaml and source_hash are returned for an
	// API-managed tenant (so the file can be opened and fixed; withheld as
	// RawYAMLWithheld says) — a file that is not YAML is git-managed (#3064,
	// ManagedBy), so neither is returned for it — and resolved_thresholds, custom_alerts,
	// validation_warnings and validation_notices are ABSENT — not empty,
	// which would read as "none". Partial writes (PUT .../custom-alerts, the
	// batch patches) refuse such a file (TENANT_CONFIG_NOT_LOADABLE) until
	// the tenant file itself is repaired. Absent on a usable file.
	ConfigError string `json:"config_error,omitempty" enums:"malformed_yaml,invalid_config"`
	// Masked is true when the caller may read this tenant but not write it
	// (#1560): raw_yaml is then the file re-encoded with every receiver
	// credential (webhook / chat URLs, PagerDuty keys, passwords, tokens)
	// replaced by the placeholder "<masked: write permission required>" and
	// without comments, and custom_alerts is masked the same way. A PUT
	// carrying the placeholder is refused (400), so a masked copy cannot be
	// written back over the real values. source_hash is then empty: a hash
	// of the stored file would let a guess be checked offline.
	Masked bool `json:"masked,omitempty"`
	// RawYAMLWithheld is true, with raw_yaml, custom_alerts and source_hash
	// empty, when the caller would be shown the masked form but the file
	// cannot be masked with certainty: it uses an anchor or alias inside the
	// tenant's section (a credential can be placed outside any credential
	// field through one).
	RawYAMLWithheld bool `json:"raw_yaml_withheld,omitempty"`
	// ManagedBy is "api" when the tenant's conf.d file is API-shaped (#3064):
	// one YAML document declaring only this tenant (or none yet), under the
	// file's own name, with no anchor, alias, merge key (<<) or tagged key at
	// the top level or directly under tenants:. Such a file is the tenant's alone:
	// raw_yaml, source_hash and custom_alerts describe this tenant only, and
	// the writes accept it. "git" for every other file (shared with other
	// tenants, a name that is not the declared id, several documents, YAML
	// indirection where tenants are declared, not YAML): the tenant is
	// changed only in git, the answer carries the effective view alone —
	// resolved_thresholds and the validation fields (or config_error) — with
	// raw_yaml, source_hash and custom_alerts ABSENT, and every write is
	// refused with 409 TENANT_GIT_MANAGED.
	ManagedBy string `json:"managed_by" enums:"api,git"`
	// GitManagedReason says why the tenant is git-managed (managed_by: git):
	// not_tenant_document (not YAML, or its root or tenants: is not a
	// mapping), several_documents, yaml_indirection, tenant_count (several
	// tenants), id_mismatch (the one tenant is not the file's name). A
	// fixed code: it names no tenant. Absent when managed_by is api.
	GitManagedReason string `json:"git_managed_reason,omitempty" enums:"not_tenant_document,several_documents,yaml_indirection,tenant_count,id_mismatch"`
	// DeclaredElsewhere is true when, in the tree this server reads now, a
	// conf.d file other than the tenant's own declares the tenant id (#3172)
	// — the condition the #2078 write guard (409 TENANT_DECLARED_ELSEWHERE)
	// checks, judged as /diff judges it, on /diff's walk. It is not a promise
	// of a write's answer: a write can be refused earlier for another reason,
	// and a PR-mode write judges the latest base branch. Two cases: a
	// duplicate (the own file declares the id too), which threshold-exporter
	// refuses — the whole tree stays on its last good load; or declared only
	// elsewhere (the own file declares nothing for the id), which the
	// exporter loads from the other file. One bit only: it names no file and
	// no other tenant — /effective's 409 already gives a caller with read
	// permission the duplicate case. Absent when it could not be judged (the
	// conf.d walk failed or timed out; see the server log) — absent is not
	// false.
	DeclaredElsewhere *bool `json:"declared_elsewhere,omitempty"`
}

// Values of TenantDetail.ManagedBy (#3064).
const (
	managedByAPI = "api"
	managedByGit = "git"
)

// tenantDetailGitManaged is TenantDetail for a git-managed tenant (#3064):
// the same JSON names, minus every field read off the file's text (raw_yaml,
// source_hash, custom_alerts), which are absent. For a file that cannot be
// loaded (config_error), the derived fields are absent too, as on
// tenantDetailNotLoadable.
type tenantDetailGitManaged struct {
	ID string `json:"id"`
	// Resolved is a pointer so that "no thresholds" ([]) and "absent"
	// (config_error) stay two answers.
	Resolved         *[]cfg.ResolvedThreshold `json:"resolved_thresholds,omitempty"`
	Warnings         []string                 `json:"validation_warnings,omitempty"`
	Notices          []string                 `json:"validation_notices,omitempty"`
	ConfigError      string                   `json:"config_error,omitempty"`
	ManagedBy        string                   `json:"managed_by"`
	GitManagedReason string                   `json:"git_managed_reason"`
	// DeclaredElsewhere: as on TenantDetail.
	DeclaredElsewhere *bool `json:"declared_elsewhere,omitempty"`
}

// tenantDetailNotLoadable is TenantDetail for a file with a config_error: the
// same JSON names, minus every field derived from the file's content
// (resolved_thresholds, custom_alerts, validation_*), which are absent.
type tenantDetailNotLoadable struct {
	ID          string `json:"id"`
	RawYAML     string `json:"raw_yaml"`
	SourceHash  string `json:"source_hash"`
	ConfigError string `json:"config_error"`
	// Masked / RawYAMLWithheld / ManagedBy: as on TenantDetail (always
	// "api" here: a git-managed file is tenantDetailGitManaged).
	Masked          bool   `json:"masked,omitempty"`
	RawYAMLWithheld bool   `json:"raw_yaml_withheld,omitempty"`
	ManagedBy       string `json:"managed_by"`
	// DeclaredElsewhere: as on TenantDetail.
	DeclaredElsewhere *bool `json:"declared_elsewhere,omitempty"`
}

// sourceHashFor is the source_hash a caller is shown: empty when the caller
// is shown the masked form (#1560). The hash of the stored bytes confirms a
// guess of the file, credentials included, offline; the caller cannot write,
// so it has no use for the hash as a base_hash either.
func sourceHashFor(data []byte, masked bool) string {
	if masked {
		return ""
	}
	return cfg.ComputeSourceHash(data)
}

// rawYAMLFor returns what a caller is shown of an API-shaped tenant file's
// bytes (#3064: the file is the tenant's alone; a git-managed one is never
// read here): verbatim to one who can see credentials, masked to anyone
// else, and nothing (withheld) when the masked form cannot be built with
// certainty.
func rawYAMLFor(r *http.Request, d *Deps, tenantID string, data []byte) (raw string, masked, withheld bool) {
	if canSeeCredentials(r, d, tenantID) {
		return string(data), false, false
	}
	m, err := credmask.MaskYAML(data)
	if err != nil {
		return "", false, true
	}
	return string(m), true, false
}

// GetTenant handles GET /api/v1/tenants/{id}
//
// @Summary     Get tenant config
// @Description Returns the raw YAML and resolved thresholds for a single tenant.
// @Description managed_by says who changes the tenant (#3064). "api": its conf.d file (located by its name) is API-shaped —
// @Description one YAML document declaring only this tenant (or none yet), with no anchor, alias, merge key (<<) or tagged
// @Description key at the top level or directly under tenants: — so raw_yaml, source_hash and custom_alerts are that
// @Description tenant's alone. "git": any other file (shared with other tenants, a name that is not the declared id, several
// @Description documents, YAML indirection where tenants are declared, not YAML), with git_managed_reason; the answer is the
// @Description effective view only — resolved_thresholds and validation_warnings / validation_notices, or config_error —
// @Description and raw_yaml, source_hash and custom_alerts are ABSENT, for every caller. Every write to such a tenant is
// @Description refused with 409 TENANT_GIT_MANAGED; change it in git.
// @Description When an API-managed tenant's file cannot be loaded as a tenant config, the answer is still 200 with raw_yaml
// @Description and source_hash, plus `config_error` (invalid_config, as on the list row); threshold-exporter skips such a
// @Description file, so resolved_thresholds, custom_alerts and the validation fields are absent (not empty: the file's
// @Description content is not vouched for). Partial writes refuse such a file (409 TENANT_CONFIG_NOT_LOADABLE) until it is
// @Description repaired; a whole-file PUT can replace it. When the end-of-life check cannot decode the current file's
// @Description tenants: block, that PUT requires write permission on all tenants (an RBAC rule with tenants: ["*"] granting
// @Description write and no org-scope, environments or domains; a prefix pattern does not count) and is otherwise refused
// @Description with 400, file unchanged — fix it in git then; when allowed, its end-of-life recipe usage counts as none, so
// @Description a body with an end-of-life recipe is refused. A file that exists but cannot be read at all (permissions, I/O,
// @Description a directory) refuses the write.
// @Description A caller who may read the tenant but not write it gets raw_yaml with every receiver credential replaced by
// @Description "<masked: write permission required>" and comments removed, custom_alerts masked the same way and
// @Description source_hash empty (masked: true);
// @Description or raw_yaml, custom_alerts and source_hash empty with raw_yaml_withheld: true when the file uses an anchor or alias
// @Description (inside the tenant's own section: anywhere else makes the tenant git-managed).
// @Description declared_elsewhere (#3172), on every 200 shape: true when, in the tree this server reads now, a conf.d file
// @Description other than the tenant's own declares the tenant id — the condition of 409 TENANT_DECLARED_ELSEWHERE, judged
// @Description as /diff judges it. Not a promise of a write's answer: a write can be refused earlier for another reason, and
// @Description a PR-mode write judges the latest base branch. When the tenant's own file declares the id too (a duplicate),
// @Description threshold-exporter keeps the whole tree on its last good load; when only the other file declares it, the
// @Description exporter loads it from there. It names no file and no other tenant. Absent (not false) when the conf.d walk
// @Description behind it could not run.
// @Tags        tenants
// @Produce     json
// @Param       id   path     string true "Tenant ID"
// @Success     200  {object} TenantDetail
// @Failure     400  {object} ErrorResponse
// @Failure     404  {object} ErrorResponse
// @Failure     409  {object} ErrorResponse "Conflict: ambiguous tenant file (code TENANT_FILE_AMBIGUOUS), or the tenant's conf.d file is not a regular file (code TENANT_CONFIG_NOT_LOADABLE, config_error not_regular_file)"
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
			WriteKind(w, r, errkind.AmbiguousTenantFile, err.Error())
			return
		case err != nil:
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}
		// #2477: a tenant file that is not a regular file is answered at once
		// (409, config_error not_regular_file) instead of being read.
		data, err := confd.ReadRegularFile(filePath)
		if errors.Is(err, fs.ErrNotExist) {
			// Lost a race with a delete between resolve and read.
			WriteJSONError(w, r, http.StatusNotFound, "tenant not found: "+tenantID)
			return
		}
		if errors.Is(err, confd.ErrNotRegularFile) {
			writeTenantFileNotRegular(w, r, tenantID)
			return
		}
		if err != nil {
			WriteJSONError(w, r, http.StatusInternalServerError, err.Error())
			return
		}
		// #3172: whether another conf.d file declares the id — the #2078
		// guard's condition as /diff judges it, on the Writer's shared read
		// walk. Carried on every 200 shape below.
		elsewhere := declaredElsewhereFor(d, tenantID)

		// #2373: a file threshold-exporter rejects whole (cfg.ParseTenantFile
		// — not YAML, the wrong shape, or a non-UTF-8 declared tenant id) is
		// served as its content plus config_error, the same reason the list
		// row carries. Nothing is derived from it: the exporter serves none
		// of its tenants, so thresholds resolved from it would describe
		// values no plane holds. 200, not 4xx/5xx, so an editor can still
		// load the file (and its source_hash) to fix it. The derived fields
		// are ABSENT, not [] (#2373 review F1): an empty list reads as an
		// authoritative "none", and a client editing from it would write
		// that back over the file's real content (the partial writes refuse
		// such a file for the same reason — checkPartialWriteBase). Not null
		// either: the spec types them as arrays (Swagger 2.0 has no
		// nullable) and does not require them, so absence stays valid.
		// #3064: a git-managed tenant gets the effective view only.
		if gitReason := confd.TenantFileManagement(data, tenantID); gitReason != "" {
			getGitManagedTenant(w, r, d, tenantID, data, gitReason, elsewhere)
			return
		}
		if reason := tenantConfigError(data); reason != "" {
			raw, masked, withheld := rawYAMLFor(r, d, tenantID, data)
			writeJSON(w, http.StatusOK, tenantDetailNotLoadable{
				ID:                tenantID,
				RawYAML:           raw,
				SourceHash:        sourceHashFor(data, masked || withheld),
				ConfigError:       reason,
				Masked:            masked,
				RawYAMLWithheld:   withheld,
				ManagedBy:         managedByAPI,
				DeclaredElsewhere: elsewhere,
			})
			return
		}

		// Merge over the root platform surface: the defaults carrier, the
		// platform files' per-tenant `tenants:` layer (#2208) and the
		// profile the tenant elects (#1385).
		merged, err := d.loadMergedConfig(tenantID, data)
		if err != nil {
			slog.Error("tenant GET: conf.d root platform read failed", "tenant", tenantID, "error", err)
			WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, errcode.Internal, msgRootPlatformRead)
			return
		}

		// #1231 1b: two-channel split — validation_warnings stays Errors-only
		// (the blocking set), deprecation notices get their own field.
		// #3039: the file may declare other tenants; messages about those the
		// caller may not read are counted, not shown.
		warnings, notices := gitops.DiscloseKeyValidation(tenantID, merged.ValidateTenantKeysByTenant(),
			tenantNamesFor(d.RBAC, d.TenantOrg, rbac.RequestPrincipal(r)))
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
			// Unreachable since #3063. Extract decodes `tenants:` into generic
			// maps, so it fails only where a generic decode of the file does,
			// and tenantConfigError above answers every such file with
			// config_error: confd.YAMLProblem catches bytes that are not
			// YAML, and cfg.ParseTenantFile — the typed decode AND, since
			// #3063, the generic decode of the same document — rejects the
			// rest as invalid_config. Between #2373 and #3063 this was
			// reachable: ParseTenantFile used only the typed decode, which
			// accepts a scalar whose explicit tag its text does not satisfy
			// (`!!int abc`), so such a file reached here and answered 500.
			// Kept as a guard should Extract grow a failure ParseTenantFile
			// does not share. Surface it rather than returning a 200 with
			// silently-empty custom_alerts. Keep the raw parser error (which
			// can echo file contents) in the server log only; return a
			// stable, non-sensitive message to clients.
			slog.Error("failed to parse tenant custom alerts", "tenant", tenantID, "err", err)
			WriteJSONError(w, r, http.StatusInternalServerError, msgCustomAlertsUnparseable)
			return
		}

		// #1560: custom_alerts is the same file decoded, so it is masked the
		// same way — and when the masked file is withheld it is withheld
		// too: the decode resolved the aliases that made the file
		// unmaskable, so a credential can sit under any key in it.
		raw, masked, withheld := rawYAMLFor(r, d, tenantID, data)
		switch {
		case withheld:
			customAlerts = []map[string]any{}
		case masked:
			for i, recipe := range customAlerts {
				customAlerts[i], _ = credmask.MaskValue(recipe).(map[string]any)
			}
		}
		detail := TenantDetail{
			ID:                tenantID,
			RawYAML:           raw,
			Resolved:          tenantResolved,
			Warnings:          warnings,
			Notices:           notices,
			SourceHash:        sourceHashFor(data, masked || withheld),
			CustomAlerts:      customAlerts,
			Masked:            masked,
			RawYAMLWithheld:   withheld,
			ManagedBy:         managedByAPI,
			DeclaredElsewhere: elsewhere,
		}

		writeJSON(w, http.StatusOK, detail)
	}
}

// getGitManagedTenant answers GET /tenants/{id} for a git-managed tenant
// (#3064): the effective view — the thresholds and validation messages
// tenant-api derives for this tenant, as for an API-managed one — and
// nothing read off the file's text: no raw_yaml, no source_hash (it would
// confirm a guess of the whole shared file), no custom_alerts (an alias or a
// merge key can carry another tenant's content into it). A file that cannot
// be loaded answers config_error alone, as tenantDetailNotLoadable does.
func getGitManagedTenant(w http.ResponseWriter, r *http.Request, d *Deps, tenantID string, data []byte, gitReason string, elsewhere *bool) {
	out := tenantDetailGitManaged{ID: tenantID, ManagedBy: managedByGit, GitManagedReason: gitReason,
		DeclaredElsewhere: elsewhere}
	if reason := tenantConfigError(data); reason != "" {
		out.ConfigError = reason
		writeJSON(w, http.StatusOK, out)
		return
	}
	merged, err := d.loadMergedConfig(tenantID, data)
	if err != nil {
		slog.Error("tenant GET: conf.d root platform read failed", "tenant", tenantID, "error", err)
		WriteJSONErrorWithCode(w, r, http.StatusInternalServerError, errcode.Internal, msgRootPlatformRead)
		return
	}
	// #3039: the file may declare other tenants; messages about those the
	// caller may not read are counted, not shown.
	out.Warnings, out.Notices = gitops.DiscloseKeyValidation(tenantID, merged.ValidateTenantKeysByTenant(),
		tenantNamesFor(d.RBAC, d.TenantOrg, rbac.RequestPrincipal(r)))
	resolved := make([]cfg.ResolvedThreshold, 0)
	for _, rt := range merged.ResolveAt(time.Now()) {
		if rt.Tenant == tenantID {
			resolved = append(resolved, rt)
		}
	}
	out.Resolved = &resolved
	writeJSON(w, http.StatusOK, out)
}

// declaredElsewhereFor is GET's declared_elsewhere (#3172): the Writer's
// judgement (gitops.Writer.DeclaredElsewhere — the #2078 guard's condition,
// on the shared read walk), or nil when it cannot be given: no
// Writer (handler-test literals), or a walk that failed or timed out. A nil
// is omitted from the body, so an unjudged tree never reads as "no". The
// walk's error carries server paths: log only.
func declaredElsewhereFor(d *Deps, tenantID string) *bool {
	if d.Writer == nil {
		return nil
	}
	v, err := d.Writer.DeclaredElsewhere(tenantID)
	if err != nil {
		slog.Warn("tenant GET: cannot judge whether another conf.d file declares the tenant",
			"tenant", tenantID, "error", err)
		return nil
	}
	return &v
}

// msgCustomAlertsUnparseable is the fixed 500 text when a tenant's
// _custom_alerts cannot be extracted; the parser's own error stays in the log.
const msgCustomAlertsUnparseable = "failed to parse tenant custom alerts"

// msgRootPlatformRead is the fixed client-facing text for a GET whose read of
// the conf.d root platform files did not complete in time. The full error
// goes to the server log only.
const msgRootPlatformRead = "cannot read the conf.d root platform files in time; " +
	"the tenant's effective thresholds cannot be computed"

// defaultRootReadGuard is the process-wide bound behind Deps.RootReadGuard
// when that field is nil.
var defaultRootReadGuard = &boundedcall.Guard{}

// loadMergedConfig merges the tenant file over the conf.d root platform
// surface — the defaults carrier, the root platform files' per-tenant
// `tenants:` layer, and the profile the tenant elects from those files'
// `profiles:` (#1385; read in the same bounded read — no other file is
// opened for them). Same merge core as validate and the write gate
// (cfg.MergeTenantWithRootDefaults is exactly these two halves), so GET /
// validate / write all merge identically (the consolidation that closed the
// ADR-024 PR4 / #704 write-vs-read asymmetry).
//
// ⛔ THE ROOT READ IS BOUNDED AND SHARED (#2208, PR #2214 review). It reads
// every root `_*.yaml`, and a read that never returns (a hung mount)
// would hang this GET — before #2208 only a file named like the defaults
// carrier could. So:
//
//   - bounded: the read runs under RootReadGuard (boundedcall, the writer's
//     conf.d-walk mechanism, on its own guard so a stuck GET read does not
//     fail writes);
//   - shared: the read is tenant-independent, so concurrent GETs for one
//     conf.d join ONE in-flight read (rootReads) instead of each starting
//     their own. A stuck file therefore costs one stuck goroutine, not one
//     per GET that arrived inside the first timeout window; after it times
//     out, the guard fails new GETs at once until the read returns.
//
// ⚠️ WHAT A JOINER SEES. A GET that arrives while a read is in progress
// joins it and gets what the files said when THAT read started — not
// necessarily what they say when the GET arrived: a platform file rewritten
// after the read began is not seen by GETs that join it. That staleness
// window is at most the duration of the one read, and at most the guard's
// timeout. A GET that starts after the read FINISHED always reads again
// (nothing is kept; unchanged files still hit the content-hash decode
// cache). The per-tenant merge then runs on this goroutine — a panic there
// reaches chi's Recoverer, as before the bound.
func (d *Deps) loadMergedConfig(tenantID string, tenantData []byte) (cfg.TenantMerge, error) {
	guard := d.RootReadGuard
	if guard == nil {
		guard = defaultRootReadGuard
	}
	load := d.loadRoot
	if load == nil {
		load = cfg.LoadRootPlatform
	}
	// Keyed by guard as well as directory: callers with different bounds
	// (tests) must not wait on each other's read.
	key := fmt.Sprintf("%p\x00%s", guard, d.ConfigDir)
	root, err := rootReads.do(key, func() (cfg.RootPlatform, error) {
		// A panic in the read comes back as boundedcall.ErrPanicked (it runs
		// on boundedcall's goroutine, where chi's Recoverer cannot reach);
		// the caller logs it and answers with the same fixed 500.
		return boundedcall.Do(guard, func() cfg.RootPlatform { return load(d.ConfigDir) })
	})
	if err != nil {
		return cfg.TenantMerge{}, err
	}
	return cfg.MergeTenantOverRootPlatform(root, tenantID, tenantData), nil
}

// loadMergedConfig is the unbounded, unshared merge (see Deps.loadMergedConfig).
func loadMergedConfig(configDir, tenantID string, tenantData []byte) cfg.TenantMerge {
	return cfg.MergeTenantWithRootDefaults(configDir, tenantID, tenantData)
}

// rootReads is the process-wide in-flight set behind Deps.loadMergedConfig.
var rootReads = &rootReadFlight{}

// rootReadFlight merges concurrent calls with the same key into one: the
// first caller runs fn, the others wait for its result. A single-purpose
// singleflight (the module has no direct dependency on x/sync, and this
// needs nothing beyond "wait for the one in flight").
//
// ⛔ NOTHING IS REMEMBERED. The entry is removed the moment fn returns, so a
// call that starts afterwards runs fn again. Waiters get the leader's result
// exactly, error included — when the leader's read times out, every GET that
// joined it fails with it, and none of them started a read of its own.
//
// ⚠️ A joiner gets the result of a read that began BEFORE it arrived, so it
// sees the files as they were when the leader started reading them; a
// rewrite in between is invisible to it. The window is bounded by the one
// read's duration, hence by the guard's timeout. Pinned by
// TestGetTenant_ConcurrentGETsGetTheirOwnTenantAndFreshFiles (no read is
// reused once it finished) and TestRootReadFlightMergesOnlyWhatIsInFlight.
type rootReadFlight struct {
	mu    sync.Mutex
	calls map[string]*rootReadCall
}

type rootReadCall struct {
	done    chan struct{}
	val     cfg.RootPlatform
	err     error
	waiters int // joined callers, under rootReadFlight.mu (observability for tests)
}

func (f *rootReadFlight) do(key string, fn func() (cfg.RootPlatform, error)) (cfg.RootPlatform, error) {
	f.mu.Lock()
	if c, ok := f.calls[key]; ok {
		c.waiters++
		f.mu.Unlock()
		<-c.done
		return c.val, c.err
	}
	c := &rootReadCall{done: make(chan struct{})}
	if f.calls == nil {
		f.calls = make(map[string]*rootReadCall)
	}
	f.calls[key] = c
	f.mu.Unlock()

	// fn is boundedcall.Do, which neither panics nor outlives its bound, so
	// the entry is always removed and the waiters always released. The defer
	// keeps that true should fn ever change.
	defer func() {
		f.mu.Lock()
		delete(f.calls, key)
		f.mu.Unlock()
		close(c.done)
	}()
	c.err = errors.New("root platform read did not complete")
	c.val, c.err = fn()
	return c.val, c.err
}

// tenantIDFromPath is a helper for chi URL param extraction used by middleware.
func tenantIDFromPath(r *http.Request) string {
	return chi.URLParam(r, "id")
}

// TenantIDFromPath is the exported version for use in router setup.
var TenantIDFromPath = tenantIDFromPath
