// Package rbac implements RBAC loading and permission checking for tenant-api.
//
// Design:
//   - _rbac.yaml is loaded on startup and hot-reloaded on change (SHA-256 detection).
//   - The parsed config is stored in an atomic.Value for lock-free reads.
//   - Group → Tenant mapping supports wildcards ("*") and prefix patterns ("db-a-*").
//   - Permissions: "read" | "write" | "admin".
//
// _rbac.yaml format:
//
//	groups:
//	  - name: platform-admins          # no match: → the name IS the matched IdP group (legacy shape)
//	    tenants: ["*"]
//	    permissions: [read, write, admin]
//	  - name: db-operators
//	    tenants: ["db-a-*", "db-b-*"]
//	    permissions: [read, write]
//	  - name: org-4821-operators       # match: present → name is a pure label/audit id
//	    match:
//	      groups: [operators]          # OR-within the list
//	      claims:
//	        org: [ORG-4821]            # claim key → allowed values (OR-within)
//	    tenants: ["*"]
//	    permissions: [read, write]
//
// Parsing is STRICT (yaml KnownFields): an unknown field is a load error,
// never silently ignored (see parseConfig).
package rbac

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"strings"

	"github.com/vencil/tenant-api/internal/configwatcher"
	"gopkg.in/yaml.v3"
)

// Permission represents a single permission level.
type Permission string

const (
	PermRead  Permission = "read"
	PermWrite Permission = "write"
	PermAdmin Permission = "admin"
)

// GroupRule maps an IdP group to a set of tenants and permissions.
//
// v2.5.0: Added Environments and Domains for metadata-based filtering.
// These fields are optional — omitting them is equivalent to wildcard (all).
//
// ADR-027 / LD-6 P3: Added the optional Match block. Without it (the legacy
// shape) a rule applies iff Name equals one of the caller's IdP groups —
// byte-identical to the pre-P3 behavior, evaluated on the SAME code path
// (ruleMatches degenerates to the group-name test, not a separate branch).
// With Match present, Name becomes a pure label / audit identifier and the
// rule applies iff the Match conditions hold (see MatchBlock).
type GroupRule struct {
	Name         string       `yaml:"name"`
	Match        *MatchBlock  `yaml:"match,omitempty"`        // optional claims-aware matcher; nil = legacy name matching
	Tenants      []string     `yaml:"tenants"`                // tenant IDs or patterns ("*", "db-a-*")
	Permissions  []Permission `yaml:"permissions"`            // [read, write, admin]
	Environments []string     `yaml:"environments,omitempty"` // ["production", "staging"] — empty = all
	Domains      []string     `yaml:"domains,omitempty"`      // ["finance", "ecommerce"] — empty = all

	// OrgScope opts this rule into the org-scope axis (ADR-027 / LD-6 P4).
	// Empty (the default) = the rule places no org restriction and behaves
	// byte-identically to pre-P4. When set, its value is the claim KEY (e.g.
	// "org-code") whose caller value must be one of the target tenant's orgs
	// (from _tenant_orgs.yaml, keyed by tenant ID) for the rule to grant that
	// tenant. The key MUST be declared in --identity-claim-headers
	// (validateConfig enforces this at load — an org-scope on an undeclared
	// claim could never match and must fail loud, not silently deny).
	OrgScope string `yaml:"org-scope,omitempty"`
}

// MatchBlock is the claims-aware rule matcher (ADR-027 / LD-6 P3).
//
// Semantics: AND across condition kinds, OR within a condition's list —
//   - Groups (if non-empty): at least ONE entry must be among the caller's
//     IdP groups.
//   - Claims: EVERY listed claim key must be present on the caller's
//     principal AND its value must be one of the allowed values (exact
//     string equality on the trimmed value the trusted hop carried — no
//     wildcard/prefix patterns; the tenants-list pattern syntax deliberately
//     does not leak into claims).
//
// Fail-closed guarantees (enforced by validateConfig at load + defensively
// at evaluation): an EMPTY match block is a config error, NOT match-all; a
// claim key not declared in --identity-claim-headers is a load error; a
// principal missing a required claim simply does not match.
//
// Namespace honesty (single-trusted-hop MVP): the claim KEY is the
// namespace unit. A deployment must not map two different upstream sources
// onto the same claim key — nothing here can tell them apart. A true
// issuer namespace arrives with JWT verification (iss), deferred to D2-A.
type MatchBlock struct {
	Groups []string            `yaml:"groups,omitempty"` // OR-within: any one group qualifies
	Claims map[string][]string `yaml:"claims,omitempty"` // claim key → allowed values (OR-within); keys AND-across
}

// RBACConfig is the parsed _rbac.yaml structure.
type RBACConfig struct {
	Groups []GroupRule `yaml:"groups"`
}

