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
// Concurrency: reads are lock-free (atomic.Value). Hot-reloaded via SHA-256.
package policy

import (
	"crypto/sha256"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"sync/atomic"
	"time"

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

// Manager holds the hot-reloadable domain policy config.
type Manager struct {
	configDir string
	value     atomic.Value // stores *DomainPolicyConfig
	lastHash  string
}

// NewManager creates a Manager that reads _domain_policy.yaml from configDir.
func NewManager(configDir string) *Manager {
	m := &Manager{configDir: configDir}
	if err := m.load(); err != nil {
		log.Printf("WARN: policy: initial load: %v", err)
	}
	if m.value.Load() == nil {
		m.value.Store(&DomainPolicyConfig{DomainPolicies: make(map[string]DomainPolicy)})
	}
	return m
}

// Get returns the current policy config snapshot (lock-free).
func (m *Manager) Get() *DomainPolicyConfig {
	v := m.value.Load()
	if v == nil {
		return &DomainPolicyConfig{DomainPolicies: make(map[string]DomainPolicy)}
	}
	return v.(*DomainPolicyConfig)
}

// WatchLoop periodically checks for changes to the policy config file.
func (m *Manager) WatchLoop(interval time.Duration, stopCh <-chan struct{}) {
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-stopCh:
			return
		case <-ticker.C:
			if err := m.load(); err != nil {
				log.Printf("WARN: policy reload failed: %v", err)
			}
		}
	}
}

func (m *Manager) load() error {
	path := filepath.Join(m.configDir, "_domain_policy.yaml")
	data, err := os.ReadFile(path)
	if err != nil {
		if os.IsNotExist(err) {
			m.value.Store(&DomainPolicyConfig{DomainPolicies: make(map[string]DomainPolicy)})
			return nil
		}
		return fmt.Errorf("read %s: %w", path, err)
	}

	hash := fmt.Sprintf("%x", sha256.Sum256(data))
	if hash == m.lastHash {
		return nil
	}

	var cfg DomainPolicyConfig
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return fmt.Errorf("parse %s: %w", path, err)
	}
	if cfg.DomainPolicies == nil {
		cfg.DomainPolicies = make(map[string]DomainPolicy)
	}

	m.value.Store(&cfg)
	m.lastHash = hash
	log.Printf("policy: loaded %d domain policies from %s", len(cfg.DomainPolicies), path)
	return nil
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

	// Check forbidden list
	for _, forbidden := range c.ForbiddenReceiverTypes {
		if receiverType == forbidden {
			violations = append(violations, Violation{
				Domain:     domain,
				Constraint: "forbidden_receiver_types",
				Message:    fmt.Sprintf("receiver type '%s' is forbidden by domain policy '%s'", receiverType, domain),
			})
		}
	}

	// Check allowed list (if specified, receiver must be in the list)
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
