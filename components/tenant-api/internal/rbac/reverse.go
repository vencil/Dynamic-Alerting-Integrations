package rbac

// Reverse access report (ADR-027 / LD-6 P6): answers "who can access tenant X,
// via which rule, under which conditions" by ENUMERATING the live config
// against the SAME unexported predicates the forward gates run.
//
// ⛔ AUDIT-ONLY DISCLAIMER: this report is an observability artifact, never an
// authorization input. Authorization decisions remain exclusively with the
// forward gates (Allowed / AllowedInOrg / AllowedInOrgRead / ScopeAllowed);
// nothing may branch on a ReverseReport to grant or deny a request.
//
// ⛔ SEMANTICS ARE BORROWED, NEVER RE-IMPLEMENTED: every match-shaped question
// this file answers is answered by calling the forward predicates —
// ruleMatches (via subjectFor), tenantMatches, ruleGrants, scopeSetModes — on
// real inputs. There is NO second copy of rule-matching, permission-coverage,
// tenant-pattern or org-membership logic here (rbac.go's ruleMatches iron
// rule). If the forward semantics change, this report follows automatically;
// a drift between the two is a bug in this file, pinned by the dogfood tests.
//
// WHO conditions are NOT evaluated: the report transcribes each rule's
// matcher verbatim (legacy group name, or the match: block's groups/claims
// with the full map[string][]string value lists — OR-within values,
// AND-across keys). Evaluating WHO would require enumerating principals,
// which is the forward gates' job at request time.
//
// ⚠️ PRESENCE-IMPLIES-MEMBERSHIP CAVEAT: for a rule that pins its org claim to
// literal values (OrgScope=K plus match.claims[K]), the mere PRESENCE of a
// grant entry for tenant X can reveal that X belongs to one of those orgs —
// even in the redacted projection, which strips the values but not the entry.
// RedactReverseReport cannot fully eliminate this inference; consumers of the
// redacted view must treat grant presence itself as weakly identifying. In the
// DEFAULT (full) view the exposure is direct, not inferential: a value-pinned
// rule's org values appear verbatim inside who.claims_all_of (the LOCKED
// verbatim-transcription semantics), with or without ?include=org_values.

import (
	"sort"
	"time"
)

// ── Enumerations (schema-stable string values) ────────────────────────────

// Report mode — how the current rbac snapshot answers "is anything enforced".
const (
	// ReverseModeRules: a non-empty rule set is loaded; grants enumerate it.
	ReverseModeRules = "rules"
	// ReverseModeOpenRead: zero groups WITHOUT fail-closed (path-less open
	// mode) — grants is empty but ANY authenticated caller can read. The
	// report must say so rather than lie that nobody can touch the tenant.
	ReverseModeOpenRead = "open_read"
	// ReverseModeFailClosedEmpty: zero groups WITH fail-closed (ADR-027
	// MED-8: configured-but-empty _rbac.yaml) — all access is denied.
	ReverseModeFailClosedEmpty = "fail_closed_empty"
)

// Top-level machine verdict (axis-3 schema lock point D1; the future CLI's
// exit-code semantics key off this single field).
const (
	ReverseVerdictGrantsFound     = "grants_found"
	ReverseVerdictNoGrants        = "no_grants"
	ReverseVerdictOpenRead        = "open_read"
	ReverseVerdictFailClosedEmpty = "fail_closed_empty"
)

// WHO kinds — which matcher shape the rule uses (transcribed, not evaluated).
const (
	WhoKindLegacyGroup = "legacy_group" // no match: block; the rule Name IS the IdP group
	WhoKindMatchBlock  = "match_block"  // claims-aware match: block (P3)
)

// Org-gate outcomes, one per fail-mode (flag-agnostic dual view: the reader
// projects the effective one via flags.org_scope_enforce).
const (
	OrgOutcomeNotRequired   = "not_required"              // rule has no org-scope
	OrgOutcomeConditional   = "conditional_on_caller_org" // labeled tenant: depends on the caller's org claim value
	OrgOutcomePassUnlabeled = "pass_unlabeled"            // unlabeled tenant, shadow leniency passes
	OrgOutcomeFailUnlabeled = "fail_unlabeled"            // unlabeled tenant, enforce denies
)

// Tenant org labeling status (tenantorg.OrgsForTenant's known bit, reserved
// for P6 there): labeled = listed with ≥1 org; unlabeled = listed with an
// empty list (created-but-unassigned); not_onboarded = absent from
// _tenant_orgs.yaml entirely.
const (
	OrgStatusLabeled      = "labeled"
	OrgStatusUnlabeled    = "unlabeled"
	OrgStatusNotOnboarded = "not_onboarded"
)

