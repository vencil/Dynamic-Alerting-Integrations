package guard

// routing_policy_test.go — the #2280 additions to the routing checks: ADR-007
// `routes` entries, domain policies on every rendered receiver type, unknown
// routing profiles and unusable platform files. Cross-language parity of the
// same rules lives in cmd/da-guard/routing_policy_parity_test.go.

import (
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
	"gopkg.in/yaml.v3"
)

func routingFrom(t *testing.T, src string) map[string]any {
	t.Helper()
	var m map[string]any
	if err := yaml.Unmarshal([]byte(src), &m); err != nil {
		t.Fatal(err)
	}
	return m
}

// kindsByField is "field=kind" for every finding, sorted.
func kindsByField(fs []Finding) []string {
	out := make([]string, 0, len(fs))
	for _, f := range fs {
		out = append(out, f.Field+"="+string(f.Kind))
	}
	sort.Strings(out)
	return out
}

const pdMain = "receiver: {type: pagerduty, service_key: k}\n"

func TestRoutes_EntryShapes(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name string
		src  string
		want []string
	}{
		{"routes checked without any overrides (was behind the overrides early return)",
			pdMain + "routes: [{match: {severity: critical}, receiver: {type: bogus, url: 'https://x.example/h'}}]\n",
			[]string{"routes[0].receiver.type=unknown_receiver_type"}},
		{"valid route", pdMain + "routes: [{match: {severity: critical}, receiver: {type: slack, api_url: 'https://hooks.slack.com/x'}}]\n",
			[]string{}},
		{"not a list", pdMain + "routes: nope\n", []string{"routes=invalid_route_entry"}},
		{"null is absent", pdMain + "routes: ~\n", []string{}},
		{"entry not a mapping", pdMain + "routes: [x]\n", []string{"routes[0]=invalid_route_entry"}},
		{"continue", pdMain + "routes: [{match: {a: b}, continue: true, receiver: {type: slack}}]\n",
			[]string{"routes[0]=invalid_route_entry"}},
		{"match_re", pdMain + "routes: [{match_re: {a: b}, receiver: {type: slack}}]\n", []string{"routes[0]=invalid_route_entry"}},
		{"empty match", pdMain + "routes: [{match: {}, receiver: {type: slack}}]\n", []string{"routes[0]=invalid_route_entry"}},
		{"non-string value", pdMain + "routes: [{match: {a: 1}, receiver: {type: slack}}]\n", []string{"routes[0]=invalid_route_entry"}},
		{"empty value", pdMain + "routes: [{match: {a: ''}, receiver: {type: slack}}]\n", []string{"routes[0]=invalid_route_entry"}},
		{"bad label", pdMain + "routes: [{match: {bad-label: x}, receiver: {type: slack}}]\n", []string{"routes[0]=invalid_route_entry"}},
		{"renderable entry without receiver", pdMain + "routes: [{match: {a: b}}]\n",
			[]string{"routes[0].receiver=missing_receiver_field"}},
		{"receiver field format", pdMain + "routes: [{match: {a: b}, receiver: {type: webhook, url: 'not a url'}}]\n",
			[]string{"routes[0].receiver.url=invalid_receiver_field"}},
		{"duplicate match is not reported", pdMain +
			"routes: [{match: {a: b}, receiver: {type: email, to: [x@example.com], smarthost: 'smtp.example.com:25', from: a@example.com}}, " +
			"{match: {a: b}, receiver: {type: email, to: [x@example.com], smarthost: 'smtp.example.com:25', from: a@example.com}}]\n",
			[]string{}},
	}
	for _, tc := range cases {
		got := kindsByField(checkOneTenantRouting("t-routes", routingFrom(t, tc.src)))
		if !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s: findings %v, want %v", tc.name, got, tc.want)
		}
	}
}

