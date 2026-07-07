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
//	  - name: platform-admins
//	    tenants: ["*"]
//	    permissions: [read, write, admin]
//	  - name: db-operators
//	    tenants: ["db-a-*", "db-b-*"]
//	    permissions: [read, write]
package rbac

import (
	"fmt"
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
type GroupRule struct {
	Name         string       `yaml:"name"`
	Tenants      []string     `yaml:"tenants"`                // tenant IDs or patterns ("*", "db-a-*")
	Permissions  []Permission `yaml:"permissions"`            // [read, write, admin]
	Environments []string     `yaml:"environments,omitempty"` // ["production", "staging"] — empty = all
	Domains      []string     `yaml:"domains,omitempty"`      // ["finance", "ecommerce"] — empty = all
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
// HasPermission's `len(cfg.Groups) == 0` check then degrades to
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
	// nil (the default) means no claim axes are declared — the principal's
	// Claims stays nil and behavior is byte-identical to pre-P2. Set once at
	// startup via SetClaimHeaders.
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

// SetScopeAuditor installs the would-deny metric sink for scope filters
// (ADR-027 / LD-6 P1). Called once at startup. Passing nil leaves recording
// disabled (the filter still behaves correctly). Mirrors SetMachineAuditor.
func (m *Manager) SetScopeAuditor(a ScopeAuditRecorder) { m.scopeAudit = a }

// SetClaimHeaders installs the claimKey→headerName declaration for the
// identity-claims seam (ADR-027 / LD-6 P2), as parsed by ParseClaimHeaders.
// Called once at startup, before serving begins; it must NOT be called again
// after requests start flowing — the map is read per-request without locking,
// mirroring EnableMetadataScopeEnforce / SetScopeAuditor. Passing nil (the
// default state) leaves the seam closed: principals carry no claims and
// behavior is byte-identical to pre-P2.
func (m *Manager) SetClaimHeaders(h map[string]string) { m.claimHeaders = h }

// NewManager creates a Manager and loads the RBAC config from path.
// If path is empty, the manager starts in open mode (all
// authenticated users have read access, no write).
//
// Unlike the other config managers (groups / views / policy), an
// initial-load failure here is FATAL for the caller — the rbac
// gate is the only enforcement layer between identity headers and
// tenant data, so a config that cannot be parsed is not safe to
// serve. main.go calls log.Fatalf on this error.
func NewManager(path string) (*Manager, error) {
	w, err := configwatcher.New(path, "RBAC", parseConfig, emptyConfig)
	if err != nil {
		return nil, fmt.Errorf("rbac: initial load failed: %w", err)
	}
	// MED-8: a configured --rbac path that parses to zero groups is a
	// misconfiguration → fail closed. Path-less (open) mode keeps read.
	return &Manager{Watcher: w, failClosedOnEmpty: path != ""}, nil
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

func parseConfig(data []byte) (*RBACConfig, error) {
	var cfg RBACConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	return &cfg, nil
}

// HasPermission checks whether any of the provided IdP groups grants the
// specified permission for the given tenantID.
//
// Permission hierarchy: admin ⊇ write ⊇ read.
// An "admin" grant satisfies "write" and "read" checks.
func (m *Manager) HasPermission(idpGroups []string, tenantID string, want Permission) bool {
	cfg := m.Get()
	if len(cfg.Groups) == 0 {
		if m.failClosedOnEmpty {
			return false // MED-8: configured but empty _rbac.yaml → deny
		}
		// Open mode — authenticated users have read access only
		return want == PermRead
	}

	groupSet := make(map[string]bool, len(idpGroups))
	for _, g := range idpGroups {
		groupSet[g] = true
	}

	for _, rule := range cfg.Groups {
		if !groupSet[rule.Name] {
			continue
		}
		if !tenantMatches(rule.Tenants, tenantID) {
			continue
		}
		for _, p := range rule.Permissions {
			if permCovers(p, want) {
				return true
			}
		}
	}
	return false
}

// HasMetadataAccess checks whether any of the provided IdP groups grants
// access for a tenant with the given environment and domain metadata.
// Returns true if at least one matching rule allows the metadata values.
// Empty environment or domain in the tenant metadata always passes (no restriction).
func (m *Manager) HasMetadataAccess(idpGroups []string, tenantID, environment, domain string) bool {
	cfg := m.Get()
	if len(cfg.Groups) == 0 {
		if m.failClosedOnEmpty {
			return false // MED-8: configured but empty _rbac.yaml → deny
		}
		return true // open mode — no metadata restrictions
	}

	groupSet := make(map[string]bool, len(idpGroups))
	for _, g := range idpGroups {
		groupSet[g] = true
	}

	// Evaluate visibility under BOTH scope modes in one pass so the would-deny
	// signal is per-tenant, not per-field: the tenant is recorded iff it is
	// visible under shadow but would be hidden under enforce (its access hinges
	// on unlabeled-tenant leniency). A wildcard rule granting access under
	// strict semantics sets enforceVisible and suppresses the (false) would-deny.
	shadowVisible, enforceVisible := false, false
	for _, rule := range cfg.Groups {
		if !groupSet[rule.Name] {
			continue
		}
		if !tenantMatches(rule.Tenants, tenantID) {
			continue
		}
		envShadow, envEnforce := scopeFieldModes(rule.Environments, environment)
		domShadow, domEnforce := scopeFieldModes(rule.Domains, domain)
		if envShadow && domShadow {
			shadowVisible = true
		}
		if envEnforce && domEnforce {
			enforceVisible = true
		}
		if shadowVisible && enforceVisible {
			break // both outcomes decided; further rules cannot change either
		}
	}

	m.recordScopeShadowGap(shadowVisible, enforceVisible, scopeAxisMetadata)
	if m.metadataScopeEnforce {
		return enforceVisible
	}
	return shadowVisible
}

// AccessibleEnvironments returns the set of environments the user's IdP groups
// can access (empty set means "all" — no restriction).
func (m *Manager) AccessibleEnvironments(idpGroups []string) []string {
	cfg := m.Get()
	if len(cfg.Groups) == 0 {
		return nil // open mode
	}

	groupSet := make(map[string]bool, len(idpGroups))
	for _, g := range idpGroups {
		groupSet[g] = true
	}

	hasWildcard := false
	envs := make(map[string]bool)
	for _, rule := range cfg.Groups {
		if !groupSet[rule.Name] {
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

// AccessibleDomains returns the set of domains the user's IdP groups
// can access (empty set means "all" — no restriction).
func (m *Manager) AccessibleDomains(idpGroups []string) []string {
	cfg := m.Get()
	if len(cfg.Groups) == 0 {
		return nil
	}

	groupSet := make(map[string]bool, len(idpGroups))
	for _, g := range idpGroups {
		groupSet[g] = true
	}

	hasWildcard := false
	doms := make(map[string]bool)
	for _, rule := range cfg.Groups {
		if !groupSet[rule.Name] {
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