// Redacted pattern kinds (tenant_pattern verbatim is stripped; only its shape
// survives). A rule-level wildcard (platform_wide) downgrades to wildcard even
// when the recorded first-hit pattern is a narrower member of a mixed list —
// the kind must not contradict the platform_wide boolean it sits next to.
const (
	PatternKindWildcard = "wildcard" // "*" (or platform_wide rule, see above)
	PatternKindPrefix   = "prefix"   // "<literal>-*"
	PatternKindLiteral  = "literal"  // exact tenant id
)

// Grant surfaces — which decision planes an effective permission acts on.
// env/domain annotations constrain the list surface only (axis-2 note).
const (
	SurfaceList     = "list"
	SurfaceReadByID = "read_by_id"
	SurfaceWrite    = "write"
	SurfaceAdmin    = "admin"
)

// AnchorUnanchored is the config_anchor value when no trustworthy hash exists
// (Override/NewForTest snapshots have no file hash; a torn snapshot/hash pair
// that a retry could not reconcile is reported the same way). NEVER an empty
// string pretending to be an anchor.
const AnchorUnanchored = "unanchored"

// provenanceRuntime marks a value as read from the live process at report
// time (axis-3 D2: runtime fields carry provenance so a future offline/CLI
// rendering can honestly mark its values as snapshot-derived instead).
const provenanceRuntime = "runtime"

// ReverseAdvisory is the fixed audit-only banner every report carries.
const ReverseAdvisory = "audit-only; authorization decisions remain with forward gates"

// reverseSchemaVersion pins the report's JSON schema generation.
const reverseSchemaVersion = 1

// ── Report structs (the swagger @Success shape; JSON contract spec §3) ────

// BoolProvenance is a runtime boolean plus where it came from.
type BoolProvenance struct {
	Value  bool   `json:"value"`
	Source string `json:"source"`
}

// StringProvenance is a runtime string plus where it came from.
type StringProvenance struct {
	Value  string `json:"value"`
	Source string `json:"source"`
}

// ReverseFlags reports the per-axis enforce flags in effect when the report
// was generated. Flag values are excluded from the P7 dry-run diff semantics
// (the dual outcome_shadow/outcome_enforce fields are the diffable surface).
type ReverseFlags struct {
	MetadataScopeEnforce BoolProvenance `json:"metadata_scope_enforce"`
	OrgScopeEnforce      BoolProvenance `json:"org_scope_enforce"`
}

// ReverseConfigAnchor pins the report to the exact config bytes it enumerated
// (SHA-256 of _rbac.yaml, and of _tenant_orgs.yaml as supplied by the
// caller). Either value may be AnchorUnanchored — never an empty string.
type ReverseConfigAnchor struct {
	RBACSHA256       StringProvenance `json:"rbac_sha256"`
	TenantOrgsSHA256 StringProvenance `json:"tenant_orgs_sha256"`
}

// ReverseTenant identifies the queried tenant and its org labeling status.
// Orgs appears ONLY under ReverseReportOptions.IncludeOrgValues (the opt-in
// org-value expansion); the default view carries the three-state OrgStatus
// enum only.
type ReverseTenant struct {
	ID        string   `json:"id"`
	OrgStatus string   `json:"org_status"`
	Orgs      []string `json:"orgs,omitempty"` // opt-in; sorted; absent by default
}

// CoverageGap is one surface the report does NOT cover, with its status —
// either a runtime reading (dev-bypass active/inactive) or "by_design".
type CoverageGap struct {
	Surface string `json:"surface"`
	Status  string `json:"status"`
}

// ReverseCompleteness declares what the enumeration covers and — more
// importantly for an auditor — what it does not.
type ReverseCompleteness struct {
	Covers     []string      `json:"covers"`
	NotCovered []CoverageGap `json:"not_covered"`
}

// EffectivePerms is the ruleGrants expansion of a rule's permission list
// under the admin ⊇ write ⊇ read hierarchy.
type EffectivePerms struct {
	Read  bool `json:"read"`
	Write bool `json:"write"`
	Admin bool `json:"admin"`
}

// ReverseWho transcribes a rule's matcher VERBATIM — it is config prose, not
// an evaluation. ClaimsAllOf keeps the full map[string][]string shape
// (OR-within a value list, AND-across keys); flattening it would misstate the
// match semantics. GroupsCount/ClaimsCount appear only in the redacted
// projection, replacing the stripped identifiers.
type ReverseWho struct {
	Kind        string              `json:"kind"`
	LegacyGroup string              `json:"legacy_group,omitempty"`
	GroupsAnyOf []string            `json:"groups_any_of,omitempty"`
	ClaimsAllOf map[string][]string `json:"claims_all_of,omitempty"`
	GroupsCount *int                `json:"groups_count,omitempty"` // redacted view only
	ClaimsCount *int                `json:"claims_count,omitempty"` // redacted view only
}

