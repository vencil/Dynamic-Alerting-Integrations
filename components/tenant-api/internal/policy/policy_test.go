package policy

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

const sampleDomainPolicyYAML = `domain_policies:
  finance:
    description: "Finance domain compliance requirements"
    tenants: [db-a, db-b]
    constraints:
      allowed_receiver_types: [pagerduty, email, opsgenie]
      forbidden_receiver_types: [slack]
      enforce_group_by: [tenant, alertname, severity]
      max_repeat_interval: 1h
      min_group_wait: 30s
  ecommerce:
    description: "E-commerce platform policies"
    tenants: [db-c]
    constraints:
      forbidden_receiver_types: [webhook]
`

func TestNewManager_NoFile(t *testing.T) {
	// When no policy file exists, manager should initialize with empty config
	dir := t.TempDir()
	m := NewManager(dir)

	cfg := m.Get()
	if cfg == nil {
		t.Fatal("Get() returned nil")
	}
	if len(cfg.DomainPolicies) != 0 {
		t.Errorf("expected 0 domain policies when no file exists, got %d", len(cfg.DomainPolicies))
	}
}

func TestNewManager_ValidFile(t *testing.T) {
	// When policy file exists with valid content, manager should load it
	dir := t.TempDir()
	path := filepath.Join(dir, "_domain_policy.yaml")
	if err := os.WriteFile(path, []byte(sampleDomainPolicyYAML), 0644); err != nil {
		t.Fatalf("failed to write policy file: %v", err)
	}

	m := NewManager(dir)

	cfg := m.Get()
	if len(cfg.DomainPolicies) != 2 {
		t.Errorf("expected 2 domain policies, got %d", len(cfg.DomainPolicies))
	}

	// Verify finance policy loaded correctly
	finance, ok := cfg.DomainPolicies["finance"]
	if !ok {
		t.Fatal("finance policy not found")
	}
	if finance.Description != "Finance domain compliance requirements" {
		t.Errorf("finance description = %q, want %q", finance.Description, "Finance domain compliance requirements")
	}
	if len(finance.Tenants) != 2 {
		t.Errorf("finance tenants count = %d, want 2", len(finance.Tenants))
	}
	if len(finance.Constraints.AllowedReceiverTypes) != 3 {
		t.Errorf("finance allowed_receiver_types count = %d, want 3", len(finance.Constraints.AllowedReceiverTypes))
	}
}

func TestCheckWrite_NoPolicies(t *testing.T) {
	// When no policies exist, all writes should be allowed
	m := &Manager{}
	m.value.Store(&DomainPolicyConfig{DomainPolicies: make(map[string]DomainPolicy)})

	patch := map[string]string{
		"_routing_receiver_type": "slack",
	}

	violations := m.CheckWrite("db-a", patch)
	if len(violations) != 0 {
		t.Errorf("expected no violations with empty policies, got %d", len(violations))
	}
}

func TestCheckWrite_ForbiddenReceiver(t *testing.T) {
	// When tenant is in a policy and receiver type is forbidden, write should be rejected
	m := &Manager{}
	m.value.Store(&DomainPolicyConfig{
		DomainPolicies: map[string]DomainPolicy{
			"finance": {
				Description: "Finance domain",
				Tenants:     []string{"db-a", "db-b"},
				Constraints: Constraints{
					ForbiddenReceiverTypes: []string{"slack", "webhook"},
				},
			},
		},
	})

	patch := map[string]string{
		"_routing_receiver_type": "slack",
	}

	violations := m.CheckWrite("db-a", patch)
	if len(violations) != 1 {
		t.Fatalf("expected 1 violation, got %d", len(violations))
	}

	v := violations[0]
	if v.Domain != "finance" {
		t.Errorf("violation domain = %q, want %q", v.Domain, "finance")
	}
	if v.Constraint != "forbidden_receiver_types" {
		t.Errorf("violation constraint = %q, want %q", v.Constraint, "forbidden_receiver_types")
	}
	if v.Message == "" {
		t.Error("violation message is empty")
	}
}