// Manager holds the hot-reloadable RBAC config. The hot-reload
// machinery (atomic.Value + SHA-256 dedup + WatchLoop) lives in the
// embedded configwatcher.Watcher; this type only adds the
// permission-check methods.
//
// Open-read mode: when the configured path is empty (no _rbac.yaml
// supplied), the underlying Watcher stores an empty RBACConfig{}.
// Allowed's `len(cfg.Groups) == 0` check then degrades to
// "authenticated users have read access only" — matches the
// pre-PR-8 behavior.
type Manager struct {
	*configwatcher.Watcher[RBACConfig]

	// failClosedOnEmpty (ADR-027 MED-8): when true, an empty group set
	// (a mistyped or empty _rbac.yaml that parses to zero groups) DENIES
	// all access instead of degrading to open-read. Set when a --rbac
	// PATH was configured — a configured-but-empty policy is a
	// misconfiguration and must fail closed, not silently grant read to
	// every authenticated identity. A bare run with no --rbac path stays
	// open-read (intentional no-RBAC, e.g. local/demo), and an operator
	// can restore the legacy behavior with --rbac-empty-open.
	failClosedOnEmpty bool

	// machineAuditor (ADR-027 PR-1b-i): optional machine-identity audit
	// side-channel. When non-nil, Middleware calls Observe on every request
	// AFTER resolving the header principal and independently of the authz
	// decision — audit only (verify + log + metric); it never changes authz or
	// fails the request (a synchronous review may add bounded latency). nil
	// (the default) means the feature is disabled and Middleware behaves
	// byte-identically to the pre-seam version. Set once at startup via
	// SetMachineAuditor.
	machineAuditor MachineIdentityAuditor

	// metadataScopeEnforce (ADR-027 / LD-6 P1) controls the fail-mode of the
	// metadata (environment/domain) scope filter for an UNLABELED tenant — one
	// that carries no value for a field a matching rule restricts. false (the
	// default) is SHADOW mode: the unlabeled tenant still passes (byte-identical
	// to the legacy fail-OPEN behavior) but a would-deny signal is recorded so
	// operators can backfill labels before flipping. true is ENFORCE mode: the
	// unlabeled tenant is denied (fail-CLOSED). Set once at startup via
	// EnableMetadataScopeEnforce. Per-axis by design (ADR-027 D4): the org scope
	// axis (P4) carries its own flag so the two audit→enforce rollouts stay
	// independent.
	metadataScopeEnforce bool

	// orgScopeEnforce (ADR-027 / LD-6 P4) is the org-scope axis's own fail-mode
	// flag, mirroring metadataScopeEnforce but for the org (tenant→organization)
	// axis. false (the default) is SHADOW: an unlabeled tenant (one with no orgs
	// in _tenant_orgs.yaml) that an org-scoped rule would otherwise hide still
	// passes, but a would-deny is recorded so operators can backfill org labels
	// before flipping. true is ENFORCE (fail-closed). Per-axis by design so the
	// metadata and org audit→enforce rollouts stay independent (ADR-027 D4).
	//
	// P4b NOTE: this ONE flag governs BOTH decision surfaces of the org axis —
	// list visibility (ScopeAllowed) and per-tenant write authorization
	// (AllowedInOrg) — so they flip to enforce ATOMICALLY under one control
	// (--rbac-org-scope-enforce / TA_RBAC_ORG_SCOPE_ENFORCE / helm
	// rbac.orgScopeEnforce, wired in main.go via EnableOrgScopeEnforce).
	// Flipping either surface alone would be a false-safe isolation.
	orgScopeEnforce bool

	// scopeAudit is the optional would-deny metric sink for scope filters
	// (instance-method DI, mirroring machineAuditor / the rate-limiter bridge,
	// so metric state is not a package singleton and tests stay isolatable).
	// nil (the default) means no recording — the filter still behaves correctly,
	// it just emits no would-deny counter. Set once at startup via
	// SetScopeAuditor. Shared across scope axes (P1 metadata; P4 org).
	scopeAudit ScopeAuditRecorder

	// claimHeaders (ADR-027 / LD-6 P2) declares which trusted-hop header
	// loads which named claim (claimKey → headerName), parsed from
	// --identity-claim-headers by ParseClaimHeaders. Middleware hands it to
	// HeaderResolver so the resolved principal carries the named claims.
	// nil means no claim axes are declared — the principal's Claims stays
	// nil and behavior is byte-identical to pre-P2. Installed by NewManager
	// (P3): the same map feeds config validation (a match.claims key must
	// be a declared claim key), so the declaration and the enforcement can
	// never drift — which is why this is a constructor argument and no
	// longer a post-construction setter.
	claimHeaders map[string]string
}

// SetMachineAuditor installs the machine-identity audit side-channel
// (ADR-027 PR-1b-i). Called once at startup from main after wiring the
// TokenReview-backed KSAResolver. Passing nil leaves auditing disabled. This
// is a prod setter (mirrors the test-only setter style) rather than a
// constructor arg so NewManager's signature — and its many call sites — stay
// unchanged.
func (m *Manager) SetMachineAuditor(a MachineIdentityAuditor) { m.machineAuditor = a }

// EnableMetadataScopeEnforce switches the metadata (environment/domain) scope
// filter from SHADOW (default) to ENFORCE mode: an unlabeled tenant on a
// restricted field is DENIED instead of allowed-with-would-deny-signal
// (ADR-027 / LD-6 P1). Called from main when --rbac-metadata-scope-enforce is
// set — after a shadow soak has driven the would-deny counter to zero. Kept a
// setter (not a NewManager arg) so the many NewManager call sites stay
// unchanged, mirroring AllowOpenReadOnEmpty / SetMachineAuditor.
func (m *Manager) EnableMetadataScopeEnforce() { m.metadataScopeEnforce = true }

// EnableOrgScopeEnforce switches the org-scope axis from SHADOW (default) to
// ENFORCE: an unlabeled tenant on an org-scoped rule is DENIED instead of
// allowed-with-would-deny-signal (ADR-027 / LD-6 P4). Called from main when
// --rbac-org-scope-enforce is set (P4b) — the single control that flips list
// visibility (ScopeAllowed) and write authorization (AllowedInOrg) to enforce
// atomically; flip only after the axis="org" AND axis="org_write" would-deny
// counters both hold increase()==0 over the soak window. Kept a setter (not a
// NewManager arg) to mirror EnableMetadataScopeEnforce and to drive the
// enforce branch from tests.
func (m *Manager) EnableOrgScopeEnforce() { m.orgScopeEnforce = true }