// ReverseOrgGate reports a rule's org-scope axis for the queried tenant in
// BOTH fail-modes at once (flag-agnostic; the reader projects the effective
// outcome via flags.org_scope_enforce). PassingOrgValues is derived by
// ENUMERATION — each candidate org value is run through the real ruleMatches
// + scopeSetModes, so an OrgScope=K rule that also pins match.claims[K]
// yields the intersection naturally. An empty derived set on a LABELED tenant
// marks the grant Unsatisfiable: no caller org value can ever pass this gate
// for this tenant.
type ReverseOrgGate struct {
	Required         bool     `json:"required"`
	ClaimKey         string   `json:"claim_key,omitempty"`
	OutcomeShadow    string   `json:"outcome_shadow"`
	OutcomeEnforce   string   `json:"outcome_enforce"`
	Unsatisfiable    bool     `json:"unsatisfiable"`
	PassingOrgValues []string `json:"passing_org_values,omitempty"` // opt-in (IncludeOrgValues); sorted
}

// ReverseGrant is ONE rule's grant to the queried tenant. Grants are emitted
// per rule, in config order, and NEVER merged across rules — a cross-rule
// union would fabricate access no single rule grants (the exact hazard
// TestAllowedInOrg_CrossRuleUnionNoLeak pins on the forward path). Identity
// is Index (rules may share a Name).
type ReverseGrant struct {
	Index         int    `json:"index"`
	Rule          string `json:"rule,omitempty"`           // stripped in the redacted view
	TenantPattern string `json:"tenant_pattern,omitempty"` // the pattern that hit X; stripped when redacted
	PatternKind   string `json:"pattern_kind,omitempty"`   // redacted view only: literal|prefix|wildcard
	// PlatformWide is RULE-level: true iff ANY pattern in the rule's tenant
	// list is the literal "*" (the same tenantMatches(rule.Tenants, "*") borrow
	// the endpoint bar runs) — NOT a property of the first-hit TenantPattern,
	// which stays the narrower member on a mixed list like ["db-team-1","*"].
	PlatformWide bool           `json:"platform_wide"`
	Permissions  []Permission   `json:"permissions"` // rule verbatim
	Effective    EffectivePerms `json:"effective"`   // ruleGrants expansion
	Who          ReverseWho     `json:"who"`
	OrgGate      ReverseOrgGate `json:"org_gate"`
	// Environments/Domains transcribe the rule verbatim; NOT evaluated against
	// tenant labels (axis 2). The redacted view STRIPS the verbatim strings —
	// they are free-form text that can carry customer-recognizable markers, and
	// the redacted audience is wider than platform admins — keeping the keys
	// (emptied) and carrying the sizes in EnvironmentsCount/DomainsCount
	// (redacted view only, mirroring who.groups_count/claims_count).
	Environments      []string `json:"environments"`
	EnvironmentsCount *int     `json:"environments_count,omitempty"` // redacted view only
	Domains           []string `json:"domains"`
	DomainsCount      *int     `json:"domains_count,omitempty"` // redacted view only
	Surfaces          []string `json:"surfaces"`
	// ConstraintsNotEvaluated is the fixed machine-readable declaration that
	// this report transcribes environments/domains but never evaluates them
	// against the tenant's actual labels (axis-2 decision 2b) — a reader must
	// not treat a transcribed constraint as a verified restriction.
	ConstraintsNotEvaluated []string `json:"constraints_not_evaluated"`
}

// ReverseReport is the full reverse-access report for one tenant. This struct
// IS the endpoint's @Success schema; field additions bump reverseSchemaVersion
// only when the change is not backward-compatible.
//
// P7 dry-run diff contract: GeneratedAt and Flags are excluded from diff
// semantics; the dual outcome_shadow/outcome_enforce fields are the diffable
// surface (they are flag-agnostic by construction).
type ReverseReport struct {
	SchemaVersion int                 `json:"schema_version"`
	Advisory      string              `json:"advisory"`
	GeneratedAt   string              `json:"generated_at"`
	Verdict       string              `json:"verdict"`
	Mode          string              `json:"mode"`
	Flags         ReverseFlags        `json:"flags"`
	ConfigAnchor  ReverseConfigAnchor `json:"config_anchor"`
	Tenant        ReverseTenant       `json:"tenant"`
	Completeness  ReverseCompleteness `json:"completeness"`
	Grants        []ReverseGrant      `json:"grants"`
}