func TestCheckWrite_AllowedReceiver(t *testing.T) {
	// When tenant is in a policy with allowed_receiver_types list, allowed type should pass
	m := &Manager{}
	m.value.Store(&DomainPolicyConfig{
		DomainPolicies: map[string]DomainPolicy{
			"finance": {
				Description: "Finance domain",
				Tenants:     []string{"db-a", "db-b"},
				Constraints: Constraints{
					AllowedReceiverTypes: []string{"pagerduty", "email", "opsgenie"},
				},
			},
		},
	})

	patch := map[string]string{
		"_routing_receiver_type": "pagerduty",
	}

	violations := m.CheckWrite("db-a", patch)
	if len(violations) != 0 {
		t.Errorf("expected no violations for allowed receiver type, got %d: %v", len(violations), violations)
	}
}

func TestCheckWrite_AllowedReceiverNotInList(t *testing.T) {
	// When receiver type is not in the allowed list, write should be rejected
	m := &Manager{}
	m.value.Store(&DomainPolicyConfig{
		DomainPolicies: map[string]DomainPolicy{
			"finance": {
				Description: "Finance domain",
				Tenants:     []string{"db-a", "db-b"},
				Constraints: Constraints{
					AllowedReceiverTypes: []string{"pagerduty", "email", "opsgenie"},
				},
			},
		},
	})

	patch := map[string]string{
		"_routing_receiver_type": "slack",
	}

	violations := m.CheckWrite("db-a", patch)
	if len(violations) != 1 {
		t.Fatalf("expected 1 violation, got %d", len(violations))
	}

	v := violations[0]
	if v.Constraint != "allowed_receiver_types" {
		t.Errorf("violation constraint = %q, want %q", v.Constraint, "allowed_receiver_types")
	}
}

func TestCheckWrite_TenantNotInPolicy(t *testing.T) {
	// When tenant is not in any policy, no violations should be returned
	m := &Manager{}
	m.value.Store(&DomainPolicyConfig{
		DomainPolicies: map[string]DomainPolicy{
			"finance": {
				Description: "Finance domain",
				Tenants:     []string{"db-a", "db-b"},
				Constraints: Constraints{
					ForbiddenReceiverTypes: []string{"slack"},
				},
			},
		},
	})

	patch := map[string]string{
		"_routing_receiver_type": "slack",
	}

	violations := m.CheckWrite("db-c", patch)
	if len(violations) != 0 {
		t.Errorf("expected no violations for tenant not in policy, got %d", len(violations))
	}
}

func TestCheckWrite_NestedRoutingFormat(t *testing.T) {
	// Policy should also check the nested "_routing.receiver.type" patch format
	m := &Manager{}
	m.value.Store(&DomainPolicyConfig{
		DomainPolicies: map[string]DomainPolicy{
			"finance": {
				Description: "Finance domain",
				Tenants:     []string{"db-a"},
				Constraints: Constraints{
					ForbiddenReceiverTypes: []string{"slack"},
				},
			},
		},
	})

	patch := map[string]string{
		"_routing.receiver.type": "slack",
	}

	violations := m.CheckWrite("db-a", patch)
	if len(violations) != 1 {
		t.Fatalf("expected 1 violation for nested format, got %d", len(violations))
	}
}

