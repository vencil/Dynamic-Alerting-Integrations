package handler

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"net/http"
	"reflect"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

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
	// Values: unreadable (stat/read failed, e.g. an unresolvable symlink —
	// dangling, looping or through a non-directory — or a permission
	// error), not_regular_file (e.g. a symlink to a directory),
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
		// reaches here: the route gate (MiddlewareAnyTenant, cmd/server/routes.go)
		// fail-closes it with 403 before this filter runs (ADR-027 MED-8).
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
// every file this refusal reaches: since #3064 a git-managed file (one that is
// not YAML, say) is refused first with TENANT_GIT_MANAGED and repaired in git,
// so only an API-shaped file gets here. That PUT replaces rather than merges;
// when its end-of-life guard cannot decode the current file's tenants: block,
// a caller with write permission on all tenants may replace it, the file
// counted as having no end-of-life usage (#2405) — a body adding an
// end-of-life recipe is still refused, as is one failing any other check —
// and any other caller is refused with 400. Pinned by
// TestPutTenant_RepairsFileExporterRejects.
type tenantFileNotLoadableError struct {
	TenantID string
	Reason   string
	// Detail (configErrorNotRoundTrippable only): where the merged file
	// first differs from the base with the edit applied — a path in the
	// tenant's own block, never another tenant's (verifyPartialWrite).
	Detail string
}