// ReverseReportOptions controls the optional expansions of a report.
type ReverseReportOptions struct {
	// IncludeOrgValues opts in to the org-value expansion (?include=org_values):
	// tenant.orgs and each grant's passing_org_values appear. Scope of the
	// guarantee: it covers exactly the org values DERIVED from _tenant_orgs
	// (tenant.orgs / passing_org_values) — those are absent from the default
	// view, which carries the org_status enum and the org-gate outcomes. Org
	// values PINNED VERBATIM in a rule's match.claims still appear in the
	// default full view via who.claims_all_of — the unavoidable consequence of
	// the LOCKED transcribe-verbatim WHO semantics, which this option neither
	// controls nor claims to.
	IncludeOrgValues bool
	// DevBypassActive is the runtime state of --dev-bypass-auth (ADR-022),
	// surfaced in completeness.not_covered as active|inactive. It is
	// CALLER-INJECTED because the runtime signal lives in the handler package
	// (handler.SetDevBypassActive's atomic gauge) and rbac must not import
	// handler; passing it per call keeps the value runtime-fresh (round-1 C7:
	// dynamic, not a compile-time string) with zero new Manager state. The
	// endpoint handler reads its package gauge; a future CLI/dry-run caller
	// supplies its own truthful value.
	DevBypassActive bool
}

// ── Core API ──────────────────────────────────────────────────────────────

