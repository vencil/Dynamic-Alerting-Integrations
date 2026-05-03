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
//
// Concurrency: reads are lock-free (atomic.Value). Hot-reloaded via SHA-256
// (the underlying configwatcher.Watcher dedups disk reads on each tick).
package policy

import (
	"fmt"
	"log/slog"
	"path/filepath"

	"github.com/vencil/tenant-api/internal/configwatcher"
	"gopkg.in/yaml.v3"
)

// Constraints defines the constraints for a domain policy.
type Constraints struct {
	AllowedReceiverTypes   []string `yaml:"allowed_receiver_types"`
	ForbiddenReceiverTypes []string `yaml:"forbidden_receiver_types"`
	EnforceGroupBy         []string `yaml:"enforce_group_by"`
	MaxRepeatInterval      string   `yaml:"max_repeat_interval"`
	MinGroupWait           string   `yaml:"min_group_wait"`
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
	Message    string `json:"message"`
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
	return &cfg, nil
}

// CheckWrite validates a tenant config patch against applicable domain policies.
// Returns violations (empty if the write is allowed).
//
// tenantID: the target tenant.
// patch: the key-value pairs being written.
//
// Currently enforces:
//   - forbidden_receiver_types: rejects writes that set _routing.receiver.type to a forbidden value.
//   - allowed_receiver_types: rejects writes that set _routing.receiver.type to a non-allowed value.
func (m *Manager) CheckWrite(tenantID string, patch map[string]string) []Violation {
	cfg := m.Get()
	if len(cfg.DomainPolicies) == 0 {
		return nil
	}

	var violations []Violation

	for domainName, dp := range cfg.DomainPolicies {
		if !isTenantInPolicy(dp.Tenants, tenantID) {
			continue
		}

		// Check receiver type constraints
		if receiverType, ok := patch["_routing_receiver_type"]; ok {
			violations = append(violations, checkReceiverType(domainName, dp.Constraints, receiverType)...)
		}
		// Also check nested routing patch format
		if receiverType, ok := patch["_routing.receiver.type"]; ok {
			violations = append(violations, checkReceiverType(domainName, dp.Constraints, receiverType)...)
		}
	}

	return violations
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

func checkReceiverType(domain string, c Constraints, receiverType string) []Violation {
	var violations []Violation

	// Check forbidden list first (takes precedence)
	for _, forbidden := range c.ForbiddenReceiverTypes {
		if receiverType == forbidden {
			violations = append(violations, Violation{
				Domain:     domain,
				Constraint: "forbidden_receiver_types",
				Message:    fmt.Sprintf("receiver type '%s' is forbidden by domain policy '%s'", receiverType, domain),
			})
			// If forbidden, don't check allowed list - forbidden takes precedence
			return violations
		}
	}

	// Check allowed list only if not forbidden (if specified, receiver must be in the list)
	if len(c.AllowedReceiverTypes) > 0 {
		allowed := false
		for _, a := range c.AllowedReceiverTypes {
			if receiverType == a {
				allowed = true
				break
			}
		}
		if !allowed {
			violations = append(violations, Violation{
				Domain:     domain,
				Constraint: "allowed_receiver_types",
				Message:    fmt.Sprintf("receiver type '%s' is not in the allowed list for domain policy '%s'", receiverType, domain),
			})
		}
	}

	return violations
}