func (e *tenantFileNotLoadableError) Error() string {
	if e.Reason == configErrorNotRoundTrippable {
		return fmt.Sprintf("tenant %s: a partial update re-encodes the config file, and here it would also "+
			"change content the update does not edit (config_error: %s; first difference: %s); nothing was "+
			"written. Rewrite the tenant file in plain form (a whole-file PUT /api/v1/tenants/{id} replaces it)",
			e.TenantID, e.Reason, e.Detail)
	}
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
// tenant. (Whether the merge itself kept everything else is checked after
// it, on its result: verifyPartialWrite, #2903.)
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

// --- the partial-write invariant -------------------------------------
//
// #2903 (ADR-036 §4): a partial write — a batch patch (mergePatchYAML), PUT
// .../custom-alerts (customalerts.MergeCustomAlerts) — decodes the tenant
// file into yaml.v3 nodes, edits tenants.<id> and re-encodes the WHOLE file.
// The route generator only judges the result; it cannot tell a value the
// edit meant to change from one the re-encode changed on its own (a block
// scalar's leading blank line, a plain scalar the encoder now quotes, an
// alias that now names another anchor, an anchored block another tenant
// aliases).
//
// So the result must keep, outside what the edit replaces, the very same
// written YAML: parsed back into nodes, every node has the same kind, style
// (flow or block, plain or which quotes), tag as written (explicit or
// implicit), scalar text, anchor and — for an alias — the same target, node
// for node; the documents are as many, and the file defines the same anchors
// in the same order (yaml.v3 lets an anchor be defined twice, so removing one
// definition can re-point an alias to an equal-looking node). The same
// characters mean the same to every reader, whichever one reads them. On top
// of that, the edited keys of the tenant's block must, decoded, be exactly
// what the edit says (a set key holds its new value, an unset key is gone) —
// a `<<` merge key can still supply a key whose explicit entry was removed.
// Comments and positions are not compared.
//
// Anything else is refused before the write: config_error
// not_round_trippable, the first difference named only inside the written
// tenant's own block. The parser's own error text never reaches the client.

// configErrorNotRoundTrippable is the config_error of a partial write whose
// re-encode would change content the write does not edit.
const configErrorNotRoundTrippable = "not_round_trippable"

// partialEdit is one key of tenants.<id> a partial write changes. set with
// checkValue: the merged file must hold value there (a batch patch value,
// always a string, written double-quoted); set without checkValue: the key
// must be there, its value is the write's own (custom alerts, validated
// elsewhere); !set: the key must be gone.
type partialEdit struct {
	key        string
	set        bool
	checkValue bool
	value      string
}

// verifyPartialWrite returns a *tenantFileNotLoadableError (Reason
// configErrorNotRoundTrippable) unless merged keeps base's written YAML
// everywhere outside the edits to tenants.<tenantID>, and the edited keys
// read as the edits say. An empty base (a new file) has nothing to keep.
// names is the caller's tenantNamesFor: which other tenants' paths the
// refusal may name (describePath).
func verifyPartialWrite(base, merged []byte, tenantID string, edits []partialEdit, names gitops.TenantNames) error {
	if len(bytes.TrimSpace(base)) == 0 {
		return nil
	}
	refuse := func(detail string) error {
		return &tenantFileNotLoadableError{TenantID: tenantID, Reason: configErrorNotRoundTrippable, Detail: detail}
	}
	bDocs, err := parseAllDocs(base)
	if err != nil {
		return refuse("the current file does not parse") // the parser's text may quote other tenants' values
	}
	mDocs, err := parseAllDocs(merged)
	if err != nil {
		return refuse("the merged file would not parse")
	}
	if len(bDocs) != len(mDocs) {
		return refuse(fmt.Sprintf("the number of YAML documents (%d, would be %d)", len(bDocs), len(mDocs)))
	}
	if len(bDocs) == 0 {
		return refuse("the current file has no document")
	}
	if !reflect.DeepEqual(anchorsOf(bDocs), anchorsOf(mDocs)) {
		return refuse("the anchors the file defines (the edit would remove or add one)")
	}
	c := &nodeCompare{tenantID: tenantID, edits: edits, seen: map[[2]*yaml.Node]bool{}}
	for i := range bDocs {
		if path, ok := c.equal(bDocs[i], mDocs[i], nil, i == 0); !ok {
			return refuse(describePath(path, tenantID, names))
		}
	}
	if detail := c.checkEdits(); detail != "" {
		return refuse(detail)
	}
	return nil
}

func parseAllDocs(data []byte) ([]*yaml.Node, error) {
	dec := yaml.NewDecoder(bytes.NewReader(data))
	var docs []*yaml.Node
	for {
		n := new(yaml.Node)
		if err := dec.Decode(n); err != nil {
			if errors.Is(err, io.EOF) {
				return docs, nil
			}
			return nil, err
		}
		docs = append(docs, n)
	}
}

// nodeCompare walks base and merged in step. In document 1 it knows where
// the edit is: the root mapping's `tenants`, its tenantID pair (which the
// merge may append) and that pair's block, where the edited keys are skipped.
type nodeCompare struct {
	tenantID     string
	edits        []partialEdit
	seen         map[[2]*yaml.Node]bool
	mBlock       *yaml.Node // the merged block, for checkEdits
	blockMissing bool
}

func (c *nodeCompare) edited(key string) bool {
	for _, e := range c.edits {
		if e.key == key {
			return true
		}
	}
	return false
}

// sameScalarShape is the per-node part of the comparison.
func sameNode(b, m *yaml.Node) bool {
	return b.Kind == m.Kind && b.Style == m.Style && b.Tag == m.Tag && b.Value == m.Value && b.Anchor == m.Anchor
}

// equal reports whether b and m are the same written YAML; role says where
// the walk is in document 1 (0: elsewhere, 1: the root, 2: `tenants`, 3: the
// written tenant's block). On a difference it returns the path to it.
func (c *nodeCompare) equal(b, m *yaml.Node, path []string, root bool) ([]string, bool) {
	role := 0
	if root {
		role = 1
	}
	return c.eq(b, m, path, role)
}

func (c *nodeCompare) eq(b, m *yaml.Node, path []string, role int) ([]string, bool) {
	if b == nil || m == nil {
		return path, b == m
	}
	if b.Kind == yaml.DocumentNode && m.Kind == yaml.DocumentNode {
		if len(b.Content) != len(m.Content) {
			return path, false
		}
		for i := range b.Content {
			if p, ok := c.eq(b.Content[i], m.Content[i], path, role); !ok {
				return p, false
			}
		}
		return nil, true
	}
	if !sameNode(b, m) {
		return path, false
	}
	if b.Kind == yaml.AliasNode {
		if b.Alias == nil || m.Alias == nil {
			return path, b.Alias == m.Alias
		}
		key := [2]*yaml.Node{b.Alias, m.Alias}
		if c.seen[key] {
			return nil, true
		}
		c.seen[key] = true
		// The target, written the same, wherever it is — not just its name.
		return c.eq(b.Alias, m.Alias, path, 0)
	}
	if b.Kind != yaml.MappingNode {
		if len(b.Content) != len(m.Content) {
			return path, false
		}
		for i := range b.Content {
			if p, ok := c.eq(b.Content[i], m.Content[i], append(append([]string{}, path...), fmt.Sprintf("[%d]", i)), 0); !ok {
				return p, false
			}
		}
		return nil, true
	}
	bp, mp := pairs(b), pairs(m)
	switch role {
	case 2: // `tenants`: the merge may append the written tenant's pair.
		if keyIndex(bp, c.tenantID) < 0 {
			// ...unless the id is already there, written as an alias: the
			// merge would add a second key for it.
			if resolvedKeyCount(bp, c.tenantID) > 0 {
				return path, false
			}
			if j := keyIndex(mp, c.tenantID); j == len(mp)-1 {
				c.mBlock, c.blockMissing = mp[j][1], true
				mp = mp[:j]
			}
		}
	case 3: // the written tenant's block: skip the edited keys.
		bp, mp = c.withoutEdited(bp), c.withoutEdited(mp)
	}
	if len(bp) != len(mp) {
		return path, false
	}
	for i := range bp {
		kb, km := bp[i][0], mp[i][0]
		sub := append(append([]string{}, path...), kb.Value)
		if p, ok := c.eq(kb, km, sub, 0); !ok {
			return p, false
		}
		next := 0
		switch {
		case role == 1 && kb.Kind == yaml.ScalarNode && kb.Value == "tenants":
			next = 2
		case role == 2 && kb.Kind == yaml.ScalarNode && kb.Value == c.tenantID:
			next, c.mBlock = 3, mp[i][1]
		}
		if p, ok := c.eq(bp[i][1], mp[i][1], sub, next); !ok {
			return p, false
		}
	}
	return nil, true
}

func pairs(m *yaml.Node) [][2]*yaml.Node {
	out := make([][2]*yaml.Node, 0, len(m.Content)/2)
	for i := 0; i+1 < len(m.Content); i += 2 {
		out = append(out, [2]*yaml.Node{m.Content[i], m.Content[i+1]})
	}
	return out
}

func keyIndex(ps [][2]*yaml.Node, text string) int {
	for i, p := range ps {
		if p[0].Kind == yaml.ScalarNode && p[0].Value == text {
			return i
		}
	}
	return -1
}

// resolvedKeyCount is how many keys of ps are text, an alias followed.
func resolvedKeyCount(ps [][2]*yaml.Node, text string) int {
	n := 0
	for _, p := range ps {
		k := p[0]
		for k != nil && k.Kind == yaml.AliasNode {
			k = k.Alias
		}
		if k != nil && k.Kind == yaml.ScalarNode && k.Value == text {
			n++
		}
	}
	return n
}

func (c *nodeCompare) withoutEdited(ps [][2]*yaml.Node) [][2]*yaml.Node {
	out := make([][2]*yaml.Node, 0, len(ps))
	for _, p := range ps {
		if p[0].Kind == yaml.ScalarNode && c.edited(p[0].Value) {
			continue
		}
		out = append(out, p)
	}
	return out
}

// checkEdits: in the merged block, decoded (so a `<<` merge key counts),
// every set key holds its value and every unset key is gone; and a value the
// merge wrote is a double-quoted string.
func (c *nodeCompare) checkEdits() string {
	if c.mBlock == nil || c.mBlock.Kind != yaml.MappingNode {
		return fmt.Sprintf("tenants.%s would not be a mapping", c.tenantID)
	}
	var decoded map[string]any
	if err := c.mBlock.Decode(&decoded); err != nil {
		return fmt.Sprintf("tenants.%s would not decode", c.tenantID)
	}
	mp := pairs(c.mBlock)
	for _, e := range c.edits {
		where := fmt.Sprintf("tenants.%s.%s", c.tenantID, e.key)
		if resolvedKeyCount(mp, e.key) > 1 { // written twice, one an alias
			return where
		}
		v, present := decoded[e.key]
		switch {
		case !e.set:
			if present {
				return where
			}
		case !present:
			return where
		case e.checkValue:
			j := keyIndex(mp, e.key)
			if s, ok := v.(string); !ok || s != e.value || j < 0 || mp[j][1].Style != yaml.DoubleQuotedStyle {
				return where
			}
		}
	}
	return ""
}

// anchorsOf is every anchor name the documents define, in document order
// (keys included; an alias is not followed).
func anchorsOf(docs []*yaml.Node) []string {
	out := []string{}
	var walk func(n *yaml.Node)
	walk = func(n *yaml.Node) {
		if n == nil || n.Kind == yaml.AliasNode {
			return
		}
		if n.Anchor != "" {
			out = append(out, n.Anchor)
		}
		for _, k := range n.Content {
			walk(k)
		}
	}
	for _, d := range docs {
		walk(d)
	}
	return out
}

// describePath names a difference for the refusal message: a path in the
// written tenant's own block, or in the block of a tenant names shows
// (tenantNamesFor: one the caller may read), in full; anything else only as
// "another place in the file".
func describePath(path []string, tenantID string, names gitops.TenantNames) string {
	if len(path) >= 2 && path[0] == "tenants" && (path[1] == tenantID || names.Shown(path[1])) {
		return strings.Join(path, ".")
	}
	return "another place in the file"
}