// SetScopeAuditor installs the would-deny metric sink for scope filters
// (ADR-027 / LD-6 P1). Called once at startup. Passing nil leaves recording
// disabled (the filter still behaves correctly). Mirrors SetMachineAuditor.
func (m *Manager) SetScopeAuditor(a ScopeAuditRecorder) { m.scopeAudit = a }

// NewManager creates a Manager and loads the RBAC config from path.
// If path is empty, the manager starts in open mode (all
// authenticated users have read access, no write).
//
// claimHeaders is the claimKey→headerName declaration parsed from
// --identity-claim-headers (ParseClaimHeaders); nil means no claim axes.
// It is a constructor argument — not a setter — because the declared claim
// keys participate in config validation: the parse closure below captures
// them, so BOTH the initial load and every hot-reload reject a config whose
// match.claims references an undeclared key (ADR-027 / LD-6 P3 fail-loud).
//
// Unlike the other config managers (groups / views / policy), an
// initial-load failure here is FATAL for the caller — the rbac
// gate is the only enforcement layer between identity headers and
// tenant data, so a config that cannot be parsed is not safe to
// serve. main.go calls log.Fatalf on this error. A hot-reload
// failure keeps serving the last-good snapshot (configwatcher logs
// a WARN and load() does not store on a parse/validate error).
func NewManager(path string, claimHeaders map[string]string) (*Manager, error) {
	parse := func(data []byte) (*RBACConfig, error) {
		return parseConfig(data, claimHeaders)
	}
	w, err := configwatcher.New(path, "RBAC", parse, emptyConfig)
	if err != nil {
		return nil, fmt.Errorf("rbac: initial load failed: %w", err)
	}
	// MED-8: a configured --rbac path that parses to zero groups is a
	// misconfiguration → fail closed. Path-less (open) mode keeps read.
	return &Manager{Watcher: w, failClosedOnEmpty: path != "", claimHeaders: claimHeaders}, nil
}

// AllowOpenReadOnEmpty restores the legacy open-read-on-empty behavior
// even when a --rbac path is configured (the --rbac-empty-open escape
// hatch). MED-8 fail-closed is the secure default; this exists only for
// backward compatibility / rollback.
func (m *Manager) AllowOpenReadOnEmpty() { m.failClosedOnEmpty = false }

// FailClosedOnEmpty reports whether this manager denies all access when the
// configured policy resolves to zero groups (ADR-027 MED-8) — vs. path-less
// open-read mode. Callers use it to distinguish the two zero-group states
// (e.g. for accurate startup warnings).
func (m *Manager) FailClosedOnEmpty() bool { return m.failClosedOnEmpty }

// NewForTest returns a Manager pre-populated with cfg and no file
// path. WatchLoop and Reload become no-ops; only the embedded
// permission-check methods are exercised. Intended for unit tests
// that drive permission logic against an in-memory snapshot.
func NewForTest(cfg *RBACConfig) *Manager {
	return &Manager{Watcher: configwatcher.NewForTest("rbac", cfg)}
}

func emptyConfig() *RBACConfig { return &RBACConfig{} }

// parseConfig parses _rbac.yaml STRICTLY (yaml.Decoder.KnownFields): an
// unknown field — a `mach:` typo for `match:`, a misspelled rule key, an
// unrecognized top-level key — is a load error, never a silently-ignored
// key. Silently dropping a mistyped match block would degrade the rule to
// plain group-name matching, i.e. WIDER access than the author intended — a
// privilege-escalation surface, not a cosmetic bug. Breaking-for-invalid-
// configs by design; a valid legacy config parses unchanged.
//
// An empty or comment-only file decodes to the empty config (the strict
// decoder surfaces io.EOF where the previous lenient yaml.Unmarshal returned
// a zero struct; MED-8 fail-closed-on-empty still governs what that means).
//
// declaredClaimKeys is the --identity-claim-headers declaration captured by
// the NewManager parse closure; validateConfig rejects a config whose
// match.claims references a key outside it.
func parseConfig(data []byte, declaredClaimKeys map[string]string) (*RBACConfig, error) {
	var cfg RBACConfig
	dec := yaml.NewDecoder(bytes.NewReader(data))
	dec.KnownFields(true)
	if err := dec.Decode(&cfg); err != nil {
		if errors.Is(err, io.EOF) {
			return &RBACConfig{}, nil
		}
		return nil, err
	}
	// A present-but-null `match:` decodes to a nil *MatchBlock — structurally
	// indistinguishable from an absent match — so validateConfig (which keys
	// off the nil pointer) cannot catch it. Detect it at the YAML-node level
	// BEFORE validateConfig, or a bare `match:` would silently revert the rule
	// to legacy group-name matching and drop its intended claim scoping.
	if err := detectNullMatchBlocks(data); err != nil {
		return nil, err
	}
	if err := validateConfig(&cfg, declaredClaimKeys); err != nil {
		return nil, err
	}
	return &cfg, nil
}