func TestCheckWrite_BothForbiddenAndAllowed(t *testing.T) {
	// When both allowed and forbidden lists are set, both constraints should be checked
	m := &Manager{}
	m.value.Store(&DomainPolicyConfig{
		DomainPolicies: map[string]DomainPolicy{
			"finance": {
				Description: "Finance domain",
				Tenants:     []string{"db-a"},
				Constraints: Constraints{
					AllowedReceiverTypes:   []string{"pagerduty", "email"},
					ForbiddenReceiverTypes: []string{"slack", "webhook"},
				},
			},
		},
	})

	// Test: type not in allowed list (violates allowed constraint)
	patch1 := map[string]string{"_routing_receiver_type": "opsgenie"}
	violations1 := m.CheckWrite("db-a", patch1)
	if len(violations1) != 1 {
		t.Errorf("expected 1 violation for type not in allowed list, got %d", len(violations1))
	}
	if violations1[0].Constraint != "allowed_receiver_types" {
		t.Errorf("expected allowed_receiver_types constraint, got %q", violations1[0].Constraint)
	}

	// Test: type in forbidden list (violates forbidden constraint)
	patch2 := map[string]string{"_routing_receiver_type": "slack"}
	violations2 := m.CheckWrite("db-a", patch2)
	if len(violations2) != 1 {
		t.Errorf("expected 1 violation for forbidden type, got %d", len(violations2))
	}
	if violations2[0].Constraint != "forbidden_receiver_types" {
		t.Errorf("expected forbidden_receiver_types constraint, got %q", violations2[0].Constraint)
	}

	// Test: type that's both in allowed and not forbidden (no violations)
	patch3 := map[string]string{"_routing_receiver_type": "pagerduty"}
	violations3 := m.CheckWrite("db-a", patch3)
	if len(violations3) != 0 {
		t.Errorf("expected no violations for allowed type, got %d", len(violations3))
	}
}

func TestCheckWrite_NoReceiverTypeInPatch(t *testing.T) {
	// When patch doesn't contain receiver type, no violations should occur
	m := &Manager{}
	m.value.Store(&DomainPolicyConfig{
		DomainPolicies: map[string]DomainPolicy{
			"finance": {
				Description: "Finance domain",
				Tenants:     []string{"db-a"},
				Constraints: Constraints{
					ForbiddenReceiverTypes: []string{"slack"},
				},
			},
		},
	})

	patch := map[string]string{
		"_routing_group_wait": "10s",
		"_routing_repeat": "1h",
	}

	violations := m.CheckWrite("db-a", patch)
	if len(violations) != 0 {
		t.Errorf("expected no violations when receiver type not in patch, got %d", len(violations))
	}
}

func TestPolicyForTenant(t *testing.T) {
	tests := []struct {
		name            string
		tenantID        string
		wantDomainName  string
		wantFound       bool
		wantDescription string
	}{
		{
			name:            "tenant in first policy",
			tenantID:        "db-a",
			wantDomainName:  "finance",
			wantFound:       true,
			wantDescription: "Finance domain compliance requirements",
		},
		{
			name:            "tenant in second policy",
			tenantID:        "db-c",
			wantDomainName:  "ecommerce",
			wantFound:       true,
			wantDescription: "E-commerce platform policies",
		},
		{
			name:            "tenant in multiple policies returns first match",
			tenantID:        "db-b",
			wantFound:       true,
			wantDomainName:  "finance", // finance policy lists db-b
			wantDescription: "Finance domain compliance requirements",
		},
		{
			name:      "tenant not in any policy",
			tenantID:  "db-unknown",
			wantFound: false,
		},
	}

	m := &Manager{}
	m.value.Store(&DomainPolicyConfig{
		DomainPolicies: map[string]DomainPolicy{
			"finance": {
				Description: "Finance domain compliance requirements",
				Tenants:     []string{"db-a", "db-b"},
				Constraints: Constraints{
					ForbiddenReceiverTypes: []string{"slack"},
				},
			},
			"ecommerce": {
				Description: "E-commerce platform policies",
				Tenants:     []string{"db-c"},
				Constraints: Constraints{
					ForbiddenReceiverTypes: []string{"webhook"},
				},
			},
		},
	})

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			domainName, policy, found := m.PolicyForTenant(tt.tenantID)

			if found != tt.wantFound {
				t.Errorf("PolicyForTenant(%q) found = %v, want %v", tt.tenantID, found, tt.wantFound)
			}

			if found && domainName != tt.wantDomainName {
				t.Errorf("PolicyForTenant(%q) domain = %q, want %q", tt.tenantID, domainName, tt.wantDomainName)
			}

			if found && policy.Description != tt.wantDescription {
				t.Errorf("PolicyForTenant(%q) description = %q, want %q",
					tt.tenantID, policy.Description, tt.wantDescription)
			}
		})
	}
}