// ReverseAccessReport builds the reverse-access report for tenantID.
//
// orgs / orgsKnown are tenantorg.OrgsForTenant(tenantID) and tenantOrgsHash is
// the tenantorg manager's LastHash(), all resolved and injected by the caller
// — rbac does not import tenantorg (the SetOrgResolver seam convention). An
// empty tenantOrgsHash is reported as AnchorUnanchored.
//
// Snapshot pairing (round-1 C6): the rbac config hash is read before and
// after taking the snapshot; on mismatch the pair is re-taken once, and a
// still-torn (or absent — Override/NewForTest) hash is reported as
// AnchorUnanchored rather than an empty string posing as an anchor. The
// enumeration itself always runs against the ONE snapshot taken here.
//
// Grant inclusion: a rule contributes a grant iff its tenant pattern hits
// tenantID (tenantMatches, per pattern), it grants at least the weakest
// permission (ruleGrants(rule, PermRead) — any valid grant implies read under
// the admin ⊇ write ⊇ read hierarchy), AND its WHO is not statically dead
// (the rule's own minimal witness passes ruleMatches — see the guard in the
// loop). WHO is transcribed, never evaluated against any real caller.
//
// Determinism: grants are emitted in config-index order; derived value sets
// (tenant.orgs, passing_org_values) are sorted and de-duplicated; verbatim
// slices keep their config order (they are prose, and config order is itself
// deterministic).
//
// Modes (round-1 C4) — the report never lies about an empty rule set:
// zero groups without fail-closed is open_read (any authenticated caller can
// read even though grants is empty); with fail-closed it is
// fail_closed_empty. Both make the LOCKED endpoint bar unsatisfiable, so the
// endpoint is unreachable in those states — but this core function must stay
// honest because P7 dry-run and a future CLI call it directly, with no bar in
// front.
func (m *Manager) ReverseAccessReport(tenantID string, orgs []string, orgsKnown bool,
	tenantOrgsHash string, opts ReverseReportOptions) ReverseReport {

	// Snapshot pairing: hash → snapshot → hash, one retry, else unanchored.
	rbacHash := m.LastHash()
	cfg := m.Get()
	if h2 := m.LastHash(); h2 != rbacHash {
		rbacHash = h2
		cfg = m.Get()
		if h3 := m.LastHash(); h3 != rbacHash {
			rbacHash = ""
		}
	}
	rbacAnchor := AnchorUnanchored
	if rbacHash != "" {
		rbacAnchor = rbacHash
	}
	orgAnchor := AnchorUnanchored
	if tenantOrgsHash != "" {
		orgAnchor = tenantOrgsHash
	}

	mode := ReverseModeRules
	if len(cfg.Groups) == 0 {
		if m.failClosedOnEmpty {
			mode = ReverseModeFailClosedEmpty
		} else {
			mode = ReverseModeOpenRead
		}
	}

	tenant := ReverseTenant{ID: tenantID, OrgStatus: orgStatusOf(orgs, orgsKnown)}
	if opts.IncludeOrgValues && len(orgs) > 0 {
		tenant.Orgs = sortedUnique(orgs)
	}

	grants := make([]ReverseGrant, 0)
	if mode == ReverseModeRules {
		for i := range cfg.Groups {
			rule := &cfg.Groups[i]
			pat, hit := matchingTenantPattern(rule.Tenants, tenantID)
			if !hit {
				continue
			}
			// Statically-dead WHO guard: if the rule's OWN minimal witness cannot
			// pass ruleMatches, NO principal ever can — the shapes that trip this
			// (an empty match block, an empty claim value list) are rejected by
			// validateConfig at load and can only arrive via a snapshot injected
			// around the loader (Override / NewForTest). Listing such a rule would
			// render a forward-always-deny rule as a grant whose WHO shows no
			// conditions — inviting the exact "empty means everyone" fail-open
			// misreading ruleMatches' defense-in-depth branch exists to forbid.
			// Decided BY the borrowed predicate on a synthesized witness (no
			// second copy of match logic); the dogfood witness-positive invariant
			// pins that every emitted grant stays witness-satisfiable.
			if !subjectFor(witnessPrincipal(rule)).ruleMatches(rule) {
				continue
			}
			eff := EffectivePerms{
				Read:  ruleGrants(rule, PermRead),
				Write: ruleGrants(rule, PermWrite),
				Admin: ruleGrants(rule, PermAdmin),
			}
			if !eff.Read {
				continue // rule mentions the tenant but grants nothing (no valid permission)
			}
			grants = append(grants, ReverseGrant{
				Index:                   i,
				Rule:                    rule.Name,
				TenantPattern:           pat,
				PlatformWide:            tenantMatches(rule.Tenants, "*"),
				Permissions:             copyPermissions(rule.Permissions),
				Effective:               eff,
				Who:                     whoOf(rule),
				OrgGate:                 orgGateFor(rule, orgs, opts.IncludeOrgValues),
				Environments:            copyStrings(rule.Environments),
				Domains:                 copyStrings(rule.Domains),
				Surfaces:                surfacesOf(eff),
				ConstraintsNotEvaluated: []string{"environments", "domains"},
			})
		}
	}

	verdict := ReverseVerdictNoGrants
	switch {
	case mode == ReverseModeOpenRead:
		verdict = ReverseVerdictOpenRead
	case mode == ReverseModeFailClosedEmpty:
		verdict = ReverseVerdictFailClosedEmpty
	case len(grants) > 0:
		verdict = ReverseVerdictGrantsFound
	}

	devBypassStatus := "inactive"
	if opts.DevBypassActive {
		devBypassStatus = "active"
	}

	return ReverseReport{
		SchemaVersion: reverseSchemaVersion,
		Advisory:      ReverseAdvisory,
		GeneratedAt:   time.Now().UTC().Format(time.RFC3339),
		Verdict:       verdict,
		Mode:          mode,
		Flags: ReverseFlags{
			MetadataScopeEnforce: BoolProvenance{Value: m.metadataScopeEnforce, Source: provenanceRuntime},
			OrgScopeEnforce:      BoolProvenance{Value: m.orgScopeEnforce, Source: provenanceRuntime},
		},
		ConfigAnchor: ReverseConfigAnchor{
			RBACSHA256:       StringProvenance{Value: rbacAnchor, Source: provenanceRuntime},
			TenantOrgsSHA256: StringProvenance{Value: orgAnchor, Source: provenanceRuntime},
		},
		Tenant: tenant,
		Completeness: ReverseCompleteness{
			Covers: []string{"rbac_rules", "org_scope_read_write_list", "platform_wildcard_rules"},
			NotCovered: []CoverageGap{
				{Surface: "dev_bypass_auth (ADR-022)", Status: devBypassStatus},
				{Surface: "metadata_scope effective evaluation (env/domain vs tenant labels)", Status: "by_design"},
			},
		},
		Grants: grants,
	}
}

// PlatformAdminNonOrgScoped is the LOCKED authorization bar for the reverse
// endpoint (owner decision §0.1): the caller passes iff at least ONE rule
// satisfies ruleMatches && tenantMatches(rule.Tenants, "*") &&
// ruleGrants(rule, PermAdmin) && rule.OrgScope == "" — a NON-org-scoped
// platform-wide admin grant, checked per rule with the SAME borrowed
// predicates as everything else in this file.
//
// This is deliberately TIGHTER than Allowed(p, "*", PermAdmin): the bare
// check has an org-blind seam — an ORG-SCOPED wildcard admin rule always
// passes it (Allowed evaluates the shadow component with tenantOrgs=nil, and
// flipping org enforce changes nothing on the platform-scope query), which
// would let a merely-org-scoped admin read the platform-wide access map.
//
// In both zero-group states (open_read / fail_closed_empty) no rule exists to
// satisfy the bar, so it returns false — the endpoint answers 403 even though
// open_read mode grants forward read access; the audit surface is
// intentionally narrower than the data surface.
func (m *Manager) PlatformAdminNonOrgScoped(p *VerifiedPrincipal) bool {
	cfg := m.Get()
	subject := subjectFor(p)
	for i := range cfg.Groups {
		rule := &cfg.Groups[i]
		if rule.OrgScope != "" {
			continue
		}
		if !subject.ruleMatches(rule) {
			continue
		}
		if !tenantMatches(rule.Tenants, "*") {
			continue
		}
		if !ruleGrants(rule, PermAdmin) {
			continue
		}
		return true
	}
	return false
}