// detectNullMatchBlocks rejects a rule that carries a present-but-null `match:`
// key — a bare `match:` with no value, `match: null`, or a match block whose
// only children are commented out. All three decode the field to a nil
// *MatchBlock, which is INDISTINGUISHABLE at the struct level from a rule that
// has no match key at all (legacy group-name matching). validateConfig keys off
// that nil pointer, so it cannot tell them apart; ruleMatches would then
// silently revert the rule to `groupSet[rule.Name]`, DROPPING the claim scoping
// the author was mid-writing — a privilege-escalation surface in the one
// enforcement layer (an `operators`-group member gets the grant with no claim
// required). This is the exact failure `match: {}` is rejected for; the null
// form must fail loud too.
//
// The check runs on the raw YAML node tree (a second, lenient decode of the
// same bytes) because presence-vs-null is only visible before unmarshalling
// into *MatchBlock. Discriminators, verified empirically against yaml.v3:
// an ABSENT match field leaves the node zero (IsZero); a present null value —
// bare, explicit, or comment-only — yields a ScalarNode tagged !!null; a
// `match: {}` or populated block yields a MappingNode (caught, if empty, by
// validateConfig). A non-null scalar like `match: foo` is already rejected by
// the strict struct decode (cannot unmarshal !!str into MatchBlock), so null
// is the only present-scalar form that reaches here.
func detectNullMatchBlocks(data []byte) error {
	type rawRule struct {
		Name  string    `yaml:"name"`
		Match yaml.Node `yaml:"match"`
	}
	type rawCfg struct {
		Groups []rawRule `yaml:"groups"`
	}
	var rc rawCfg
	if err := yaml.Unmarshal(data, &rc); err != nil {
		// Any real syntax error was already surfaced by the strict decode in
		// parseConfig; this lenient pass only inspects match-node presence.
		return nil
	}
	for i := range rc.Groups {
		n := rc.Groups[i].Match
		if n.Kind == yaml.ScalarNode && n.Tag == "!!null" {
			return fmt.Errorf("rbac: rule %q: `match:` is present but null (a bare `match:`, `match: null`, or a match block with only commented-out conditions) — this would silently drop the rule to legacy group-name matching and lose its claim scoping; write the groups:/claims: conditions, or remove the `match:` key entirely for legacy name matching", rc.Groups[i].Name)
		}
	}
	return nil
}

// validateConfig enforces the fail-closed config guarantees of the match:
// block (ADR-027 / LD-6 P3). It runs inside the parse path, so BOTH the
// initial load and every hot-reload pass through it: an invalid config is
// rejected at load time (initial load → NewManager error → main fatal;
// hot-reload → configwatcher keeps the last-good snapshot and logs a WARN).
//
//   - An empty match block (no groups AND no claims) is an error: empty
//     match is NOT match-all.
//   - A match.claims key not present in declaredClaimKeys is an error: a
//     rule on an undeclared claim key could never match at runtime, and a
//     silently-dead authorization rule must fail loud instead (same
//     philosophy as the ParseClaimHeaders charset guards).
//   - Empty entries — a blank match.groups name, an empty match.claims
//     value list, a blank claim value — are errors: they could only arise
//     from an authoring mistake and would otherwise be silently unmatchable.
func validateConfig(cfg *RBACConfig, declaredClaimKeys map[string]string) error {
	for i := range cfg.Groups {
		rule := &cfg.Groups[i]
		// Org-scope opt-in (ADR-027 / LD-6 P4): the claim key an org-scoped rule
		// keys off MUST be declared in --identity-claim-headers, exactly like a
		// match.claims key. Checked for EVERY rule — independently of the match
		// block — because a rule can be legacy name-matched AND org-scoped. An
		// org-scope on an undeclared claim could never carry a value at runtime,
		// so it would silently deny (shadow) / hide (enforce) every tenant; that
		// dead authorization rule must fail loud at load, not deny in silence.
		if rule.OrgScope != "" {
			if _, declared := declaredClaimKeys[rule.OrgScope]; !declared {
				return fmt.Errorf("rbac: rule %q: org-scope key %q is not declared in --identity-claim-headers (an undeclared claim key can never match at runtime; declare the axis or remove org-scope)", rule.Name, rule.OrgScope)
			}
		}
		if rule.Match == nil {
			continue
		}
		if len(rule.Match.Groups) == 0 && len(rule.Match.Claims) == 0 {
			return fmt.Errorf("rbac: rule %q: empty match block (an empty match is a config error, NOT match-all — remove the block for legacy name matching, or add groups:/claims: conditions)", rule.Name)
		}
		for _, g := range rule.Match.Groups {
			if strings.TrimSpace(g) == "" {
				return fmt.Errorf("rbac: rule %q: match.groups contains an empty entry", rule.Name)
			}
		}
		for key, values := range rule.Match.Claims {
			if _, declared := declaredClaimKeys[key]; !declared {
				return fmt.Errorf("rbac: rule %q: match.claims key %q is not declared in --identity-claim-headers (an undeclared claim key can never match at runtime; declare the axis or remove the condition)", rule.Name, key)
			}
			if len(values) == 0 {
				return fmt.Errorf("rbac: rule %q: match.claims[%q] has an empty value list", rule.Name, key)
			}
			for _, v := range values {
				if strings.TrimSpace(v) == "" {
					return fmt.Errorf("rbac: rule %q: match.claims[%q] contains an empty value", rule.Name, key)
				}
			}
		}
	}
	return nil
}

// ── Principal-based evaluation core (ADR-027 / LD-6 P3) ──────────────────
//
// Allowed / MetadataAllowed / AccessibleEnvironmentsFor / AccessibleDomainsFor
// are the ONLY production authorization entry points. They take the request's
// *VerifiedPrincipal so the shared rule-matching predicate (ruleMatches) can
// see everything the trusted hop attested — groups today, named claims once
// the optional match: block lands. The legacy groups-slice signatures
// (HasPermission / HasMetadataAccess / AccessibleEnvironments /
// AccessibleDomains) live on as test-only one-line delegates in
// export_test.go, so a production caller of the old shape is a COMPILE error.
//
// nil-principal contract: p == nil is a documented ANONYMOUS caller — no
// groups, no claims (e.g. a request that never passed through Middleware).
// It evaluates exactly as the empty groups slice always has: open mode still
// grants read (and MetadataAllowed passes), configured/fail-closed modes
// deny, and no rule can ever match.

// matchSubject is the precomputed view of the caller that ruleMatches
// evaluates each GroupRule against. It is built once per evaluation call
// (subjectFor) and shared by the four evaluation methods.
type matchSubject struct {
	groupSet map[string]bool
	claims   map[string]string
}