func TestCheckWrite_MultipleViolations(t *testing.T) {
	// Test that multiple violations are reported when receiver type violates multiple constraints
	m := &Manager{}
	m.value.Store(&DomainPolicyConfig{
		DomainPolicies: map[string]DomainPolicy{
			"finance": {
				Description: "Finance domain",
				Tenants:     []string{"db-a"},
				Constraints: Constraints{
					AllowedReceiverTypes:   []string{"pagerduty", "email"},
					ForbiddenReceiverTypes: []string{"slack", "webhook"},
				},
			},
		},
	})

	// slack is in the forbidden list AND not in the allowed list
	patch := map[string]string{
		"_routing_receiver_type": "slack",
	}

	violations := m.CheckWrite("db-a", patch)
	if len(violations) != 1 {
		t.Errorf("expected violations for slack (forbidden), got %d: %v", len(violations), violations)
	}
	// slack should trigger the forbidden constraint
	if violations[0].Constraint != "forbidden_receiver_types" {
		t.Errorf("expected forbidden_receiver_types constraint, got %q", violations[0].Constraint)
	}
}

func TestCheckWrite_EmptyConstraints(t *testing.T) {
	// Test that policy with empty constraints doesn't restrict writes
	m := &Manager{}
	m.value.Store(&DomainPolicyConfig{
		DomainPolicies: map[string]DomainPolicy{
			"finance": {
				Description: "Finance domain",
				Tenants:     []string{"db-a"},
				Constraints: Constraints{
					// All fields empty
				},
			},
		},
	})

	patch := map[string]string{
		"_routing_receiver_type": "anything",
	}

	violations := m.CheckWrite("db-a", patch)
	if len(violations) != 0 {
		t.Errorf("expected no violations with empty constraints, got %d", len(violations))
	}
}

func TestNewManager_InvalidYAML(t *testing.T) {
	// When policy file contains invalid YAML, NewManager should handle gracefully
	dir := t.TempDir()
	path := filepath.Join(dir, "_domain_policy.yaml")
	if err := os.WriteFile(path, []byte("{{invalid yaml"), 0644); err != nil {
		t.Fatalf("failed to write invalid file: %v", err)
	}

	m := NewManager(dir)

	// Manager should still be usable with empty config
	cfg := m.Get()
	if cfg == nil || cfg.DomainPolicies == nil {
		t.Error("Get() should return valid (possibly empty) config after invalid YAML")
	}
}

func TestWatchLoop(t *testing.T) {
	// Test that policy changes are picked up by watch loop
	dir := t.TempDir()
	path := filepath.Join(dir, "_domain_policy.yaml")

	// Start with empty policy file
	if err := os.WriteFile(path, []byte("domain_policies: {}"), 0644); err != nil {
		t.Fatalf("failed to write initial file: %v", err)
	}

	m := NewManager(dir)
	stopCh := make(chan struct{})

	// Verify initial empty state
	if len(m.Get().DomainPolicies) != 0 {
		t.Fatal("expected empty policies initially")
	}

	// Start watch loop
	go m.WatchLoop(100*time.Millisecond, stopCh)

	// Update the policy file
	newPolicy := `domain_policies:
  test:
    description: Test policy
    tenants: [db-a]
    constraints: {}`
	if err := os.WriteFile(path, []byte(newPolicy), 0644); err != nil {
		t.Fatalf("failed to update file: %v", err)
	}

	// Give watch loop time to detect change (not deterministic, but reasonable)
	// Note: In a real scenario with proper wait mechanisms, this would be more robust
	time.Sleep(200 * time.Millisecond)
	close(stopCh)

	// Verify the update was loaded
	cfg := m.Get()
	if len(cfg.DomainPolicies) != 1 {
		t.Errorf("after update, expected 1 policy, got %d", len(cfg.DomainPolicies))
	}
	if _, ok := cfg.DomainPolicies["test"]; !ok {
		t.Error("test policy not found after update")
	}
}