// RedactReverseReport projects a report for eyes wider than platform admins
// (?view=redacted) by ALLOWLIST REBUILD: a brand-new ReverseReport is
// constructed and ONLY the allowlisted fields are copied over — never a
// struct copy followed by field deletion, which fails open the moment a new
// sensitive field is added (denylist-after-merge hazard).
//
// Kept: the schema skeleton (version/advisory/timestamps/mode/verdict/flags/
// config anchors/completeness), enums, booleans, counts, indexes,
// permissions, effective, surfaces, org-gate outcomes + unsatisfiable, and
// whether the pattern is platform-wide.
//
// Stripped (the three identifier classes): rule names + legacy group names +
// groups_any_of (group identifiers, replaced by groups_count), claims_all_of
// keys AND values including org_gate.claim_key (claim identifiers, replaced
// by claims_count), and every org value (tenant.orgs, passing_org_values).
// tenant_pattern verbatim is downgraded to pattern_kind
// (literal|prefix|wildcard). The rules' environments/domains verbatim strings
// are ALSO stripped (owner ruling): they are free-form text that can carry
// customer-recognizable markers, and the redacted reader surface is wider —
// the keys stay (emptied, schema stability) and environments_count /
// domains_count carry the sizes.
//
// ⚠️ presence-implies-membership: stripping values does NOT strip the
// inference that a value-pinned org rule's grant entry existing at all
// implies the tenant's org membership — see the file-header caveat.
//
// Idempotent: redacting an already-redacted report preserves the counts and
// pattern kind instead of recomputing them from the (now absent) originals.
func RedactReverseReport(r ReverseReport) ReverseReport {
	out := ReverseReport{
		SchemaVersion: r.SchemaVersion,
		Advisory:      r.Advisory,
		GeneratedAt:   r.GeneratedAt,
		Verdict:       r.Verdict,
		Mode:          r.Mode,
		Flags:         r.Flags,        // value struct, no identifiers
		ConfigAnchor:  r.ConfigAnchor, // hashes are anchors, not identifiers
		Tenant: ReverseTenant{
			ID:        r.Tenant.ID,
			OrgStatus: r.Tenant.OrgStatus,
			// Orgs deliberately dropped (org values).
		},
		Completeness: ReverseCompleteness{
			Covers:     copyStrings(r.Completeness.Covers),
			NotCovered: append([]CoverageGap(nil), r.Completeness.NotCovered...),
		},
		Grants: make([]ReverseGrant, 0, len(r.Grants)),
	}
	for _, g := range r.Grants {
		// Idempotent counts (same convention as redactWho): an already-redacted
		// input carries the counts and empty verbatim lists — the existing
		// counts win over recomputation from the emptied originals.
		envCount, domCount := len(g.Environments), len(g.Domains)
		if g.EnvironmentsCount != nil {
			envCount = *g.EnvironmentsCount
		}
		if g.DomainsCount != nil {
			domCount = *g.DomainsCount
		}
		out.Grants = append(out.Grants, ReverseGrant{
			Index: g.Index,
			// Rule name deliberately dropped; identity is Index.
			PatternKind:  patternKindOf(g),
			PlatformWide: g.PlatformWide,
			Permissions:  copyPermissions(g.Permissions),
			Effective:    g.Effective,
			Who:          redactWho(g.Who),
			OrgGate: ReverseOrgGate{
				Required: g.OrgGate.Required,
				// ClaimKey deliberately dropped (claim identifier).
				OutcomeShadow:  g.OrgGate.OutcomeShadow,
				OutcomeEnforce: g.OrgGate.OutcomeEnforce,
				Unsatisfiable:  g.OrgGate.Unsatisfiable,
				// PassingOrgValues deliberately dropped (org values).
			},
			// Verbatim env/domain strings deliberately dropped (see the doc
			// comment above): keys stay, emptied; counts replace the content.
			Environments:            []string{},
			EnvironmentsCount:       &envCount,
			Domains:                 []string{},
			DomainsCount:            &domCount,
			Surfaces:                copyStrings(g.Surfaces),
			ConstraintsNotEvaluated: copyStrings(g.ConstraintsNotEvaluated),
		})
	}
	return out
}

