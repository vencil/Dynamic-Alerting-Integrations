// Package policy implements domain policy enforcement at the API layer.
//
// v2.5.0 Phase C: Domain policies are loaded from _domain_policy.yaml and
// enforced on write operations. Violations that were previously WARN-only
// (CI validation) now return 403 at API time.
//
// Schema (_domain_policy.yaml):
//
//	domain_policies:
//	  finance:
//	    description: "Finance domain compliance requirements"
//	    tenants: [db-a, db-b]
//	    constraints:
//	      allowed_receiver_types: [pagerduty, email, opsgenie]
//	      forbidden_receiver_types: [slack, webhook]
//	      enforce_group_by: [tenant, alertname, severity]
//	      max_repeat_interval: 1h
//	      min_group_wait: 30s
//	      require_critical_escalation: true
//
// Concurrency: reads are lock-free (atomic.Value). Hot-reloaded via SHA-256
// (the underlying configwatcher.Watcher dedups disk reads on each tick).
package policy

import (
	"fmt"
	"log/slog"
	"path/filepath"
	"sort"
	"strings"

	"github.com/vencil/tenant-api/internal/configwatcher"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
	"gopkg.in/yaml.v3"
)

// Constraints defines the constraints for a domain policy.
type Constraints struct {
	AllowedReceiverTypes   []string `yaml:"allowed_receiver_types"`
	ForbiddenReceiverTypes []string `yaml:"forbidden_receiver_types"`
	EnforceGroupBy         []string `yaml:"enforce_group_by"`
	MaxRepeatInterval      string   `yaml:"max_repeat_interval"`
	MinGroupWait           string   `yaml:"min_group_wait"`
	// RequireCriticalEscalation is read as the generator's PyYAML reads it
	// (routingpolicy.DecodePyYAML, #2325): a plain `yes` / `on` or a
	// `!!bool yEs` is true and `no` / `off` false. Only a boolean `true` turns
	// the constraint on (the generator's `is True`); any other value PyYAML
	// reads (`"true"`, `1`, any mapping or list) is logged once per load and
	// left off, and the rest of the file still applies. A scalar PyYAML refuses (`!!bool y`,
	// `!!int abc`) fails the file, as the generator drops it: a hot reload
	// keeps the last good policy and records the failure.
	RequireCriticalEscalation routingpolicy.PyYAMLValue `yaml:"require_critical_escalation"`
}

// DomainPolicy defines a single domain's compliance constraints.
type DomainPolicy struct {
	Description string      `yaml:"description"`
	Tenants     []string    `yaml:"tenants"`
	Constraints Constraints `yaml:"constraints"`
}

// DomainPolicyConfig is the parsed _domain_policy.yaml structure.
type DomainPolicyConfig struct {
	DomainPolicies map[string]DomainPolicy `yaml:"domain_policies"`
}

// Violation represents a single policy violation.
type Violation struct {
	Domain     string `json:"domain"`
	Constraint string `json:"constraint"`
	// Target is the route whose receiver breaks the constraint: `receiver`
	// (the tenant's main route), `overrides[i]` or `routes[i]`. Omitted for
	// the flat batch key `_routing_receiver_type`.
	Target  string `json:"target,omitempty"`
	Message string `json:"message"`
}

// Manager holds the hot-reloadable domain policy config. The
// hot-reload machinery (atomic.Value + SHA-256 dedup + WatchLoop)
// lives in the embedded configwatcher.Watcher; this type only adds
// the policy-specific check methods.
type Manager struct {
	*configwatcher.Watcher[DomainPolicyConfig]
}

// NewManager creates a Manager that reads _domain_policy.yaml from configDir.
func NewManager(configDir string) *Manager {
	path := filepath.Join(configDir, "_domain_policy.yaml")
	w, err := configwatcher.New(path, "policy", parseConfig, emptyConfig)
	if err != nil {
		slog.Warn("policy: initial load failed", "error", err)
	}
	return &Manager{Watcher: w}
}

// NewForTest returns a Manager pre-populated with cfg and no file
// path. WatchLoop becomes a no-op; only the embedded check methods
// are exercised. Intended for unit tests.
func NewForTest(cfg *DomainPolicyConfig) *Manager {
	return &Manager{Watcher: configwatcher.NewForTest("policy", cfg)}
}

func emptyConfig() *DomainPolicyConfig {
	return &DomainPolicyConfig{DomainPolicies: make(map[string]DomainPolicy)}
}