// subjectFor precomputes the matchSubject for principal p. A nil principal
// (anonymous caller) yields an empty group set and nil claims, so no rule
// can match — identical to how a nil groups slice has always evaluated.
func subjectFor(p *VerifiedPrincipal) matchSubject {
	var groups []string
	var claims map[string]string
	if p != nil {
		groups = p.Groups
		claims = p.Claims
	}
	set := make(map[string]bool, len(groups))
	for _, g := range groups {
		set[g] = true
	}
	return matchSubject{groupSet: set, claims: claims}
}

// ruleMatches is THE single rule-matching predicate shared by Allowed,
// MetadataAllowed, AccessibleEnvironmentsFor, AccessibleDomainsFor and
// RulesMatching (/me). Rule-matching semantics must never be implemented
// anywhere else.
//
// Without a match: block a rule applies iff its Name is one of the caller's
// IdP groups — byte-identical to the groupSet[rule.Name] test the evaluation
// methods previously inlined (the legacy model is the degenerate case of the
// same path, not a separate branch).
//
// With a match: block (ADR-027 / LD-6 P3) the rule's Name is a pure label;
// conditions AND across kinds, OR within a list:
//   - match.groups non-empty → at least one entry must be in the caller's
//     group set;
//   - every match.claims key → the principal must CARRY that claim and its
//     value must equal (exact string comparison) one of the allowed values.
//     A missing claim, a nil claims map (anonymous / machine principals) or
//     a value outside the list fails the rule — fail-closed.
//
// An empty match block never matches. validateConfig already rejects it at
// load; this evaluation-side check is defense-in-depth for snapshots
// injected around the loader (Override / NewForTest), because the only
// wrong default for "empty match" in an enforcement layer is match-all.
func (s matchSubject) ruleMatches(rule *GroupRule) bool {
	m := rule.Match
	if m == nil {
		return s.groupSet[rule.Name]
	}
	if len(m.Groups) == 0 && len(m.Claims) == 0 {
		return false // empty match ≠ match-all (defense-in-depth; see above)
	}
	if len(m.Groups) > 0 {
		anyGroup := false
		for _, g := range m.Groups {
			if s.groupSet[g] {
				anyGroup = true
				break
			}
		}
		if !anyGroup {
			return false
		}
	}
	for key, allowed := range m.Claims {
		got, present := s.claims[key]
		if !present {
			return false // missing claim → fail-closed
		}
		anyValue := false
		for _, v := range allowed {
			if v == got {
				anyValue = true
				break
			}
		}
		if !anyValue {
			return false
		}
	}
	return true
}

// ruleGrants reports whether a single rule's permission list covers the wanted
// permission (admin ⊇ write ⊇ read). Extracted from Allowed's inner loop
// (ADR-027 / LD-6 P4b) so every per-rule evaluation path shares the ONE
// permission predicate — permission-coverage semantics must never be
// re-implemented at a call site.
func ruleGrants(rule *GroupRule, want Permission) bool {
	for _, perm := range rule.Permissions {
		if permCovers(perm, want) {
			return true
		}
	}
	return false
}

// allowedOrgModes is the single per-tenant permission evaluation core
// (ADR-027 / LD-6 P4b): it folds the org-scope axis into the SAME per-rule
// loop as the permission check and evaluates BOTH org fail-modes in one pass,
// returning (passShadow, passEnforce).
//
// ⚠️ Correctness core — a rule grants iff ruleMatches && tenantMatches &&
// ruleGrants && its OWN org restriction passes (per-rule fold). Unioning "any
// rule grants the permission" and "any rule passes org" separately and AND'ing
// the two unions at the top level would leak access no single rule grants —
// the exact cross-rule hazard ScopeAllowed documents; see
// TestAllowedInOrg_CrossRuleUnionNoLeak / TestScopeAllowed_CrossRuleUnionNoLeak.
//
// orgBlockedRule is the name of the first matching rule whose grant the org
// axis curtailed (its org modes were not (true,true)) — the diagnostic handle
// for AllowedInOrg's deny/would-deny log line. Rule NAMES only; claim values
// never leave the evaluation (principal.go logging discipline). Empty when no
// matching rule was org-restricted.
//
// Pure with respect to observability: no metric recording, no enforce-flag
// read — the decision-site wrappers (Allowed / AllowedInOrg) own both, so the
// read-plane wrapper can never pollute the org_write would-deny signal.
//
// Empty-config semantics are byte-identical to the historical Allowed:
// failClosedOnEmpty denies both modes; open mode grants read-only in both.
func (m *Manager) allowedOrgModes(p *VerifiedPrincipal, tenantID string, want Permission, tenantOrgs []string) (passShadow, passEnforce bool, orgBlockedRule string) {
	cfg := m.Get()
	if len(cfg.Groups) == 0 {
		if m.failClosedOnEmpty {
			return false, false, "" // MED-8: configured but empty _rbac.yaml → deny
		}
		// Open mode — authenticated users have read access only
		return want == PermRead, want == PermRead, ""
	}

	subject := subjectFor(p)
	for i := range cfg.Groups {
		rule := &cfg.Groups[i]
		if !subject.ruleMatches(rule) {
			continue
		}
		if !tenantMatches(rule.Tenants, tenantID) {
			continue
		}
		if !ruleGrants(rule, want) {
			continue
		}
		orgShadow, orgEnforce := true, true // no org-scope on this rule = no org restriction
		if rule.OrgScope != "" {
			orgShadow, orgEnforce = scopeSetModes(subject.claims[rule.OrgScope], tenantOrgs)
		}
		if orgBlockedRule == "" && (!orgShadow || !orgEnforce) {
			orgBlockedRule = rule.Name
		}
		passShadow = passShadow || orgShadow
		passEnforce = passEnforce || orgEnforce
		if passShadow && passEnforce {
			break // both modes granted; further rules cannot change either
		}
	}
	return passShadow, passEnforce, orgBlockedRule
}