// ── Internal helpers (all borrow forward predicates; no second match logic) ─

// matchingTenantPattern returns the FIRST pattern (config order) that matches
// tenantID, deciding each pattern with the real tenantMatches predicate run
// on a single-element list — the matching GRAMMAR is never re-implemented
// here, only enumerated pattern-by-pattern to name which one hit.
func matchingTenantPattern(patterns []string, tenantID string) (string, bool) {
	for _, pat := range patterns {
		if tenantMatches([]string{pat}, tenantID) {
			return pat, true
		}
	}
	return "", false
}

// whoOf transcribes a rule's matcher verbatim (config prose, never evaluated).
func whoOf(rule *GroupRule) ReverseWho {
	if rule.Match == nil {
		return ReverseWho{Kind: WhoKindLegacyGroup, LegacyGroup: rule.Name}
	}
	return ReverseWho{
		Kind:        WhoKindMatchBlock,
		GroupsAnyOf: copyStrings(rule.Match.Groups),
		ClaimsAllOf: copyClaims(rule.Match.Claims),
	}
}

// orgGateFor reports a rule's org-scope axis for the queried tenant in both
// fail-modes. The unlabeled-vs-labeled distinction is DERIVED by probing the
// real scopeSetModes with an absent caller value: the (true, false) pair is
// that predicate's unique signature for the unlabeled-tenant leniency, so the
// classification can never drift from the forward semantics.
func orgGateFor(rule *GroupRule, tenantOrgs []string, includeValues bool) ReverseOrgGate {
	if rule.OrgScope == "" {
		return ReverseOrgGate{
			Required:       false,
			OutcomeShadow:  OrgOutcomeNotRequired,
			OutcomeEnforce: OrgOutcomeNotRequired,
		}
	}
	g := ReverseOrgGate{Required: true, ClaimKey: rule.OrgScope}
	if sh, en := scopeSetModes("", tenantOrgs); sh && !en {
		// Unlabeled tenant: shadow passes (migration leniency), enforce denies.
		g.OutcomeShadow, g.OutcomeEnforce = OrgOutcomePassUnlabeled, OrgOutcomeFailUnlabeled
		return g
	}
	// Labeled tenant: the outcome hinges on the caller's org claim value in
	// BOTH modes (a labeled non-match is denied even in shadow).
	g.OutcomeShadow, g.OutcomeEnforce = OrgOutcomeConditional, OrgOutcomeConditional

	// Passing value domain by ENUMERATION (round-1 C2): each candidate value
	// from the tenant's org list is run through the real predicates via a
	// witness subject. When the rule also pins match.claims[OrgScope], the
	// intersection emerges naturally from ruleMatches — no hand-written
	// intersection rule. Always computed for a labeled tenant (Unsatisfiable
	// lives in the default view); the VALUES are attached only on opt-in.
	passing := make([]string, 0, len(tenantOrgs))
	for _, v := range sortedUnique(tenantOrgs) {
		if orgWitnessPasses(rule, v, tenantOrgs) {
			passing = append(passing, v)
		}
	}
	g.Unsatisfiable = len(passing) == 0
	if includeValues && len(passing) > 0 {
		g.PassingOrgValues = passing
	}
	return g
}

// witnessPrincipal synthesizes the MINIMAL principal that satisfies rule's
// WHO shape by construction: legacy → the rule name as its one group; match
// block → the first listed group (OR-within: one suffices) plus the first
// allowed value of every claim key (AND-across: every key seeded). Because
// each condition is satisfied by its own first element, this witness passes
// ruleMatches iff ANY principal can — which is what makes it double as the
// statically-dead-WHO discriminator in ReverseAccessReport (a rule whose own
// witness fails, e.g. an empty match block or an empty claim value list,
// matches nobody). Shared with orgWitnessPasses so the org-value enumeration
// and the dead-rule guard can never drift apart.
func witnessPrincipal(rule *GroupRule) *VerifiedPrincipal {
	p := &VerifiedPrincipal{Claims: map[string]string{}}
	if rule.Match == nil {
		p.Groups = []string{rule.Name}
		return p
	}
	if len(rule.Match.Groups) > 0 {
		p.Groups = []string{rule.Match.Groups[0]}
	}
	for key, allowed := range rule.Match.Claims {
		if len(allowed) > 0 {
			p.Claims[key] = allowed[0]
		}
	}
	return p
}