func parseConfig(data []byte) (*DomainPolicyConfig, error) {
	var cfg DomainPolicyConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	if cfg.DomainPolicies == nil {
		cfg.DomainPolicies = make(map[string]DomainPolicy)
	}
	for _, name := range sortedDomains(&cfg) {
		v := cfg.DomainPolicies[name].Constraints.RequireCriticalEscalation.Value
		if _, isBool := v.(bool); v != nil && !isBool {
			slog.Warn("policy: require_critical_escalation is not a boolean; the constraint is not enforced",
				"domain", name, "value", fmt.Sprint(v))
		}
	}
	return &cfg, nil
}

// CheckWrite validates a flat batch patch against applicable domain policies.
// Returns violations (empty if the write is allowed).
//
// tenantID: the target tenant.
// patch: the key-value pairs being written.
//
// It judges the flat `_routing_receiver_type` key only. A tenant's routing as
// the generator renders it — `_routing_defaults`, the referenced routing
// profile and the tenant's `_routing`, main receiver, overrides and routes —
// is CheckTenantRouting's (#2280); the nested `_routing.receiver.type` key
// this used to read saw the tenant's own main receiver only, and PUT now goes
// through CheckTenantRouting instead.
func (m *Manager) CheckWrite(tenantID string, patch map[string]string) []Violation {
	receiverType, ok := patch["_routing_receiver_type"]
	if !ok {
		return nil
	}
	cfg := m.Get()
	var violations []Violation
	for _, domainName := range sortedDomains(cfg) {
		dp := cfg.DomainPolicies[domainName]
		if !isTenantInPolicy(dp.Tenants, tenantID) {
			continue
		}
		violations = append(violations, checkReceiverType(domainName, dp.Constraints, receiverType, "", "")...)
	}
	return violations
}

// RoutingPolicies is the receiver-type part of the loaded domain policies, in
// the shape pkg/routingpolicy judges.
func (m *Manager) RoutingPolicies() []routingpolicy.Policy {
	cfg := m.Get()
	out := make([]routingpolicy.Policy, 0, len(cfg.DomainPolicies))
	for _, name := range sortedDomains(cfg) {
		dp := cfg.DomainPolicies[name]
		out = append(out, routingpolicy.Policy{
			Domain:                 name,
			Tenants:                dp.Tenants,
			ForbiddenReceiverTypes: dp.Constraints.ForbiddenReceiverTypes,
			AllowedReceiverTypes:   dp.Constraints.AllowedReceiverTypes,
			AllowedListNonEmpty:    len(dp.Constraints.AllowedReceiverTypes) > 0,
			// #2325: only a boolean true (parseConfig logs anything else).
			RequireCriticalEscalation: dp.Constraints.RequireCriticalEscalation.Value == true,
		})
	}
	return out
}

// CheckTenantRouting judges one tenant's routing as the route generator
// renders it (#2280): block is the tenant's config block (its `_routing` and
// `_routing_profile`), resolved over layers (`_routing_defaults` → profile →
// the tenant's `_routing`, `{{tenant}}` substituted), and every receiver the
// result renders — main route, overrides, routes — is judged against the
// receiver-type constraints of each policy that lists the tenant.
// forbidden_receiver_types and allowed_receiver_types are independent tests,
// so one receiver can yield two violations (the generator's --strict
// semantics). A tenant with no resolved routing yields none.
//
// #2325: a policy with `require_critical_escalation: true` also refuses a
// routing whose severity=critical alerts reach no pagerduty receiver
// (routingpolicy.CheckCriticalEscalation; constraint
// require_critical_escalation, Target `receiver`). The non-blocking half —
// a non-pagerduty destination that still catches some critical alerts first
// — is JudgeTenantRouting's advisories.
func (m *Manager) CheckTenantRouting(tenantID string, block map[string]any, layers routingpolicy.Layers) []Violation {
	violations, _ := m.JudgeTenantRouting(tenantID, block, layers)
	return violations
}