// Allowed checks whether the caller p is granted the wanted permission for
// the given tenantID by any rule matching the principal.
//
// Permission hierarchy: admin ⊇ write ⊇ read.
// An "admin" grant satisfies "write" and "read" checks.
//
// ⚠️ ORG-BLIND (ADR-027 / LD-6 P4b): Allowed is the org-scope-degenerate
// wrapper over allowedOrgModes — with tenantOrgs=nil every org-scoped rule
// passes shadow mode (scopeSetModes' unlabeled-tenant leniency is (true,false)),
// so the shadow component returned here is byte-identical to the pre-P4b
// permission check and the org axis is invisible. Any per-tenant WRITE-plane
// authorization decision (PermWrite / PermAdmin gates) MUST go through
// AllowedInOrg instead — via handler.OrgAllowed, which resolves the tenant's
// orgs — never this method. The only legitimate Allowed callers are (1) the
// platform-scope tenantID="*" checks (org-scope deliberately does not apply to
// platform scope — an org-scoped rule is not a platform admin) and (2)
// read-plane filtering (org visibility is ScopeAllowed's job; read-by-id lands
// in P4c) — pinned by the go/ast tripwire, see org_write_guard_test.go.
//
// Allowed never records the org_write would-deny signal — recording lives in
// AllowedInOrg only, so middleware/read call volume cannot pollute the
// enforce-flip soak metric.
func (m *Manager) Allowed(p *VerifiedPrincipal, tenantID string, want Permission) bool {
	passShadow, _, _ := m.allowedOrgModes(p, tenantID, want, nil)
	return passShadow
}

// AllowedInOrg is the org-scope-aware per-tenant permission check and the ONLY
// write-plane authorization entry point (ADR-027 / LD-6 P4b). tenantOrgs is
// the target tenant's organization list (tenantorg.OrgsForTenant), resolved by
// the caller AT DECISION TIME — rbac does not import tenantorg, mirroring
// ScopeAllowed; nil/empty means the tenant is unlabeled.
//
// Mode selection is governed by the SAME m.orgScopeEnforce flag as
// ScopeAllowed (list visibility and write enforcement flip atomically — one
// control, no split-brain): SHADOW (default) returns the lenient decision and
// records a would-deny observation on the org_write axis whenever enforce
// would have denied; ENFORCE returns the strict decision (the same observation
// keeps counting as a "denied by org scope" signal). Monotonicity holds by
// construction: AllowedInOrg(enforce) ⟹ AllowedInOrg(shadow) ⟹ Allowed —
// the org axis only ever narrows a grant, never widens one.
func (m *Manager) AllowedInOrg(p *VerifiedPrincipal, tenantID string, want Permission, tenantOrgs []string) bool {
	passShadow, passEnforce, orgBlockedRule := m.allowedOrgModes(p, tenantID, want, tenantOrgs)
	m.recordScopeShadowGap(passShadow, passEnforce, scopeAxisOrgWrite)
	if m.orgScopeEnforce {
		if !passEnforce && orgBlockedRule != "" {
			// The org axis curtailed an otherwise-granting rule → loud enough to
			// debug a 403. Tenant + rule NAME only; claim values never reach logs
			// (principal.go multi-value-refusal discipline).
			slog.Warn("org-scope denied write-plane permission",
				"tenant", tenantID, "axis", scopeAxisOrgWrite, "rule", orgBlockedRule, "perm", string(want))
		}
		return passEnforce
	}
	if passShadow && !passEnforce {
		// Shadow-mode migration gap: enforce would deny this grant. One line per
		// observation (matches the org_write would-deny increment) so operators
		// can attribute the soak counter to a tenant/rule; no claim values.
		slog.Info("org-scope write-plane would-deny (shadow mode)",
			"tenant", tenantID, "axis", scopeAxisOrgWrite, "rule", orgBlockedRule, "perm", string(want))
	}
	return passShadow
}

// MetadataAllowed checks whether the caller p is granted access for a tenant
// with the given environment and domain metadata, WITHOUT the org axis. It is
// the org-less thin wrapper over ScopeAllowed (tenantOrgs=nil): with no orgs
// and no org-scoped rule the org axis degenerates to (true,true) and the result
// is byte-identical to the pre-P4 metadata-only filter. Retained so the
// existing metadata test matrix and the /me-adjacent callers stay unchanged.
func (m *Manager) MetadataAllowed(p *VerifiedPrincipal, tenantID, environment, domain string) bool {
	return m.ScopeAllowed(p, tenantID, environment, domain, nil)
}