func TestDomainPolicies_EveryTargetBothConstraints(t *testing.T) {
	t.Parallel()
	routing := routingFrom(t, `
receiver: {type: slack, api_url: 'https://hooks.slack.com/x'}
overrides: [{alertname: A, receiver: {type: webhook, url: 'https://x.example/h'}}]
routes: [{match: {severity: critical}, receiver: {type: pagerduty, service_key: k}}]
`)
	policies := []routingpolicy.Policy{
		{Domain: "finance", Tenants: []string{"t-pol"}, ForbiddenReceiverTypes: []string{"slack", "webhook"},
			AllowedReceiverTypes: []string{"email"}},
		{Domain: "other", Tenants: []string{"t-else"}, ForbiddenReceiverTypes: []string{"pagerduty"}},
	}
	prov := routingpolicy.Provenance{"receiver": "profile:team-a", "overrides": routingpolicy.SourceTenant,
		"routes": "profile:team-a"}
	got := checkDomainPolicies("t-pol", routing, policies, prov)
	want := []string{
		"overrides[0].receiver.type=domain_policy_violation",
		"overrides[0].receiver.type=domain_policy_violation",
		"receiver.type=domain_policy_violation",
		"receiver.type=domain_policy_violation",
		"routes[0].receiver.type=domain_policy_violation",
	}
	if g := kindsByField(got); !reflect.DeepEqual(g, want) {
		t.Fatalf("findings %v\nwant %v", g, want)
	}
	for _, f := range got {
		if f.Severity != SeverityError || !strings.Contains(f.Message, `"finance"`) {
			t.Errorf("finding %+v: want an error naming the domain", f)
		}
	}
	if !strings.Contains(got[0].Message, "routing profile 'team-a'") || !strings.Contains(got[0].Message, "forbidden_receiver_types") {
		t.Errorf("main receiver message must name the constraint and the profile it came from: %q", got[0].Message)
	}
	if !strings.Contains(got[2].Message, "the tenant's _routing") {
		t.Errorf("override message must name the tenant's _routing: %q", got[2].Message)
	}
	if checkDomainPolicies("t-pol", routing, nil, prov) != nil {
		t.Error("no policies: no findings")
	}
	// No main receiver: the generator renders nothing, so nothing is judged.
	if f := checkDomainPolicies("t-pol", routingFrom(t, "overrides: [{alertname: A, receiver: {type: slack}}]\n"), policies, nil); f != nil {
		t.Errorf("no main receiver: got %v", kindsByField(f))
	}
}

func TestRoutingGuardrails_PlatformProblemsAndUnknownProfile(t *testing.T) {
	t.Parallel()
	r, err := CheckDefaultsImpact(CheckInput{
		EffectiveConfigs: map[string]map[string]any{"t-a": {"x": 1}, "t-b": {"x": 1}},
		// No tenant has routing: the platform findings must not depend on it.
		UnknownRoutingProfiles: map[string]string{"t-b": "team-missing"},
		PlatformProblems: []routingpolicy.Problem{
			{Kind: routingpolicy.ProblemDomainPolicyUnusable, File: "_domain_policy.yaml",
				Field: "domain_policies.finance.tenants", Message: "tenants must be a list"},
			{Kind: routingpolicy.ProblemRoutingProfilesUnusable, File: "_routing_profiles.yaml",
				Field: "routing_profiles", Message: "must be a mapping"},
			{Kind: routingpolicy.ProblemRoutingDefaultsRoutes, File: "_defaults.yaml",
				Field: "_routing_defaults.routes", Message: "routes ignored"},
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	var got []string
	for _, f := range r.Findings {
		got = append(got, string(f.Severity)+"/"+string(f.Kind)+"/"+f.TenantID+"/"+f.Field)
	}
	want := []string{
		"error/routing_defaults_routes_ignored//_defaults.yaml:_routing_defaults.routes",
		"error/domain_policy_unusable//_domain_policy.yaml:domain_policies.finance.tenants",
		"warn/routing_profiles_unusable//_routing_profiles.yaml:routing_profiles",
		"warn/unknown_routing_profile/t-b/_routing_profile",
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("findings %v\nwant %v", got, want)
	}
	// A platform finding blocks the run but belongs to no tenant.
	if r.Summary.Errors != 2 || r.Summary.PassedTenantCount != 2 {
		t.Errorf("summary = %+v", r.Summary)
	}
}
