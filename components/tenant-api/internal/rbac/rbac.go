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

	"github.com/vencil/tenant-api/internal/confd"
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
// ADR-027 §7: Added the optional Match block. Without it (the legacy
// shape) a rule applies iff Name equals one of the caller's IdP groups —
// byte-identical to the pre-P3 behavior, evaluated on the SAME code path
// (ruleMatches degenerates to the group-name test, not a separate branch).
// With Match present, Name becomes a pure label / audit identifier and the
// rule applies iff the Match conditions hold (see MatchBlock).
type GroupRule struct {
	Name         string       `yaml:"name" json:"name"`
	Match        *MatchBlock  `yaml:"match,omitempty" json:"match,omitempty"`               // optional claims-aware matcher; nil = legacy name matching
	Tenants      []string     `yaml:"tenants" json:"tenants"`                               // tenant IDs or patterns ("*", "db-a-*")
	Permissions  []Permission `yaml:"permissions" json:"permissions"`                       // [read, write, admin]
	Environments []string     `yaml:"environments,omitempty" json:"environments,omitempty"` // ["production", "staging"] — empty = all
	Domains      []string     `yaml:"domains,omitempty" json:"domains,omitempty"`           // ["finance", "ecommerce"] — empty = all

	// OrgScope opts this rule into the org-scope axis (ADR-027 §7).
	// Empty (the default) = the rule places no org restriction and behaves
	// byte-identically to pre-P4. When set, its value is the claim KEY (e.g.
	// "org-code") whose caller value must be one of the target tenant's orgs
	// (from _tenant_orgs.yaml, keyed by tenant ID) for the rule to grant that
	// tenant. The key MUST be declared in the oauth2-proxy config
	// (validateConfig enforces this at load — an org-scope on an undeclared
	// claim could never match and must fail loud, not silently deny).
	OrgScope string `yaml:"org-scope,omitempty" json:"org-scope,omitempty"`
}

// MatchBlock is the claims-aware rule matcher (ADR-027 §7).
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
// claim key not declared in the oauth2-proxy config is a load error; a
// principal missing a required claim simply does not match.
//
// Namespace honesty (single-trusted-hop MVP): the claim KEY is the
// namespace unit. A deployment must not map two different upstream sources
// onto the same claim key — nothing here can tell them apart. A true
// issuer namespace arrives with JWT verification (iss), deferred to D2-A.
type MatchBlock struct {
	Groups []string            `yaml:"groups,omitempty" json:"groups,omitempty"` // OR-within: any one group qualifies
	Claims map[string][]string `yaml:"claims,omitempty" json:"claims,omitempty"` // claim key → allowed values (OR-within); keys AND-across
}

// RBACConfig is the parsed _rbac.yaml structure.
type RBACConfig struct {
	Groups []GroupRule `yaml:"groups" json:"groups"`
}

// Manager holds the hot-reloadable RBAC config. The hot-reload
// machinery (atomic.Value + SHA-256 dedup + WatchLoop) lives in the
// embedded configwatcher.Watcher; this type only adds the
// permission-check methods.
//
// No open mode (ADR-027 §7): an absent or empty _rbac.yaml — no --rbac path,
// or one that parses to zero groups — grants nothing. Every evaluation method
// below reaches that by iterating an empty rule set.
type Manager struct {
	*configwatcher.Watcher[RBACConfig]

	// scopeAudit is the optional denied-by-scope metric sink for scope filters
	// (instance-method DI, mirroring the rate-limiter bridge,
	// so metric state is not a package singleton and tests stay isolatable).
	// nil (the default) means no recording — the filter still behaves correctly,
	// it just emits no scope-denied counter. Set once at startup via
	// SetScopeAuditor. Shared across scope axes (P1 metadata; P4 org).
	scopeAudit ScopeAuditRecorder

	// claimHeaders (ADR-027 §7) declares which trusted-hop header
	// loads which named claim (claim name → headerName), read from
	// the oauth2-proxy config (--oauth2-proxy-config, proxyconfig.Load). Middleware hands it to
	// HeaderResolver so the resolved principal carries the named claims.
	// nil means no claim axes are declared — the principal's Claims stays
	// nil and behavior is byte-identical to pre-P2. Installed by NewManager
	// (P3): the same map feeds config validation (a match.claims key must
	// be a declared claim key), so the declaration and the enforcement can
	// never drift — which is why this is a constructor argument and no
	// longer a post-construction setter.
	claimHeaders map[string]string

	// orgResolver (ADR-027 §7) resolves a tenant ID to its organization
	// list for the read-by-id middleware gate. rbac must NOT import tenantorg
	// (import direction: handler → rbac), so the resolver is injected as a func
	// value wired from tenantorg.OrgsForTenant in main.go — the same DI shape as
	// SetScopeAuditor. nil (the default, e.g. a bare NewManager
	// in tests) means the middleware read gate resolves no orgs → the tenant is
	// treated as unlabeled, which with no org-scoped rule is byte-identical to the
	// pre-P4c org-blind read path. Only the middleware read path uses it; the
	// write plane resolves orgs handler-side via handler.OrgAllowed.
	orgResolver func(tenantID string) []string
}

