package policy

import (
	"bytes"
	"fmt"
	"log/slog"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
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
	t.Parallel()
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
	t.Parallel()
	// When policy file exists with valid content, manager should load it
	dir, _ := testutil.MkTempYAML(t, "_domain_policy.yaml", sampleDomainPolicyYAML)

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
	t.Parallel()
	// When no policies exist, all writes should be allowed
	m := NewForTest(&DomainPolicyConfig{DomainPolicies: make(map[string]DomainPolicy)})

	patch := map[string]string{
		"_routing_receiver_type": "slack",
	}

	violations := m.CheckWrite("db-a", patch)
	if len(violations) != 0 {
		t.Errorf("expected no violations with empty policies, got %d", len(violations))
	}
}

func TestCheckWrite_ForbiddenReceiver(t *testing.T) {
	t.Parallel()
	// When tenant is in a policy and receiver type is forbidden, write should be rejected
	m := NewForTest(&DomainPolicyConfig{
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
	t.Parallel()
	// When tenant is in a policy with allowed_receiver_types list, allowed type should pass
	m := NewForTest(&DomainPolicyConfig{
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
	t.Parallel()
	// When receiver type is not in the allowed list, write should be rejected
	m := NewForTest(&DomainPolicyConfig{
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
	t.Parallel()
	// When tenant is not in any policy, no violations should be returned
	m := NewForTest(&DomainPolicyConfig{
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
	t.Parallel()
	// #2280: the nested `_routing.receiver.type` key is no longer CheckWrite's
	// — a PUT body's routing goes through CheckTenantRouting, which judges
	// the resolved routing (every receiver) instead of one flattened key.
	// CheckWrite must stay silent on it so a PUT is not reported twice.
	m := NewForTest(&DomainPolicyConfig{
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

	if v := m.CheckWrite("db-a", map[string]string{"_routing.receiver.type": "slack"}); len(v) != 0 {
		t.Fatalf("CheckWrite must not judge the nested key any more, got %+v", v)
	}
	block := map[string]any{"_routing": map[string]any{"receiver": map[string]any{"type": "slack"}}}
	v := m.CheckTenantRouting("db-a", block, routingpolicy.Layers{})
	if len(v) != 1 || v[0].Target != "receiver" || v[0].Constraint != "forbidden_receiver_types" {
		t.Fatalf("CheckTenantRouting = %+v, want one forbidden violation on the main receiver", v)
	}
}

func TestCheckWrite_BothForbiddenAndAllowed(t *testing.T) {
	t.Parallel()
	// When both allowed and forbidden lists are set, both constraints should be checked
	m := NewForTest(&DomainPolicyConfig{
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

	// Test: type in forbidden list — and so also not in the allowed list.
	// #2280: the two constraints are independent (the route generator's
	// semantics), so this is TWO violations; before, forbidden returned early.
	patch2 := map[string]string{"_routing_receiver_type": "slack"}
	violations2 := m.CheckWrite("db-a", patch2)
	if len(violations2) != 2 {
		t.Fatalf("expected 2 violations for a forbidden type outside the allowed list, got %d", len(violations2))
	}
	if violations2[0].Constraint != "forbidden_receiver_types" || violations2[1].Constraint != "allowed_receiver_types" {
		t.Errorf("expected forbidden then allowed, got %q, %q", violations2[0].Constraint, violations2[1].Constraint)
	}

	// Test: type that's both in allowed and not forbidden (no violations)
	patch3 := map[string]string{"_routing_receiver_type": "pagerduty"}
	violations3 := m.CheckWrite("db-a", patch3)
	if len(violations3) != 0 {
		t.Errorf("expected no violations for allowed type, got %d", len(violations3))
	}
}

func TestCheckWrite_NoReceiverTypeInPatch(t *testing.T) {
	t.Parallel()
	// When patch doesn't contain receiver type, no violations should occur
	m := NewForTest(&DomainPolicyConfig{
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
		"_routing_repeat":     "1h",
	}

	violations := m.CheckWrite("db-a", patch)
	if len(violations) != 0 {
		t.Errorf("expected no violations when receiver type not in patch, got %d", len(violations))
	}
}

func TestPolicyForTenant(t *testing.T) {
	t.Parallel()
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

	m := NewForTest(&DomainPolicyConfig{
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
	t.Parallel()
	// Test that multiple violations are reported when receiver type violates multiple constraints
	m := NewForTest(&DomainPolicyConfig{
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
	// #2280: forbidden and allowed are judged independently — both fire.
	if len(violations) != 2 {
		t.Fatalf("expected 2 violations for slack (forbidden AND not allowed), got %d: %v", len(violations), violations)
	}
	if violations[0].Constraint != "forbidden_receiver_types" || violations[1].Constraint != "allowed_receiver_types" {
		t.Errorf("expected forbidden then allowed, got %+v", violations)
	}
}

func TestCheckWrite_EmptyConstraints(t *testing.T) {
	t.Parallel()
	// Test that policy with empty constraints doesn't restrict writes
	m := NewForTest(&DomainPolicyConfig{
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
	t.Parallel()
	// When policy file contains invalid YAML, NewManager should handle gracefully
	dir, _ := testutil.MkTempYAML(t, "_domain_policy.yaml", "{{invalid yaml")

	m := NewManager(dir)

	// Manager should still be usable with empty config
	cfg := m.Get()
	if cfg == nil || cfg.DomainPolicies == nil {
		t.Error("Get() should return valid (possibly empty) config after invalid YAML")
	}
}

func TestWatchLoop(t *testing.T) {
	t.Parallel()
	// Test that policy changes are picked up by watch loop
	// Start with empty policy file
	dir, _ := testutil.MkTempYAML(t, "_domain_policy.yaml", "domain_policies: {}")

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
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", newPolicy)

	// TRK-224: replace blind 200ms sleep with poll-until-loaded. The 100ms
	// WatchLoop tick + 200ms sleep was tight on slow CI. Now we poll for
	// up to 2s with 5ms granularity.
	deadline := time.NewTimer(2 * time.Second)
	defer deadline.Stop()
	tick := time.NewTicker(5 * time.Millisecond)
	defer tick.Stop()
waitLoaded:
	for {
		select {
		case <-deadline.C:
			t.Fatalf("WatchLoop did not pick up policy update within 2s")
		case <-tick.C:
			if cfg := m.Get(); len(cfg.DomainPolicies) == 1 {
				if _, ok := cfg.DomainPolicies["test"]; ok {
					break waitLoaded
				}
			}
		}
	}
	close(stopCh)

	// Final verification — equivalent to old test, but redundant after
	// the wait loop. Keep for explicit failure messaging.
	cfg := m.Get()
	if len(cfg.DomainPolicies) != 1 {
		t.Errorf("after update, expected 1 policy, got %d", len(cfg.DomainPolicies))
	}
	if _, ok := cfg.DomainPolicies["test"]; !ok {
		t.Error("test policy not found after update")
	}
}

func TestCheckTenantRouting_ResolvedRoutingEveryReceiver(t *testing.T) {
	t.Parallel()
	m := NewForTest(&DomainPolicyConfig{
		DomainPolicies: map[string]DomainPolicy{
			"finance": {
				Tenants: []string{"t-fin"},
				Constraints: Constraints{
					ForbiddenReceiverTypes: []string{"slack"},
					AllowedReceiverTypes:   []string{"pagerduty", "email"},
				},
			},
			"ops": {
				Tenants:     []string{"t-other"},
				Constraints: Constraints{ForbiddenReceiverTypes: []string{"pagerduty"}},
			},
		},
	})
	layers := routingpolicy.Layers{
		Defaults: map[string]any{"group_wait": "30s"},
		Profiles: map[string]map[string]any{
			"team-esc": {
				"receiver": map[string]any{"type": "pagerduty", "service_key": "k"},
				"routes": []any{map[string]any{
					"match":    map[string]any{"severity": "critical"},
					"receiver": map[string]any{"type": "slack", "api_url": "https://hooks.slack.com/x"},
				}},
			},
		},
	}
	cases := []struct {
		name  string
		block map[string]any
		want  []string // target/constraint
	}{
		{"profile route is judged", map[string]any{"_routing_profile": "team-esc"},
			[]string{"routes[0]/forbidden_receiver_types", "routes[0]/allowed_receiver_types"}},
		{"tenant routes: [] drops the profile's routes", map[string]any{"_routing_profile": "team-esc",
			"_routing": map[string]any{"routes": []any{}}}, nil},
		{"override is judged", map[string]any{"_routing": map[string]any{
			"receiver":  map[string]any{"type": "email"},
			"overrides": []any{map[string]any{"alertname": "A", "receiver": map[string]any{"type": "webhook"}}}}},
			[]string{"overrides[0]/allowed_receiver_types"}},
		{"disabled routing is not judged", map[string]any{"_routing_profile": "team-esc", "_routing": "disable"}, nil},
		{"no main receiver: nothing renders", map[string]any{"_routing": map[string]any{
			"overrides": []any{map[string]any{"alertname": "A", "receiver": map[string]any{"type": "slack"}}}}}, nil},
	}
	for _, tc := range cases {
		var got []string
		for _, v := range m.CheckTenantRouting("t-fin", tc.block, layers) {
			got = append(got, v.Target+"/"+v.Constraint)
		}
		if strings.Join(got, ",") != strings.Join(tc.want, ",") {
			t.Errorf("%s: violations %v, want %v", tc.name, got, tc.want)
		}
	}
	v := m.CheckTenantRouting("t-fin", map[string]any{"_routing_profile": "team-esc"}, layers)
	if len(v) == 0 || !strings.Contains(v[0].Message, "routes[0] (from routing profile 'team-esc')") {
		t.Errorf("message must name the route and the profile it came from: %+v", v)
	}
	if got := m.CheckTenantRouting("t-nobody", map[string]any{"_routing_profile": "team-esc"}, layers); got != nil {
		t.Errorf("tenant in no policy: %+v", got)
	}
}

// #2325: require_critical_escalation — non-compliance is a violation, a leak
// an advisory; a non-boolean value neither fails the load nor turns it on.
func TestJudgeTenantRouting_RequireCriticalEscalation(t *testing.T) {
	t.Parallel()
	cfg, err := parseConfig([]byte("domain_policies:\n" +
		"  esc:\n    tenants: [t-esc]\n    constraints:\n      require_critical_escalation: true\n" +
		"  quoted:\n    tenants: [t-esc]\n    constraints:\n      require_critical_escalation: \"true\"\n"))
	if err != nil {
		t.Fatalf("a non-boolean value must not fail the whole file: %v", err)
	}
	m := NewForTest(cfg)
	for _, p := range m.RoutingPolicies() {
		if p.RequireCriticalEscalation != (p.Domain == "esc") {
			t.Errorf("%s: RequireCriticalEscalation = %v", p.Domain, p.RequireCriticalEscalation)
		}
	}
	slack := map[string]any{"type": "slack", "api_url": "https://hooks.slack.com/x"}
	pd := map[string]any{"type": "pagerduty", "service_key": "k"}

	v, adv := m.JudgeTenantRouting("t-esc", map[string]any{"_routing": map[string]any{"receiver": slack}}, routingpolicy.Layers{})
	if len(v) != 1 || v[0].Constraint != "require_critical_escalation" || v[0].Target != "receiver" || adv != nil {
		t.Errorf("no escalation: violations %+v, advisories %v", v, adv)
	}
	v, adv = m.JudgeTenantRouting("t-esc", map[string]any{"_routing": map[string]any{
		"receiver": slack,
		"routes": []any{
			map[string]any{"match": map[string]any{"team": "app"}, "receiver": slack},
			map[string]any{"match": map[string]any{"severity": "critical"}, "receiver": pd},
		}}}, routingpolicy.Layers{})
	if len(v) != 0 || len(adv) != 1 || !strings.HasPrefix(adv[0], "tenant=t-esc: domain policy 'esc': routes[0] (team=app)") ||
		!strings.Contains(adv[0], "severity=critical, team=app") {
		t.Errorf("leak: violations %+v, advisories %v", v, adv)
	}
}

// #2325: hot reload of a require_critical_escalation value. One PyYAML
// refuses (`!!bool y`, `!!int abc`) makes the route generator drop the whole
// file, so it fails the reload here: the last good policy stays in effect and
// the reload failure is recorded. One PyYAML reads but is not a boolean
// (`!!int 5`) loads, with only that constraint off (and a WARN), as the
// generator does.
const escalationPolicyTmpl = "domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n" +
	"      require_critical_escalation: %s\n      forbidden_receiver_types: [slack]\n"

type reloadOutcomes struct {
	mu  sync.Mutex
	oks []bool
}

func (r *reloadOutcomes) RecordReload(_ string, ok bool) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.oks = append(r.oks, ok)
}

func (r *reloadOutcomes) last() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.oks) > 0 && r.oks[len(r.oks)-1]
}

func TestReload_RefusedEscalationValueKeepsLastGood(t *testing.T) {
	t.Parallel()
	for _, v := range []string{"!!bool y", "!!int abc", "!!float x", "!!timestamp nope", "!!bool 1",
		// Collections PyYAML refuses for their own tag or direct children.
		"!!omap [1]", "!!pairs [a]", "{<<: 1}", "!!bool [true]", "!!bool {a: 1}", "!!str [1]",
		"!!int {a: 1}", "!foo [1]", "!!seq {a: 1}", "!!map [1]", "{[1]: 2}", "!!timestamp [1]",
		"!!binary [1]", "{? [1] : 2}"} {
		dir, _ := testutil.MkTempYAML(t, "_domain_policy.yaml", fmt.Sprintf(escalationPolicyTmpl, "true"))
		m := NewManager(dir)
		obs := &reloadOutcomes{}
		m.SetReloadObserver(obs)
		testutil.WriteYAML(t, dir, "_domain_policy.yaml", fmt.Sprintf(escalationPolicyTmpl, v))
		if err := m.Reload(); err == nil {
			t.Errorf("%s: Reload() = nil, want the error PyYAML's refusal is", v)
		}
		if obs.last() {
			t.Errorf("%s: reload recorded as successful, want a failure", v)
		}
		pols := m.RoutingPolicies()
		if len(pols) != 1 || !pols[0].RequireCriticalEscalation ||
			!reflect.DeepEqual(pols[0].ForbiddenReceiverTypes, []string{"slack"}) {
			t.Errorf("%s: policies %+v, want the last good one (escalation on, slack forbidden)", v, pols)
		}
	}
}

// A mapping or sequence value is not a boolean (#2325), however it is built:
// an alias cycle or fan-out must neither crash the process (a CrashLoop, on
// the first load and on a hot reload alike) nor stall it, and as for any
// other non-boolean the constraint is off while the rest still applies.
// Accepted gap: nothing below the direct children is looked at, so a value
// PyYAML refuses only there (`[!!bool y]`) also loads with the constraint
// off — on a hot reload, from last-good on to off — while the generator and
// da-guard refuse it.
func TestLoad_CollectionEscalationValueIsNonBoolean(t *testing.T) {
	t.Parallel()
	fan := "&l0 [x,x,x,x,x,x,x,x,x,x]"
	for i := 1; i <= 8; i++ {
		fan += fmt.Sprintf(", &l%d [%s]", i, strings.TrimSuffix(strings.Repeat(fmt.Sprintf("*l%d,", i-1), 10), ","))
	}
	want := func(what, v string, m *Manager, took time.Duration) {
		t.Helper()
		if took > time.Second {
			t.Errorf("%s %.40s: took %v", what, v, took)
		}
		pols := m.RoutingPolicies()
		if len(pols) != 1 || pols[0].RequireCriticalEscalation ||
			!reflect.DeepEqual(pols[0].ForbiddenReceiverTypes, []string{"slack"}) {
			t.Errorf("%s %.40s: policies %+v, want escalation off and slack still forbidden", what, v, pols)
		}
	}
	for _, v := range []string{"&x [*x]", "&x {b: *x}", "[" + fan + "]", "!!omap [{[1]: 2}]", "{<<: !foo {b: 1}}",
		"[true]", "!!set {a: null}", "!!omap [{a: 1}]", "!!pairs [{a: 1}]", "{<<: {b: 1}}",
		// the accepted gap: PyYAML refuses these below the direct children
		"[!!bool y]", "{a: !!int x}", "!!set {!!bool y: null}", "!!omap [{a: !!bool y}]",
		"{<<: [{b: !!bool y}]}", "[[!!bool y]]", "{b: 2001-13-40}", "[2001-13-40]"} {
		start := time.Now()
		dir, _ := testutil.MkTempYAML(t, "_domain_policy.yaml", fmt.Sprintf(escalationPolicyTmpl, v))
		want("initial load", v, NewManager(dir), time.Since(start))

		dir, _ = testutil.MkTempYAML(t, "_domain_policy.yaml", fmt.Sprintf(escalationPolicyTmpl, "true"))
		m := NewManager(dir)
		testutil.WriteYAML(t, dir, "_domain_policy.yaml", fmt.Sprintf(escalationPolicyTmpl, v))
		start = time.Now()
		if err := m.Reload(); err != nil {
			t.Errorf("hot reload %.40s: Reload() = %v, want a non-boolean to load", v, err)
		}
		want("hot reload", v, m, time.Since(start))
	}
}

// A `!!null`-tagged value (#2325), on the first load and on a hot reload
// from a good policy with escalation on. PyYAML reads a tagged scalar as
// None: the file loads, escalation is off and slack is still forbidden (as
// the generator enforces it). It refuses a tagged collection and the
// generator drops the file: the first load fails (the empty policy), and a
// hot reload fails and keeps the last good policy.
func TestLoad_TaggedNullEscalationValue(t *testing.T) {
	t.Parallel()
	for v, refused := range map[string]bool{
		"!!null x": false, "!<tag:yaml.org,2002:null> x": false, `!!null ""`: false,
		"!!null {}": true, "!!null [1]": true,
	} {
		dir, _ := testutil.MkTempYAML(t, "_domain_policy.yaml", fmt.Sprintf(escalationPolicyTmpl, v))
		pols := NewManager(dir).RoutingPolicies()
		if refused && len(pols) != 0 {
			t.Errorf("initial load %s: policies %+v, want none (the file refused)", v, pols)
		}
		if !refused && (len(pols) != 1 || pols[0].RequireCriticalEscalation ||
			!reflect.DeepEqual(pols[0].ForbiddenReceiverTypes, []string{"slack"})) {
			t.Errorf("initial load %s: policies %+v, want escalation off and slack forbidden", v, pols)
		}

		dir, _ = testutil.MkTempYAML(t, "_domain_policy.yaml", fmt.Sprintf(escalationPolicyTmpl, "true"))
		m := NewManager(dir)
		testutil.WriteYAML(t, dir, "_domain_policy.yaml", fmt.Sprintf(escalationPolicyTmpl, v))
		err := m.Reload()
		if (err != nil) != refused {
			t.Errorf("hot reload %s: Reload() = %v, want refused=%v", v, err, refused)
		}
		pols = m.RoutingPolicies()
		if len(pols) != 1 || pols[0].RequireCriticalEscalation != refused ||
			!reflect.DeepEqual(pols[0].ForbiddenReceiverTypes, []string{"slack"}) {
			t.Errorf("hot reload %s: policies %+v, want escalation=%v (last good kept iff refused), slack forbidden",
				v, pols, refused)
		}
	}
}

// Not parallel: it swaps the process-wide slog default to read the WARN.
func TestReload_NonBooleanEscalationValueLoadsWithWarn(t *testing.T) {
	var buf lockedBuffer
	orig := slog.Default()
	defer slog.SetDefault(orig)
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))

	dir, _ := testutil.MkTempYAML(t, "_domain_policy.yaml", fmt.Sprintf(escalationPolicyTmpl, "true"))
	m := NewManager(dir)
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", fmt.Sprintf(escalationPolicyTmpl, "!!int 5"))
	if err := m.Reload(); err != nil {
		t.Fatalf("Reload(): %v, want a value PyYAML reads (int 5) to load", err)
	}
	pols := m.RoutingPolicies()
	if len(pols) != 1 || pols[0].RequireCriticalEscalation ||
		!reflect.DeepEqual(pols[0].ForbiddenReceiverTypes, []string{"slack"}) {
		t.Errorf("policies %+v, want escalation off and slack still forbidden", pols)
	}
	if out := buf.String(); !strings.Contains(out, "require_critical_escalation is not a boolean") ||
		!strings.Contains(out, "domain=fin") || !strings.Contains(out, "value=5") {
		t.Errorf("log %q, want the non-boolean WARN for domain fin", out)
	}
}

type lockedBuffer struct {
	mu sync.Mutex
	b  bytes.Buffer
}

func (l *lockedBuffer) Write(p []byte) (int, error) {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.Write(p)
}

func (l *lockedBuffer) String() string {
	l.mu.Lock()
	defer l.mu.Unlock()
	return l.b.String()
}