// ScopeAllowed is the single scope-filter decision for the tenant list: it
// evaluates the metadata (environment/domain) axis and the org axis TOGETHER,
// per matching rule, and returns whether the tenant is visible to caller p
// (ADR-027 / LD-6 P1 metadata + P4 org).
//
// ⚠️ Correctness core — the two axes are folded into the SAME per-rule loop, not
// AND'd at the top level. Metadata and org each grant a tenant only via a rule
// that passes BOTH of its own restrictions; unioning per-axis "does any rule
// pass this axis" separately and AND'ing the two unions would leak access no
// single rule grants (rule A passes metadata but fails org, rule B passes org
// but fails metadata → the top-level AND would show the tenant; the per-rule
// fold correctly hides it). See TestScopeAllowed_CrossRuleUnionNoLeak.
//
// Four aggregate visibility booleans are accumulated, one per (metadataMode,
// orgMode) combination, so the effective decision AND the per-axis would-deny
// signals can all be read from the single pass without re-evaluating:
//
//	visSS = shadow-metadata  & shadow-org
//	visSE = shadow-metadata  & enforce-org
//	visES = enforce-metadata & shadow-org
//	visEE = enforce-metadata & enforce-org
//
// Degeneration (pinned by tests): when no matching rule is org-scoped, every
// rule's org modes are (true,true), so visSE==visSS and visES==visEE; the
// effective result and the metadata would-deny then equal the pre-P4
// MetadataAllowed exactly, and the org would-deny is identically false.
//
// tenantOrgs is the target tenant's organization list (tenantorg.OrgsForTenant);
// nil/empty means the tenant is unlabeled. rbac does not import tenantorg — the
// handler resolves the orgs and passes them in.
func (m *Manager) ScopeAllowed(p *VerifiedPrincipal, tenantID, environment, domain string, tenantOrgs []string) bool {
	cfg := m.Get()
	if len(cfg.Groups) == 0 {
		if m.failClosedOnEmpty {
			return false // MED-8: configured but empty _rbac.yaml → deny
		}
		return true // open mode — no scope restrictions
	}

	subject := subjectFor(p)

	var visSS, visSE, visES, visEE bool
	for i := range cfg.Groups {
		rule := &cfg.Groups[i]
		if !subject.ruleMatches(rule) {
			continue
		}
		if !tenantMatches(rule.Tenants, tenantID) {
			continue
		}
		envShadow, envEnforce := scopeFieldModes(rule.Environments, environment)
		domShadow, domEnforce := scopeFieldModes(rule.Domains, domain)
		metaShadow := envShadow && domShadow
		metaEnforce := envEnforce && domEnforce

		orgShadow, orgEnforce := true, true // no org-scope on this rule = no org restriction
		if rule.OrgScope != "" {
			orgShadow, orgEnforce = scopeSetModes(subject.claims[rule.OrgScope], tenantOrgs)
		}

		visSS = visSS || (metaShadow && orgShadow)
		visSE = visSE || (metaShadow && orgEnforce)
		visES = visES || (metaEnforce && orgShadow)
		visEE = visEE || (metaEnforce && orgEnforce)
		if visSS && visSE && visES && visEE {
			break // all four outcomes decided; further rules cannot change any
		}
	}

	metaFlag := m.metadataScopeEnforce
	orgFlag := m.orgScopeEnforce

	// Per-axis would-deny: hold the OTHER axis at its current effective flag and
	// compare that axis's shadow vs enforce visibility. A tenant is a would-deny
	// for an axis iff flipping that axis alone from shadow→enforce hides it.
	m.recordScopeShadowGap(
		visAt(visSS, visSE, visES, visEE, false, orgFlag), // metadata=shadow
		visAt(visSS, visSE, visES, visEE, true, orgFlag),  // metadata=enforce
		scopeAxisMetadata)
	m.recordScopeShadowGap(
		visAt(visSS, visSE, visES, visEE, metaFlag, false), // org=shadow
		visAt(visSS, visSE, visES, visEE, metaFlag, true),  // org=enforce
		scopeAxisOrg)

	return visAt(visSS, visSE, visES, visEE, metaFlag, orgFlag)
}

// visAt selects one of the four aggregate visibility booleans by the effective
// per-axis modes (false=shadow, true=enforce). The index order matches the
// visSS/visSE/visES/visEE naming: first bit = metadata mode, second = org mode.
func visAt(visSS, visSE, visES, visEE, metaEnforce, orgEnforce bool) bool {
	switch {
	case !metaEnforce && !orgEnforce:
		return visSS
	case !metaEnforce && orgEnforce:
		return visSE
	case metaEnforce && !orgEnforce:
		return visES
	default:
		return visEE
	}
}

// AccessibleEnvironmentsFor returns the set of environments the caller p
// can access (empty set means "all" — no restriction).
func (m *Manager) AccessibleEnvironmentsFor(p *VerifiedPrincipal) []string {
	cfg := m.Get()
	if len(cfg.Groups) == 0 {
		return nil // open mode
	}

	subject := subjectFor(p)
	hasWildcard := false
	envs := make(map[string]bool)
	for i := range cfg.Groups {
		rule := &cfg.Groups[i]
		if !subject.ruleMatches(rule) {
			continue
		}
		if len(rule.Environments) == 0 {
			hasWildcard = true
			break
		}
		for _, e := range rule.Environments {
			envs[e] = true
		}
	}
	if hasWildcard {
		return nil // no restriction
	}
	result := make([]string, 0, len(envs))
	for e := range envs {
		result = append(result, e)
	}
	return result
}

// AccessibleDomainsFor returns the set of domains the caller p can access
// (empty set means "all" — no restriction).
func (m *Manager) AccessibleDomainsFor(p *VerifiedPrincipal) []string {
	cfg := m.Get()
	if len(cfg.Groups) == 0 {
		return nil
	}

	subject := subjectFor(p)
	hasWildcard := false
	doms := make(map[string]bool)
	for i := range cfg.Groups {
		rule := &cfg.Groups[i]
		if !subject.ruleMatches(rule) {
			continue
		}
		if len(rule.Domains) == 0 {
			hasWildcard = true
			break
		}
		for _, d := range rule.Domains {
			doms[d] = true
		}
	}
	if hasWildcard {
		return nil
	}
	result := make([]string, 0, len(doms))
	for d := range doms {
		result = append(result, d)
	}
	return result
}