// SetScopeAuditor installs the denied-by-scope metric sink for scope filters
// (ADR-027 §7). Called once at startup. Passing nil leaves recording
// disabled (the filter still behaves correctly).
func (m *Manager) SetScopeAuditor(a ScopeAuditRecorder) { m.scopeAudit = a }

// SetOrgResolver installs the tenant→organization-list resolver used by the
// read-by-id middleware gate (ADR-027 §7). Called once at startup from
// main, wired from tenantorg.OrgsForTenant. Passing nil leaves the read gate
// org-blind (tenants resolve as unlabeled). Kept a func-value setter — rather
// than importing tenantorg — so rbac stays free of the handler/tenantorg import
// direction, mirroring SetScopeAuditor.
func (m *Manager) SetOrgResolver(fn func(tenantID string) []string) { m.orgResolver = fn }

// NewManager creates a Manager and loads the RBAC config from path.
// If path is empty, the manager starts with an empty rule set, which grants
// nothing (ADR-027 §7: there is no open mode).
//
// claimHeaders is the claim-name→headerName map read from
// the oauth2-proxy config (proxyconfig.Load); nil means no claim axes.
// It is a constructor argument — not a setter — because the declared claim
// keys participate in config validation: the parse closure below captures
// them, so BOTH the initial load and every hot-reload reject a config whose
// match.claims references an undeclared key (ADR-027 §7 fail-loud).
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
	return &Manager{Watcher: w, claimHeaders: claimHeaders}, nil
}

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
// a zero struct); an empty config grants nothing (ADR-027 §7).
//
// declaredClaimKeys is the the oauth2-proxy config declaration captured by
// the NewManager parse closure; validateConfig rejects a config whose
// match.claims references a key outside it.
//
// ⛔ parse-tree's verdict first (ADR-036 §7 step 7, #3206; confd.Judge),
// wherever --rbac points: a file it refuses — a second document, even an
// empty one, a `!!binary` or `!` tag, a null key — is a load error (fatal at
// startup, last-good on a reload), never the empty config. yaml.v3 alone
// reads the first document and ignores the rest, so rules written after a
// `---` were silently dropped. The file is judged whether or not it sits in
// conf.d: tenant-api is its one reader (ADR-036 §12), and a second rule for
// the same file depending on its path would be two loaders.
func parseConfig(data []byte, declaredClaimKeys map[string]string) (*RBACConfig, error) {
	if err := confd.Judge(data); err != nil {
		return nil, err
	}
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
// block (ADR-027 §7). It runs inside the parse path, so BOTH the
// initial load and every hot-reload pass through it: an invalid config is
// rejected at load time (initial load → NewManager error → main fatal;
// hot-reload → configwatcher keeps the last-good snapshot and logs a WARN).
//
//   - An empty match block (no groups AND no claims) is an error: empty
//     match is NOT match-all.
//   - A match.claims key not present in declaredClaimKeys is an error: a
//     rule on an undeclared claim key could never match at runtime, and a
//     silently-dead authorization rule must fail loud instead (same
//     philosophy as the proxyconfig claim-name charset guard).
//   - Empty entries — a blank match.groups name, an empty match.claims
//     value list, a blank claim value — are errors: they could only arise
//     from an authoring mistake and would otherwise be silently unmatchable.
func validateConfig(cfg *RBACConfig, declaredClaimKeys map[string]string) error {
	for i := range cfg.Groups {
		if err := validateRule(&cfg.Groups[i], declaredClaimKeys); err != nil {
			return err
		}
	}
	return nil
}

// validateRule enforces the per-rule fail-closed guarantees of validateConfig
// for a single rule (ADR-027 §7). It returns nil when THIS rule
// passes every guard — tenant patterns, org-scope declaration, and (when
// present) the match block — and a fail-loud error otherwise. The guards and
// their messages are byte-identical to the inline loop body they were
// extracted from: the historical `continue` on a nil match block becomes a
// `return nil` here (this rule is done, no error).
func validateRule(rule *GroupRule, declaredClaimKeys map[string]string) error {
	// Tenant patterns are validated for EVERY rule (independently of the
	// match block, exactly like org-scope below): a rule can be legacy
	// name-matched AND still carry tenants. The grammar is "*" (full
	// wildcard), "<literal>-*" (single trailing "*") or an exact id. A
	// malformed pattern must fail loud: "**" collapses to prefix "*" in
	// tenantMatches and would HasPrefix-collide with the platform-scope "*"
	// gate query — passing platform gates while granting zero per-tenant
	// access (a fail-open-direction inconsistency) — while "*a*"/"a**"/""
	// silently match nothing (a dead authorization rule). Same philosophy as
	// the empty-match and undeclared-claim guards: a silently-unmatchable or
	// direction-inconsistent authorization rule fails loud at load.
	for _, pat := range rule.Tenants {
		if !validTenantPattern(pat) {
			return fmt.Errorf("rbac: rule %q: invalid tenant pattern %q (allowed: \"*\", a \"prefix-*\" with a single trailing \"*\", or an exact tenant id — no empty, embedded or repeated \"*\")", rule.Name, pat)
		}
	}
	// Org-scope opt-in (ADR-027 §7): the claim key an org-scoped rule
	// keys off MUST be declared in the oauth2-proxy config, exactly like a
	// match.claims key. Checked for EVERY rule — independently of the match
	// block — because a rule can be legacy name-matched AND org-scoped. An
	// org-scope on an undeclared claim could never carry a value at runtime,
	// so it would silently deny every tenant; that
	// dead authorization rule must fail loud at load, not deny in silence.
	if rule.OrgScope != "" {
		if _, declared := declaredClaimKeys[rule.OrgScope]; !declared {
			return fmt.Errorf("rbac: rule %q: org-scope key %q is not injected by the oauth2-proxy config (--oauth2-proxy-config): a claim no header carries can never match at runtime; inject the claim or remove org-scope", rule.Name, rule.OrgScope)
		}
	}
	if rule.Match == nil {
		return nil
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
			return fmt.Errorf("rbac: rule %q: match.claims key %q is not injected by the oauth2-proxy config (--oauth2-proxy-config): a claim no header carries can never match at runtime; inject the claim or remove the condition", rule.Name, key)
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
	return nil
}

// ── Principal-based evaluation core (ADR-027 §7) ──────────────────
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
// It evaluates exactly as the empty groups slice always has: no rule can ever
// match, so every check denies.

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
// With a match: block (ADR-027 §7) the rule's Name is a pure label;
// conditions AND across kinds, OR within a list:
//   - match.groups non-empty → at least one entry must be in the caller's
//     group set;
//   - every match.claims key → the principal must CARRY that claim and its
//     value must equal (exact string comparison) one of the allowed values.
//     A missing claim, a nil claims map (anonymous principals) or
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
// (ADR-027 §7) so every per-rule evaluation path shares the ONE
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

// scopeEval is the result of one per-tenant scope evaluation (ADR-027 §7).
// Every scope axis is enforced; there is no shadow mode.
//
//   - granted: at least one rule passes every check on its own (ruleMatches,
//     tenant pattern, the permission when one is asked for, and each of ITS
//     scope restrictions).
//   - metaBlocked / orgBlocked: at least one rule that would otherwise have
//     granted was cut by that axis. Read only when granted is false, to
//     attribute the denial for tenant_api_scope_denied_total{axis}.
//   - blockedRule: the name of the first such rule — the diagnostic handle for
//     the deny log line. Rule NAMES only; claim values never leave the
//     evaluation (principal.go logging discipline).
type scopeEval struct {
	granted     bool
	metaBlocked bool
	orgBlocked  bool
	blockedRule string
}

// scopeQuery describes one scope evaluation. checkPerm=false is the list
// plane (visibility: any matching rule counts, whatever it grants); metaLive=false
// leaves the metadata axis out of the decision (read-by-id, which has never
// bound environments/domains).
type scopeQuery struct {
	tenantID    string
	want        Permission
	checkPerm   bool
	tenantOrgs  []string
	environment string
	domain      string
	metaLive    bool
}

// evalScope is the single per-tenant scope evaluation core.
//
// ⚠️ Correctness core — a rule grants iff ruleMatches && tenantMatches &&
// (ruleGrants when a permission is asked) && its OWN metadata restriction
// passes && its OWN org restriction passes (per-rule fold). Unioning "any
// rule passes metadata" and "any rule passes org" separately and AND'ing the
// two unions would leak access no single rule grants (rule A passes metadata
// but fails org, rule B passes org but fails metadata); see
// TestScopeAllowed_CrossRuleUnionNoLeak / TestAllowedInOrg_CrossRuleUnionNoLeak.
//
// Pure with respect to observability: the decision-site wrappers own metric
// recording and logging.
//
// An empty rule set grants nothing (ADR-027 §7: no open mode).
func (m *Manager) evalScope(p *VerifiedPrincipal, q scopeQuery) scopeEval {
	var out scopeEval
	cfg := m.Get()
	subject := subjectFor(p)
	for i := range cfg.Groups {
		rule := &cfg.Groups[i]
		if !subject.ruleMatches(rule) {
			continue
		}
		if !tenantMatches(rule.Tenants, q.tenantID) {
			continue
		}
		if q.checkPerm && !ruleGrants(rule, q.want) {
			continue
		}
		metaOK := true
		if q.metaLive {
			metaOK = scopeFieldPasses(rule.Environments, q.environment) &&
				scopeFieldPasses(rule.Domains, q.domain)
		}
		orgOK := true // no org-scope on this rule = no org restriction
		if rule.OrgScope != "" {
			orgOK = scopeSetPasses(subject.claims[rule.OrgScope], q.tenantOrgs)
		}
		if metaOK && orgOK {
			out.granted = true
			return out
		}
		out.metaBlocked = out.metaBlocked || !metaOK
		out.orgBlocked = out.orgBlocked || !orgOK
		if out.blockedRule == "" {
			out.blockedRule = rule.Name
		}
	}
	return out
}

// Allowed checks whether the caller p is granted the wanted permission for
// the given tenantID by any rule matching the principal, WITHOUT the scope
// axes.
//
// Permission hierarchy: admin ⊇ write ⊇ read.
// An "admin" grant satisfies "write" and "read" checks.
//
// ⚠️ SCOPE-BLIND: Allowed never reads environments/domains/org-scope. Every
// per-tenant authorization decision MUST route through a scope-aware method
// instead: WRITE/ADMIN gates via AllowedInOrg (handler.OrgAllowed), and
// per-tenant READS via AllowedInOrgRead (the middleware read-by-id gate +
// handler.OrgAllowedRead for the collection filters). The only legitimate
// Allowed callers left are the platform-scope tenantID="*" checks for READS
// and checks that judge no tenant (invariant I6: there is no tenant org list
// for the org axis to compare against) and the middleware's own write-mount
// branch (which checks read on "*" or defers the per-tenant decision to the
// handler's RequireOrgWrite). Pinned by the go/ast tripwire, see
// guards_ast_test.go.
//
// ⛔ Allowed(p, "*", PermAdmin) is NOT a platform-admin bar: an ORG-SCOPED
// "*" admin rule passes it. A platform-admin decision uses
// PlatformAdminUnrestricted by default; PlatformAdminNonOrgScoped (reverse.go)
// only where environments and domains do not bear on the decision, at the
// call sites platform_gate_arch_test.go lists.
//
// Allowed records nothing: a scope-blind decision cannot be denied by scope.
func (m *Manager) Allowed(p *VerifiedPrincipal, tenantID string, want Permission) bool {
	cfg := m.Get()
	subject := subjectFor(p)
	for i := range cfg.Groups {
		rule := &cfg.Groups[i]
		if subject.ruleMatches(rule) && tenantMatches(rule.Tenants, tenantID) && ruleGrants(rule, want) {
			return true
		}
	}
	return false // includes the empty rule set: no open mode (ADR-027 §7)
}

// GrantsSomeTenant is the bar of the "any tenant" route gate
// (MiddlewareAnyTenant, #2520): may p hold `want` on at least one tenant?
// It replaces Allowed(p, "*", want) on the collection routes, which only a
// literal "*" rule passes — a caller granted ["db-a"] or ["db-*"] used to get
// 403 on /me, /tenants, /groups … before the handler's per-tenant filters
// (filterTenantsByRBAC, filterByRBAC / OrgAllowedRead, groupVisible, prVisible,
// eventVisible) could show them their own tenants.
//
// ⛔ The predicate is "some rule COULD grant `want` on some tenant", decided
// from the principal and _rbac.yaml ALONE — never "p can read at least one
// EXISTING tenant". Reasons:
//
//   - No new oracle. The answer depends on nothing but the caller's own rules —
//     the same facts GET /me hands back once the gate is passed. An
//     existence-based bar would make the 403/200 split a function of conf.d
//     and _tenant_orgs.yaml, so every route behind the gate — including the
//     ones whose handlers answer the same for "absent" and "not yours"
//     (federation tokens, batch, groups PUT) — would become a probe of "does a
//     tenant matching my pattern / in my org exist". With this bar, a rule
//     naming a tenant that does not exist passes and the handlers return what
//     they would for any reader: an empty list.
//   - Consistent with the filters. Each filter decides per tenant with the same
//     ruleMatches / ruleGrants / tenant pattern / org-scope evaluation; this is
//     its existential closure over every POSSIBLE tenant (any id, any metadata,
//     any org labels). Whatever passes here can still see only what those
//     filters let through, and anything a filter could ever show passes here.
//   - No per-request conf.d walk, and no gate that flips when a tenant is
//     created or deleted (an SSE stream or a UI session would start answering
//     403 without its rules changing).
//
// A rule counts iff ruleMatches(p) && ruleGrants(want) && it names at least
// one well-formed tenant pattern && its org-scope can pass for some tenant:
//
//   - no org-scope → yes;
//   - org-scope and p carries a value for the claim → yes (a tenant labeled
//     with that org passes; whether one exists is data);
//   - org-scope and p carries NO value → no: such a rule grants nothing on any
//     tenant (scopeSetPasses).
//
// The metadata axes (environments / domains) never disqualify a rule: a tenant
// labeled with an allowed value passes.
//
// An empty rule set denies everything (ADR-027 §7: no open mode).
//
// Records nothing: this is not a per-tenant decision.
func (m *Manager) GrantsSomeTenant(p *VerifiedPrincipal, want Permission) bool {
	cfg := m.Get()
	subject := subjectFor(p)
	for i := range cfg.Groups {
		rule := &cfg.Groups[i]
		if !subject.ruleMatches(rule) || !ruleGrants(rule, want) {
			continue
		}
		if !namesSomeTenant(rule.Tenants) {
			continue
		}
		if rule.OrgScope != "" && subject.claims[rule.OrgScope] == "" {
			continue // no org claim: matches no labeled tenant, and unlabeled ones are denied
		}
		return true
	}
	return false
}

// namesSomeTenant reports whether at least one pattern can match some tenant
// id. validateConfig already rejects malformed patterns at load; this is the
// same fail-closed backstop tenantMatches keeps for a rule built in a test.
func namesSomeTenant(patterns []string) bool {
	for _, pat := range patterns {
		if validTenantPattern(pat) {
			return true
		}
	}
	return false
}

// AllowedInOrg is the scope-aware per-tenant permission check and the ONLY
// write-plane authorization entry point (ADR-027 §7). It binds BOTH scope
// axes: a rule's environments[]/domains[] against the tenant's metadata
// (#1597) and its org-scope against tenantOrgs. tenantOrgs is the target
// tenant's organization list (tenantorg.OrgsForTenant), resolved by the caller
// AT DECISION TIME — rbac does not import tenantorg; nil/empty means the
// tenant is unlabeled, and an unlabeled tenant fails every rule that restricts
// that axis.
//
// A denial that a scope axis caused is counted on
// tenant_api_scope_denied_total{axis="metadata_write"|"org_write"}.
// Monotonicity holds by construction: AllowedInOrg ⟹ Allowed — the scope axes
// only ever narrow a grant, never widen one.
func (m *Manager) AllowedInOrg(p *VerifiedPrincipal, tenantID string, want Permission,
	tenantOrgs []string, environment, domain string) bool {
	return m.allowedScopedOnAxes(p, scopeQuery{
		tenantID: tenantID, want: want, checkPerm: true,
		tenantOrgs: tenantOrgs, environment: environment, domain: domain, metaLive: true,
	}, scopeAxisMetadataWrite, scopeAxisOrgWrite, "write-plane")
}

// AllowedInOrgRejudge is AllowedInOrg for a write-plane decision taken again
// on another tree — a PR-mode write judged once more, under the writer lock,
// on the fresh base its branch is cut from (#2857). The decision is the same
// as AllowedInOrg's for the same inputs, so it is always taken; what differs
// is the bookkeeping, because AllowedInOrg already counted the first
// decision. Each axis's denial is recorded only when the caller asks for it —
// it sets countMetadata when the tenant's environment/domain on this tree
// differs from what the first decision judged, and countOrg when its
// organizations do — so a write is counted once per value it is judged on.
func (m *Manager) AllowedInOrgRejudge(p *VerifiedPrincipal, tenantID string, want Permission,
	tenantOrgs []string, environment, domain string, countMetadata, countOrg bool) bool {
	ev := m.evalScope(p, scopeQuery{
		tenantID: tenantID, want: want, checkPerm: true,
		tenantOrgs: tenantOrgs, environment: environment, domain: domain, metaLive: true,
	})
	if !ev.granted {
		if countMetadata && ev.metaBlocked {
			m.recordScopeDenied(scopeAxisMetadataWrite)
		}
		if countOrg && ev.orgBlocked {
			m.recordScopeDenied(scopeAxisOrgWrite)
		}
	}
	return ev.granted
}

// AllowedInOrgRead is the org-scope-aware per-tenant permission check for the
// READ/VISIBILITY plane: the read-by-id middleware gate and the collection
// read filters (filterByRBAC / hasAccessibleMember / ListPRs via
// handler.OrgAllowedRead). It records a denial on the scopeAxisOrg ("org")
// series — the SAME axis the list-plane ScopeAllowed records — so the
// read/visibility plane and the list plane share one signal, while the write
// plane keeps its own scopeAxisOrgWrite series.
//
// Metadata-blind by design: read-by-id has never bound environments/domains;
// the list plane (ScopeAllowed) and the write plane (AllowedInOrg) do.
func (m *Manager) AllowedInOrgRead(p *VerifiedPrincipal, tenantID string, want Permission, tenantOrgs []string) bool {
	return m.allowedScopedOnAxes(p, scopeQuery{
		tenantID: tenantID, want: want, checkPerm: true, tenantOrgs: tenantOrgs,
	}, "", scopeAxisOrg, "read-plane")
}

// allowedScopedOnAxes is the shared scope decision + denial recording for
// AllowedInOrg (write plane) and AllowedInOrgRead (read plane). metaAxis /
// orgAxis select which scope_denied series records a denial (metaAxis "" =
// the metadata axis is not bound on this plane); plane names the plane in the
// deny log line.
func (m *Manager) allowedScopedOnAxes(p *VerifiedPrincipal, q scopeQuery, metaAxis, orgAxis, plane string) bool {
	ev := m.evalScope(p, q)
	if ev.granted {
		return true
	}
	if ev.metaBlocked && metaAxis != "" {
		m.recordScopeDenied(metaAxis)
	}
	if ev.orgBlocked {
		m.recordScopeDenied(orgAxis)
	}
	if ev.blockedRule != "" {
		// A scope axis curtailed an otherwise-granting rule → loud enough to
		// debug a 403. Tenant + rule NAME only; claim values never reach logs
		// (principal.go multi-value-refusal discipline).
		slog.Warn("scope denied "+plane+" permission",
			"tenant", q.tenantID, "rule", ev.blockedRule, "perm", string(q.want),
			"metadata", ev.metaBlocked, "org", ev.orgBlocked)
	}
	return false
}

// MetadataAllowed checks whether the caller p is granted access for a tenant
// with the given environment and domain metadata, WITHOUT the org axis. It is
// the org-less thin wrapper over ScopeAllowed (tenantOrgs=nil): with no
// org-scoped rule the org axis does not restrict, and the result is the
// metadata-only filter.
func (m *Manager) MetadataAllowed(p *VerifiedPrincipal, tenantID, environment, domain string) bool {
	return m.ScopeAllowed(p, tenantID, environment, domain, nil)
}

// ScopeAllowed is the single scope-filter decision for the tenant list: it
// evaluates the metadata (environment/domain) axis and the org axis TOGETHER,
// per matching rule (evalScope's per-rule fold), and returns whether the
// tenant is visible to caller p (ADR-027 §7).
//
// An unlabeled tenant — no value for a field a matching rule restricts, or no
// orgs in _tenant_orgs.yaml under an org-scoped rule — is NOT visible through
// that rule. `tenant-api config check --kind scope` lists those tenants
// before an upgrade.
//
// tenantOrgs is the target tenant's organization list (tenantorg.OrgsForTenant);
// nil/empty means the tenant is unlabeled. rbac does not import tenantorg — the
// handler resolves the orgs and passes them in.
func (m *Manager) ScopeAllowed(p *VerifiedPrincipal, tenantID, environment, domain string, tenantOrgs []string) bool {
	ev := m.evalScope(p, scopeQuery{
		tenantID: tenantID, tenantOrgs: tenantOrgs,
		environment: environment, domain: domain, metaLive: true,
	})
	if ev.granted {
		return true
	}
	if ev.metaBlocked {
		m.recordScopeDenied(scopeAxisMetadata)
	}
	if ev.orgBlocked {
		m.recordScopeDenied(scopeAxisOrg)
	}
	return false
}

// ScopeAllowedUnknownMetadata is the list-plane visibility decision for a
// tenant whose environment/domain are UNKNOWN — its conf.d file exists but is
// not usable, so its `_metadata` could not be read (#1680's degraded row).
//
// ⛔ Unknown is NOT unlabeled, which is why this is not ScopeAllowed(p, id,
// "", "", orgs): a rule grants here only if it places NO restriction on
// either metadata axis (empty Environments AND empty Domains). An
// environment-restricted caller must not see a tenant whose real environment
// — merely unreadable right now — they may not be allowed.
//
// The org axis IS evaluated exactly as ScopeAllowed does: tenantOrgs comes from
// _tenant_orgs.yaml, which is independent of the broken file.
//
// Per-rule fold, like ScopeAllowed: a single rule must pass tenant pattern,
// the metadata wildcard AND its own org restriction.
//
// Denial recording: the ORG axis only. A broken file is an incident state,
// not a labeling gap, so the metadata axis records nothing here.
func (m *Manager) ScopeAllowedUnknownMetadata(p *VerifiedPrincipal, tenantID string, tenantOrgs []string) bool {
	cfg := m.Get()
	subject := subjectFor(p)
	orgBlocked := false
	for i := range cfg.Groups {
		rule := &cfg.Groups[i]
		if !subject.ruleMatches(rule) {
			continue
		}
		if !tenantMatches(rule.Tenants, tenantID) {
			continue
		}
		if ruleRestrictsMetadata(rule) {
			continue // restricted on a metadata axis whose value is unknown
		}
		if rule.OrgScope == "" || scopeSetPasses(subject.claims[rule.OrgScope], tenantOrgs) {
			return true
		}
		orgBlocked = true
	}
	if orgBlocked {
		m.recordScopeDenied(scopeAxisOrg)
	}
	return false // includes the empty rule set: no open mode (ADR-027 §7)
}

// accessibleFieldFor is the shared implementation behind AccessibleEnvironmentsFor
// and AccessibleDomainsFor: the two differ ONLY in which per-rule string slice
// they collect, supplied here by selector. A matched rule with an EMPTY
// selected slice is the wildcard that short-circuits to nil ("no
// restriction"); otherwise the union of the selected values across every
// matched rule is returned (map-collected, so unordered and de-duplicated).
// No matched rule — including the empty rule set — yields a non-nil empty
// slice: no access.
func (m *Manager) accessibleFieldFor(p *VerifiedPrincipal, selector func(*GroupRule) []string) []string {
	cfg := m.Get()
	subject := subjectFor(p)
	hasWildcard := false
	values := make(map[string]bool)
	for i := range cfg.Groups {
		rule := &cfg.Groups[i]
		if !subject.ruleMatches(rule) {
			continue
		}
		field := selector(rule)
		if len(field) == 0 {
			hasWildcard = true
			break
		}
		for _, v := range field {
			values[v] = true
		}
	}
	if hasWildcard {
		return nil // no restriction
	}
	result := make([]string, 0, len(values))
	for v := range values {
		result = append(result, v)
	}
	return result
}

// AccessibleEnvironmentsFor returns the set of environments the caller p
// can access (nil means "all" — no restriction; a non-nil empty slice means
// no access).
func (m *Manager) AccessibleEnvironmentsFor(p *VerifiedPrincipal) []string {
	return m.accessibleFieldFor(p, func(r *GroupRule) []string { return r.Environments })
}

// AccessibleDomainsFor returns the set of domains the caller p can access
// (nil means "all" — no restriction; a non-nil empty slice means no access).
func (m *Manager) AccessibleDomainsFor(p *VerifiedPrincipal) []string {
	return m.accessibleFieldFor(p, func(r *GroupRule) []string { return r.Domains })
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

// scopeFieldPasses evaluates one metadata field (environment or domain)
// against a matching rule's allow-list (ADR-027 §7):
//
//   - Empty allow-list → true:  the rule does not restrict this field.
//   - Empty value      → false: unlabeled tenant on a restricted field.
//   - Labeled value    → exact membership.
func scopeFieldPasses(allowList []string, value string) bool {
	if len(allowList) == 0 {
		return true // wildcard — no restriction on this field
	}
	if value == "" {
		return false // unlabeled tenant on a restricted field
	}
	return metadataMatches(allowList, value)
}

// scopeSetPasses is the org-axis analogue of scopeFieldPasses: a
// set-membership test of the caller's org value against the tenant's org list.
//
//   - len(tenantOrgs)==0 (UNLABELED tenant) → false. ⚠️ This is the OPPOSITE of
//     scopeFieldPasses' empty-allow-list wildcard: an empty org list means
//     "tenant not yet assigned to any org", NOT "no org restriction". A
//     wildcard-on-empty here would grant every unlabeled tenant to every
//     caller — the exact leak org-scope exists to prevent.
//   - userOrgVal=="" (caller carries no org claim) → false: no basis to match.
//   - otherwise → membership.
func scopeSetPasses(userOrgVal string, tenantOrgs []string) bool {
	if len(tenantOrgs) == 0 {
		return false // unlabeled tenant: no org to match
	}
	if userOrgVal == "" {
		return false // caller carries no org claim: no basis to match
	}
	for _, o := range tenantOrgs {
		if o == userOrgVal {
			return true
		}
	}
	return false
}

// recordScopeDenied counts one decision that a scope axis denied — a rule
// matched the caller and the tenant (and granted the permission asked for)
// but its scope restriction on axis did not pass, and no other rule granted.
// Called at most once per axis per decision, so the counter measures denied
// decisions, not per-field checks. nil sink → no-op.
func (m *Manager) recordScopeDenied(axis string) {
	if m.scopeAudit != nil {
		m.scopeAudit.IncScopeDenied(axis)
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
			// Fail-closed backstop for a malformed pattern that bypassed
			// validateConfig (e.g. a rule built directly in a test, not parsed
			// from YAML): a prefix that is empty or still contains "*" comes from
			// "*", "**", "a**" etc. Never prefix-match it — otherwise "**" leaves
			// prefix "*" and HasPrefix("*", "*") would fail the rule OPEN onto a
			// platform-scope "*" gate query (Allowed(p, "*", …)) while granting
			// zero per-tenant access. validateConfig rejects these at load.
			if prefix == "" || strings.Contains(prefix, "*") {
				continue
			}
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

// validTenantPattern reports whether pat is a well-formed tenant match pattern:
// the full wildcard "*", a prefix pattern "<literal>*" with a single trailing
// "*" and a non-empty literal ("db-a-*"), or an exact tenant id (no "*"). It is
// the allowlist counterpart to tenantMatches' grammar (the GroupRule.Tenants
// struct comment): validateConfig rejects everything else at load so a typo
// like "**" — which would otherwise collapse to prefix "*" and fail open onto
// platform-scope "*" gates — never reaches the matcher.
func validTenantPattern(pat string) bool {
	if pat == "*" {
		return true // full wildcard
	}
	switch strings.Count(pat, "*") {
	case 0:
		// exact id — reject blank / whitespace-only, mirroring the
		// match.groups / match.claims blank-entry guards in validateConfig.
		return strings.TrimSpace(pat) != ""
	case 1:
		// prefix pattern: the single "*" must be the trailing char, with a
		// non-empty literal before it. The lone "*" is already handled above.
		return strings.HasSuffix(pat, "*") && len(pat) > 1
	default:
		return false // repeated "*" ("**", "*a*", "a**") — malformed
	}
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
