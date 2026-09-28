package routingpolicy

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

func decode(t *testing.T, src string) map[string]any {
	t.Helper()
	var m map[string]any
	if err := yaml.Unmarshal([]byte(src), &m); err != nil {
		t.Fatal(err)
	}
	return m
}

func writeRoot(t *testing.T, files map[string]string) string {
	t.Helper()
	dir := t.TempDir()
	for name, content := range files {
		p := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func TestResolve_ShallowLayersAndProvenance(t *testing.T) {
	t.Parallel()
	layers := Layers{
		Defaults: decode(t, "receiver: {type: email}\ngroup_wait: 10s\ngroup_by: [a]\n"),
		Profiles: map[string]map[string]any{
			"team-a": decode(t, "receiver: {type: slack, api_url: 'https://{{tenant}}.hooks.example/x'}\n"+
				"routes: [{match: {severity: critical}, receiver: {type: pagerduty, service_key: k}}]\n"),
		},
	}
	block := decode(t, "_routing_profile: ' team-a '\n_routing:\n  group_by: [b, '{{tenant}}']\n  routes: []\n")
	got, ok, prov, unknown := Resolve("t-one", block, layers)
	if !ok || unknown != "" {
		t.Fatalf("ok=%v unknown=%q", ok, unknown)
	}
	want := map[string]any{
		"receiver":   map[string]any{"type": "slack", "api_url": "https://t-one.hooks.example/x"},
		"group_wait": "10s",
		"group_by":   []any{"b", "t-one"},
		"routes":     []any{},
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("resolved = %#v\nwant %#v", got, want)
	}
	wantProv := Provenance{"receiver": "profile:team-a", "group_wait": SourceDefaults,
		"group_by": SourceTenant, "routes": SourceTenant}
	if !reflect.DeepEqual(prov, wantProv) {
		t.Errorf("provenance = %v, want %v", prov, wantProv)
	}
	// The result is a copy: writing it must not reach the shared layers.
	got["receiver"].(map[string]any)["type"] = "mutated"
	if layers.Profiles["team-a"]["receiver"].(map[string]any)["type"] != "slack" {
		t.Error("Resolve aliased the profile layer")
	}
	if !strings.Contains(Describe(prov["receiver"]), "routing profile 'team-a'") {
		t.Errorf("Describe = %q", Describe(prov["receiver"]))
	}
}

func TestResolve_NoRouting(t *testing.T) {
	t.Parallel()
	layers := Layers{Profiles: map[string]map[string]any{
		"chat":   decode(t, "receiver: {type: slack}\n"),
		"broken": nil, // a profile body that is not a mapping: known, empty
	}}
	cases := []struct {
		name        string
		block       string
		wantOK      bool
		wantUnknown string
	}{
		{"disable string beats the profile", "_routing_profile: chat\n_routing: ' Disabled '\n", false, ""},
		{"unknown profile, nothing else", "_routing_profile: nope\n", false, "nope"},
		{"unknown profile is reported even when disabled", "_routing_profile: nope\n_routing: off\n", false, "nope"},
		{"known but not a mapping", "_routing_profile: broken\n", false, ""},
		{"empty _routing mapping", "_routing: {}\n", false, ""},
		{"non-string profile reference is ignored", "_routing_profile: 7\n_routing: {receiver: {type: email}}\n", true, ""},
		{"non-disabling string _routing is ignored", "_routing_profile: chat\n_routing: yes-please\n", true, ""},
	}
	for _, tc := range cases {
		_, ok, _, unknown := Resolve("t-x", decode(t, tc.block), layers)
		if ok != tc.wantOK || unknown != tc.wantUnknown {
			t.Errorf("%s: ok=%v unknown=%q, want ok=%v unknown=%q", tc.name, ok, unknown, tc.wantOK, tc.wantUnknown)
		}
	}
}

func TestIsDisabled_MatchesTheExporter(t *testing.T) {
	t.Parallel()
	for _, s := range []string{"disable", "Disabled", " OFF ", "false", "no", "enable", "", "disabledx"} {
		want := config.IsDisabled(strings.ToLower(strings.TrimSpace(s)))
		if got := IsDisabled(s); got != want {
			t.Errorf("IsDisabled(%q) = %v, exporter says %v", s, got, want)
		}
	}
	if IsDisabled(false) || IsDisabled(nil) {
		t.Error("a non-string is never a disabling value")
	}
}

func TestRouteEntryProblem(t *testing.T) {
	t.Parallel()
	cases := []struct {
		src     string
		bad     bool
		mention string
	}{
		{"match: {severity: critical}\nreceiver: {type: slack}\n", false, ""},
		{"match: {severity: critical, team_1: dba}\ngroup_by: [a]\ngroup_wait: 1s\ngroup_interval: 1m\nrepeat_interval: 1h\n", false, ""},
		{"match: {severity: critical}\ncontinue: true\n", true, "continue"},
		{"match_re: {severity: x}\n", true, "match_re"},
		{"receiver: {type: slack}\n", true, "non-empty 'match'"},
		{"match: {}\n", true, "non-empty 'match'"},
		{"match: [severity]\n", true, "non-empty 'match'"},
		{"match: {bad-label: x}\n", true, "bad-label"},
		{"match: {1abc: x}\n", true, "1abc"},
		{"match: {severity: 1}\n", true, "must be a string"},
		{"match: {severity: true}\n", true, "must be a string"},
		{"match: {severity: null}\n", true, "must be a string"},
		{"match: {severity: ''}\n", true, "is empty"},
		// yaml.v3 reads YAML 1.1 `yes` as text (PyYAML: True) — a matrix row.
		{"match: {paging: yes}\n", false, ""},
	}
	for _, tc := range cases {
		var entry any
		if err := yaml.Unmarshal([]byte(tc.src), &entry); err != nil {
			t.Fatal(err)
		}
		reason, bad := RouteEntryProblem(entry)
		if bad != tc.bad || !strings.Contains(reason, tc.mention) {
			t.Errorf("%q: bad=%v reason=%q, want bad=%v mentioning %q", tc.src, bad, reason, tc.bad, tc.mention)
		}
	}
	for _, v := range []any{"x", 1, nil, []any{}} {
		if _, bad := RouteEntryProblem(v); !bad {
			t.Errorf("non-mapping entry %#v accepted", v)
		}
	}
	if _, bad := RouteEntryProblem(map[any]any{1: "x", "match": map[string]any{"a": "b"}}); !bad {
		t.Error("a non-string entry key must be unsupported")
	}
}

func TestTargets(t *testing.T) {
	t.Parallel()
	resolved := decode(t, `
receiver: {type: pagerduty}
overrides:
  - {alertname: A, receiver: {type: email}}
  - {alertname: A, metric_group: B, receiver: {type: slack}}
  - {receiver: {type: slack}}
  - {alertname: '', metric_group: mg, receiver: {type: webhook}}
  - {alertname: C}
  - not-a-mapping
routes:
  - {match: {severity: critical}, receiver: {type: teams}}
  - {match: {severity: warning}}
  - {match: {}, receiver: {type: slack}}
  - {match: {team: dba}, receiver: {type: 7}}
`)
	var got []string
	for _, tg := range Targets(resolved) {
		got = append(got, tg.Ref+"="+ReceiverType(tg.Receiver))
	}
	want := []string{"receiver=pagerduty", "overrides[0]=email", "overrides[3]=webhook", "routes[0]=teams", "routes[3]="}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("targets = %v, want %v", got, want)
	}
	if Targets(decode(t, "overrides: [{alertname: A, receiver: {type: slack}}]\n")) != nil {
		t.Error("no main receiver: nothing renders, so nothing is a target")
	}
}

func TestCheckReceiverTypes_IndependentConstraintsInDomainOrder(t *testing.T) {
	t.Parallel()
	resolved := decode(t, "receiver: {type: slack}\nroutes: [{match: {a: b}, receiver: {type: email}}]\n")
	pols := []Policy{
		{Domain: "zeta", Tenants: []string{"t-x"}, AllowedReceiverTypes: []string{"pagerduty"}},
		{Domain: "alpha", Tenants: []string{"t-x"}, ForbiddenReceiverTypes: []string{"slack"}, AllowedReceiverTypes: []string{"email"}},
		{Domain: "other", Tenants: []string{"t-y"}, ForbiddenReceiverTypes: []string{"slack"}},
	}
	var got []string
	for _, v := range CheckReceiverTypes("t-x", resolved, pols) {
		got = append(got, v.Domain+"/"+v.Target+"/"+v.Constraint+"/"+v.ReceiverType)
	}
	want := []string{
		"alpha/receiver/forbidden_receiver_types/slack",
		"alpha/receiver/allowed_receiver_types/slack",
		"zeta/receiver/allowed_receiver_types/slack",
		"zeta/routes[0]/allowed_receiver_types/email",
	}
	if !reflect.DeepEqual(got, want) {
		t.Errorf("violations =\n %v\nwant\n %v", got, want)
	}
	if pols[0].Domain != "zeta" {
		t.Error("CheckReceiverTypes reordered the caller's slice")
	}
}

func TestLoadRoot_Layers(t *testing.T) {
	t.Parallel()
	dir := writeRoot(t, map[string]string{
		// Both carrier spellings: only _defaults.yaml is read.
		"_defaults.yaml": "_routing_defaults:\n  receiver: {type: email}\n  routes: [{match: {a: b}, receiver: {type: slack}}]\n",
		"_defaults.yml":  "_routing_defaults:\n  receiver: {type: webhook}\n",
		// Any `_` file carries _routing_defaults; a later one replaces it whole.
		"_zz_routing.yaml":       "_routing_defaults:\n  group_wait: 5s\n",
		"_routing_profiles.yaml": "routing_profiles:\n  a: {receiver: {type: slack}}\n  b: {receiver: {type: email}}\n",
		"_routing_profiles.yml":  "routing_profiles:\n  b: {receiver: {type: teams}}\n  c: not-a-mapping\n",
		"_domain_policy.yaml": "domain_policies:\n  finance:\n    tenants: [t-a, 010]\n" +
			"    constraints: {forbidden_receiver_types: [slack], allowed_receiver_types: [email, 3]}\n",
		"_domain_policy.yml":         "domain_policies:\n  ops: {tenants: [t-b], constraints: {allowed_receiver_types: [email]}}\n",
		"tenant.yaml":                "_routing_defaults: {receiver: {type: slack}}\nrouting_profiles: {x: {}}\n",
		"sub/_routing_profiles.yaml": "routing_profiles:\n  nested: {receiver: {type: slack}}\n",
		".hidden.yaml":               "_routing_defaults: {receiver: {type: slack}}\n",
	})
	layers, pols, probs := LoadRoot(dir, nil)
	// The stripped routes are named even though a later file replaces the
	// whole block (the Python reader records the WARN per file too).
	if len(probs) != 1 || probs[0].Kind != ProblemRoutingDefaultsRoutes || probs[0].File != "_defaults.yaml" ||
		probs[0].Field != "_routing_defaults.routes" {
		t.Fatalf("problems: %+v", probs)
	}
	if !reflect.DeepEqual(layers.Defaults, map[string]any{"group_wait": "5s"}) {
		t.Errorf("defaults = %#v", layers.Defaults)
	}
	if got := ReceiverType(layers.Profiles["b"]["receiver"]); got != "teams" {
		t.Errorf("profile b = %q, want the later file's teams", got)
	}
	if _, known := layers.Profiles["c"]; !known || layers.Profiles["c"] != nil {
		t.Errorf("profile c must be known and empty: %#v", layers.Profiles["c"])
	}
	for _, name := range []string{"x", "nested"} {
		if _, known := layers.Profiles[name]; known {
			t.Errorf("profile %q read from a file the reader does not take profiles from", name)
		}
	}
	want := []Policy{
		{Domain: "finance", Tenants: []string{"t-a", "010"}, ForbiddenReceiverTypes: []string{"slack"}, AllowedReceiverTypes: []string{"email"}},
		{Domain: "ops", Tenants: []string{"t-b"}, AllowedReceiverTypes: []string{"email"}},
	}
	if !reflect.DeepEqual(pols, want) {
		t.Errorf("policies = %+v\nwant %+v", pols, want)
	}

	// The defaults carrier alone: routes are stripped before any merge.
	dir2 := writeRoot(t, map[string]string{
		"_defaults.yaml": "_routing_defaults:\n  receiver: {type: email}\n  routes: [{match: {a: b}, receiver: {type: slack}}]\n",
	})
	l2, _, p2 := LoadRoot(dir2, nil)
	if len(p2) != 1 || p2[0].Kind != ProblemRoutingDefaultsRoutes {
		t.Errorf("stripped routes must be reported: %+v", p2)
	}
	if _, has := l2.Defaults["routes"]; has || ReceiverType(l2.Defaults["receiver"]) != "email" {
		t.Errorf("defaults = %#v, want the receiver without routes", l2.Defaults)
	}
}

func TestLoadRoot_ProblemsAndSkip(t *testing.T) {
	t.Parallel()
	dir := writeRoot(t, map[string]string{
		"_domain_policy.yaml": "domain_policies:\n" +
			"  a: {tenants: t-a, constraints: {forbidden_receiver_types: [slack]}}\n" +
			"  b: {tenants: [t-b], constraints: {forbidden_receiver_types: slack, allowed_receiver_types: [email]}}\n" +
			"  c: [not, a, mapping]\n" +
			"  d: {tenants: [t-d], constraints: [x]}\n" +
			"  e: ~\n" +
			"  f: {tenants: [t-f], constraints: ~}\n",
		"_routing_profiles.yaml": "routing_profiles: [a, b]\n",
	})
	_, pols, probs := LoadRoot(dir, nil)
	var fields []string
	for _, p := range probs {
		fields = append(fields, p.Kind+":"+p.Field)
	}
	want := []string{
		"routing_profiles_unusable:routing_profiles",
		"domain_policy_unusable:domain_policies.a.tenants",
		"domain_policy_unusable:domain_policies.b.constraints.forbidden_receiver_types",
		"domain_policy_unusable:domain_policies.c",
		"domain_policy_unusable:domain_policies.d.constraints",
	}
	if !reflect.DeepEqual(fields, want) {
		t.Errorf("problems = %v\nwant %v", fields, want)
	}
	// b keeps the constraint that is usable; a / c / d / e / f enforce nothing.
	if len(pols) != 1 || pols[0].Domain != "b" || len(pols[0].ForbiddenReceiverTypes) != 0 ||
		!reflect.DeepEqual(pols[0].AllowedReceiverTypes, []string{"email"}) {
		t.Errorf("policies = %+v", pols)
	}

	bad := writeRoot(t, map[string]string{
		"_domain_policy.yaml":    "domain_policies: [\n",
		"_routing_profiles.yaml": "- a\n- b\n",
	})
	_, _, probs = LoadRoot(bad, nil)
	if len(probs) != 2 || probs[0].File != "_domain_policy.yaml" || probs[1].File != "_routing_profiles.yaml" {
		t.Errorf("problems = %+v", probs)
	}
	// A file the caller already reported is not read, and so not named twice.
	_, _, probs = LoadRoot(bad, func(rel string) bool { return rel == "_domain_policy.yaml" })
	if len(probs) != 1 || probs[0].Kind != ProblemRoutingProfilesUnusable {
		t.Errorf("with skip: problems = %+v", probs)
	}
}