// RulesMatching returns (shallow value copies of) every rule that applies to
// the caller p, decided by the same ruleMatches predicate the evaluation
// methods use. It exists so read-only renderings of "which rules hit" —
// today /api/v1/me's permissions map — converge on the single predicate
// instead of re-implementing rule matching outside this package. Match-block
// rules that the principal's groups/claims satisfy are therefore listed
// exactly like legacy name-matched rules.
//
// Read-only contract: the copies share slice/map backing storage with the
// live config snapshot; callers must not mutate the returned rules.
func (m *Manager) RulesMatching(p *VerifiedPrincipal) []GroupRule {
	cfg := m.Get()
	subject := subjectFor(p)
	var out []GroupRule
	for i := range cfg.Groups {
		if subject.ruleMatches(&cfg.Groups[i]) {
			out = append(out, cfg.Groups[i])
		}
	}
	return out
}

// scopeFieldModes evaluates one metadata field (environment or domain) against
// a matching rule's allow-list under BOTH scope modes at once, returning
// (passesShadow, passesEnforce). It is pure (no side effects) — the would-deny
// recording happens once per tenant at the decision site (recordScopeShadowGap),
// not per field, so the counter measures would-be-hidden tenants rather than
// field-checks (ADR-027 / LD-6 P1).
//
//   - Empty allow-list → (true, true):  the rule does not restrict this field.
//   - Empty value      → (true, false): unlabeled tenant on a restricted field —
//     shadow is lenient (passes, legacy fail-open), enforce is strict (denies).
//   - Labeled value    → (ok, ok):      exact membership, identical in both modes.
//
// Shared across scope axes (P1 metadata; P4 org) as the pure evaluation rail.
func scopeFieldModes(allowList []string, value string) (passShadow, passEnforce bool) {
	if len(allowList) == 0 {
		return true, true // wildcard — no restriction on this field
	}
	if value == "" {
		return true, false // unlabeled: shadow allows, enforce denies
	}
	ok := metadataMatches(allowList, value)
	return ok, ok
}

// scopeSetModes is the org-axis analogue of scopeFieldModes (ADR-027 / LD-6 P4):
// a set-membership test of the caller's org value against the tenant's org list,
// evaluated under BOTH scope modes at once, returning (passesShadow,
// passesEnforce). Pure — the would-deny recording happens once per axis at the
// decision site (recordScopeShadowGap in ScopeAllowed for the list plane,
// AllowedInOrg for the write plane), not here.
//
//   - len(tenantOrgs)==0 (UNLABELED tenant) → (true, false): shadow allows
//     (migration leniency, would-deny recorded), enforce denies. ⚠️ This is the
//     OPPOSITE of scopeFieldModes' empty-allow-list wildcard: an empty org list
//     means "tenant not yet assigned to any org", NOT "no org restriction". An
//     org-scoped rule with a wildcard-on-empty here would grant every unlabeled
//     tenant to every caller — the exact leak org-scope exists to prevent.
//   - userOrgVal=="" (caller carries no org claim) on a LABELED tenant →
//     (false, false): denied in both modes (no basis to match), mirroring a
//     metadata labeled-non-match.
//   - LABELED tenant, non-empty caller org → (ok, ok) with ok = membership;
//     identical in both modes (a labeled non-match is denied even in shadow).
func scopeSetModes(userOrgVal string, tenantOrgs []string) (passShadow, passEnforce bool) {
	if len(tenantOrgs) == 0 {
		return true, false // unlabeled tenant: shadow allows, enforce denies
	}
	if userOrgVal == "" {
		return false, false // labeled tenant, no caller org → no basis to match
	}
	for _, o := range tenantOrgs {
		if o == userOrgVal {
			return true, true
		}
	}
	return false, false // labeled non-match: denied in both modes
}

// recordScopeShadowGap records one would-deny for axis iff a subject is visible
// under shadow but would be hidden under enforce — i.e. its access hinges on the
// unlabeled-tenant leniency. Called once per scope decision (per user+tenant),
// so the counter measures would-be-hidden subjects, not per-field checks: a
// tenant with two restricted-and-unlabeled fields is one observation, and a
// tenant that another rule grants under strict semantics is zero (no false
// positive that would keep the shadow-soak counter off zero forever). Under
// enforce mode the same condition holds for a tenant that IS being hidden, so
// the counter keeps doubling as a "denied by scope" signal. nil sink → no-op.
// Shared across scope axes (P1 metadata; P4 org).
func (m *Manager) recordScopeShadowGap(shadowVisible, enforceVisible bool, axis string) {
	if shadowVisible && !enforceVisible && m.scopeAudit != nil {
		m.scopeAudit.IncWouldDeny(axis)
	}
}

// metadataMatches reports whether value is a member of a rule's allow-list.
// An empty allow-list is a wildcard (the rule places no restriction on this
// field). It no longer special-cases an empty value — the "unlabeled tenant on
// a restricted field" case is a scope decision handled mode-aware by the caller
// (scopeFieldModes), not silently fail-open here.
func metadataMatches(allowList []string, value string) bool {
	if len(allowList) == 0 {
		return true // wildcard — no restriction
	}
	for _, allowed := range allowList {
		if allowed == value {
			return true
		}
	}
	return false
}

// tenantMatches reports whether tenantID matches any pattern in the list.
// Patterns: "*" (wildcard), "prefix-*" (prefix), or exact match.
func tenantMatches(patterns []string, tenantID string) bool {
	for _, pat := range patterns {
		if pat == "*" {
			return true
		}
		if strings.HasSuffix(pat, "*") {
			prefix := strings.TrimSuffix(pat, "*")
			if strings.HasPrefix(tenantID, prefix) {
				return true
			}
			continue
		}
		if pat == tenantID {
			return true
		}
	}
	return false
}

// permCovers reports whether grant satisfies want (admin covers write and read).
func permCovers(grant, want Permission) bool {
	switch want {
	case PermRead:
		return grant == PermRead || grant == PermWrite || grant == PermAdmin
	case PermWrite:
		return grant == PermWrite || grant == PermAdmin
	case PermAdmin:
		return grant == PermAdmin
	}
	return false
}