// JudgeTenantRouting is CheckTenantRouting plus its advisories: one message
// per `require_critical_escalation` leak (#2325), never blocking. Each names
// the tenant (`tenant=<id>`), so it reads alone in a batch-level list.
func (m *Manager) JudgeTenantRouting(tenantID string, block map[string]any, layers routingpolicy.Layers) ([]Violation, []string) {
	pols := m.RoutingPolicies()
	if len(pols) == 0 {
		return nil, nil
	}
	resolved, ok, prov, _ := routingpolicy.Resolve(tenantID, block, layers)
	if !ok {
		return nil, nil
	}
	var out []Violation
	for _, v := range routingpolicy.CheckReceiverTypes(tenantID, resolved, pols) {
		topKey := v.Target
		if i := strings.IndexByte(topKey, '['); i >= 0 {
			topKey = topKey[:i]
		}
		source := routingpolicy.SourceTenant
		if s, found := prov[topKey]; found {
			source = s
		}
		where := fmt.Sprintf("%s (from %s)", v.Target, routingpolicy.Describe(source))
		out = append(out, violation(v.Domain, v.Constraint, v.ReceiverType, v.Target, where))
	}
	escalation := strings.Join(routingpolicy.EscalationTypes, ", ")
	var advisories []string
	for _, f := range routingpolicy.CheckCriticalEscalation(tenantID, resolved, pols) {
		if !f.Verdict.Compliant() {
			out = append(out, Violation{
				Domain:     f.Domain,
				Constraint: routingpolicy.ConstraintRequireCriticalEscalation,
				Target:     routingpolicy.MainReceiverRef,
				Message: fmt.Sprintf("severity=critical alerts reach no receiver of type %s (main receiver type '%s', "+
					"and no routes entry matches severity=critical with such a receiver), but domain policy '%s' "+
					"requires critical escalation", escalation, routingpolicy.ReceiverType(resolved["receiver"]), f.Domain),
			})
			continue
		}
		for _, l := range f.Verdict.Leaks {
			if l.Ref == routingpolicy.MainReceiverRef {
				advisories = append(advisories, fmt.Sprintf(
					"tenant=%s: domain policy '%s': severity=critical alerts that no sub-route catches go to the main "+
						"receiver (type '%s'), not a receiver of type %s", tenantID, f.Domain, l.ReceiverType, escalation))
				continue
			}
			advisories = append(advisories, fmt.Sprintf(
				"tenant=%s: domain policy '%s': %s (%s) receiver type '%s' catches alerts with %s before any receiver "+
					"of type %s does, so they never reach one", tenantID, f.Domain, l.Ref,
				routingpolicy.FormatLabels(l.Match), l.ReceiverType, routingpolicy.FormatLabels(l.Caught), escalation))
		}
	}
	return out, advisories
}

func sortedDomains(cfg *DomainPolicyConfig) []string {
	names := make([]string, 0, len(cfg.DomainPolicies))
	for name := range cfg.DomainPolicies {
		names = append(names, name)
	}
	sort.Strings(names)
	return names
}

// PolicyForTenant returns the domain name and policy for a tenant, or empty if none applies.
func (m *Manager) PolicyForTenant(tenantID string) (string, *DomainPolicy, bool) {
	cfg := m.Get()
	for name, dp := range cfg.DomainPolicies {
		if isTenantInPolicy(dp.Tenants, tenantID) {
			return name, &dp, true
		}
	}
	return "", nil, false
}

func isTenantInPolicy(tenants []string, tenantID string) bool {
	for _, t := range tenants {
		if t == tenantID {
			return true
		}
	}
	return false
}

// checkReceiverType judges one receiver type against one domain's
// constraints. forbidden_receiver_types and allowed_receiver_types are
// INDEPENDENT tests (#2280, the route generator's semantics): a type on the
// forbidden list that is also missing from the allowed list is two
// violations. target / where name the route in the violation ("" for the
// flat batch key).
func checkReceiverType(domain string, c Constraints, receiverType, target, where string) []Violation {
	var violations []Violation
	for _, forbidden := range c.ForbiddenReceiverTypes {
		if receiverType == forbidden {
			violations = append(violations, violation(domain, "forbidden_receiver_types", receiverType, target, where))
			break
		}
	}
	if len(c.AllowedReceiverTypes) > 0 {
		allowed := false
		for _, a := range c.AllowedReceiverTypes {
			if receiverType == a {
				allowed = true
				break
			}
		}
		if !allowed {
			violations = append(violations, violation(domain, "allowed_receiver_types", receiverType, target, where))
		}
	}
	return violations
}

// violation is one Violation with its operator message.
func violation(domain, constraint, receiverType, target, where string) Violation {
	subject := fmt.Sprintf("receiver type '%s'", receiverType)
	if where != "" {
		subject += " at " + where
	}
	msg := fmt.Sprintf("%s is forbidden by domain policy '%s'", subject, domain)
	if constraint == "allowed_receiver_types" {
		msg = fmt.Sprintf("%s is not in the allowed list for domain policy '%s'", subject, domain)
	}
	return Violation{Domain: domain, Constraint: constraint, Target: target, Message: msg}
}