// orgWitnessPasses reports whether org claim value v would carry a caller
// through rule's org gate for a tenant with tenantOrgs, by BORROWING the
// forward predicates: take the rule's minimal witness principal
// (witnessPrincipal), pin its org claim to v, then run the SAME ruleMatches +
// scopeSetModes the forward gates run. The strict (enforce) membership result
// is the criterion — a passing value must pass even under enforce.
func orgWitnessPasses(rule *GroupRule, v string, tenantOrgs []string) bool {
	p := witnessPrincipal(rule)
	// Pin the org claim to the candidate AFTER seeding, so a rule pinning
	// match.claims[OrgScope] tests v against its allowed list inside
	// ruleMatches — the intersection semantics come from the predicate itself.
	p.Claims[rule.OrgScope] = v
	if !subjectFor(p).ruleMatches(rule) {
		return false
	}
	_, enforce := scopeSetModes(v, tenantOrgs)
	return enforce
}

// surfacesOf maps effective permissions to the decision surfaces they act on:
// read → list + read_by_id, write → write, admin → admin. Fixed order.
func surfacesOf(eff EffectivePerms) []string {
	surf := make([]string, 0, 4)
	if eff.Read {
		surf = append(surf, SurfaceList, SurfaceReadByID)
	}
	if eff.Write {
		surf = append(surf, SurfaceWrite)
	}
	if eff.Admin {
		surf = append(surf, SurfaceAdmin)
	}
	return surf
}

// orgStatusOf maps tenantorg.OrgsForTenant's (orgs, known) result to the
// three-state enum (tenantorg.go documents the two "no orgs" states P6 must
// distinguish: never onboarded vs onboarded-but-unassigned).
func orgStatusOf(orgs []string, known bool) string {
	switch {
	case !known:
		return OrgStatusNotOnboarded
	case len(orgs) == 0:
		return OrgStatusUnlabeled
	default:
		return OrgStatusLabeled
	}
}

// patternKindOf classifies a grant's tenant pattern for the redacted view.
// A rule-level wildcard (PlatformWide) wins the downgrade regardless of which
// list member was recorded as the first hit — kind and platform_wide must
// agree. Already-redacted input (empty verbatim pattern) keeps its existing
// kind.
func patternKindOf(g ReverseGrant) string {
	if g.PlatformWide {
		return PatternKindWildcard // rule carries a literal "*" somewhere in its list
	}
	if g.TenantPattern == "" {
		return g.PatternKind // idempotent re-redaction
	}
	switch {
	case g.TenantPattern == "*":
		return PatternKindWildcard
	case len(g.TenantPattern) > 1 && g.TenantPattern[len(g.TenantPattern)-1] == '*':
		return PatternKindPrefix
	default:
		return PatternKindLiteral
	}
}

// redactWho rebuilds a WHO block keeping only its kind and identifier COUNTS.
// A legacy rule matches exactly one group (its name), so its groups_count is
// 1. Idempotent: counts already present (an already-redacted input) win over
// recomputation from the absent originals.
func redactWho(w ReverseWho) ReverseWho {
	gc, cc := 0, 0
	switch w.Kind {
	case WhoKindLegacyGroup:
		gc = 1
	case WhoKindMatchBlock:
		gc = len(w.GroupsAnyOf)
		cc = len(w.ClaimsAllOf)
	}
	if w.GroupsCount != nil {
		gc = *w.GroupsCount
	}
	if w.ClaimsCount != nil {
		cc = *w.ClaimsCount
	}
	return ReverseWho{Kind: w.Kind, GroupsCount: &gc, ClaimsCount: &cc}
}

// ── Small copy/sort utilities (fresh backing storage; deterministic output) ─

// copyStrings returns a fresh, never-nil copy of src (schema stability: an
// absent list renders as [], not null).
func copyStrings(src []string) []string {
	out := make([]string, len(src))
	copy(out, src)
	return out
}

// copyPermissions returns a fresh, never-nil copy of src.
func copyPermissions(src []Permission) []Permission {
	out := make([]Permission, len(src))
	copy(out, src)
	return out
}

// copyClaims deep-copies a match block's claims map (values get fresh backing
// arrays; the report must not alias the live config snapshot).
func copyClaims(src map[string][]string) map[string][]string {
	if src == nil {
		return nil
	}
	out := make(map[string][]string, len(src))
	for k, v := range src {
		out[k] = copyStrings(v)
	}
	return out
}

// sortedUnique returns a fresh sorted copy of src with duplicates removed.
func sortedUnique(src []string) []string {
	out := copyStrings(src)
	sort.Strings(out)
	n := 0
	for i, v := range out {
		if i == 0 || v != out[n-1] {
			out[n] = v
			n++
		}
	}
	return out[:n]
}
