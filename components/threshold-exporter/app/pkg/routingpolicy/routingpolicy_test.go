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
	if !ok || unknown != nil {
		t.Fatalf("ok=%v unknown=%v", ok, unknown)
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
		wantUnknown string // "-" = none
	}{
		{"disable string beats the profile", "_routing_profile: chat\n_routing: ' Disabled '\n", false, "-"},
		{"unknown profile, nothing else", "_routing_profile: nope\n", false, "nope"},
		{"unknown profile is reported even when disabled", "_routing_profile: nope\n_routing: off\n", false, "nope"},
		{"whitespace-only reference names the unknown profile ''", "_routing_profile: '   '\n", false, ""},
		{"empty string is no reference", "_routing_profile: ''\n", false, "-"},
		{"known but not a mapping", "_routing_profile: broken\n", false, "-"},
		{"empty _routing mapping", "_routing: {}\n", false, "-"},
		{"non-string profile reference is ignored", "_routing_profile: 7\n_routing: {receiver: {type: email}}\n", true, "-"},
		{"non-disabling string _routing is ignored", "_routing_profile: chat\n_routing: yes-please\n", true, "-"},
	}
	for _, tc := range cases {
		_, ok, _, unknown := Resolve("t-x", decode(t, tc.block), layers)
		got := "-"
		if unknown != nil {
			got = *unknown
		}
		if ok != tc.wantOK || got != tc.wantUnknown {
			t.Errorf("%s: ok=%v unknown=%q, want ok=%v unknown=%q", tc.name, ok, got, tc.wantOK, tc.wantUnknown)
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

	// An allowed list whose entries are all non-strings allows nothing (the
	// Python check restricts on the non-empty set); non-string forbidden
	// entries match nothing.
	odd := []Policy{{Domain: "odd", Tenants: []string{"t-x"}, AllowedListNonEmpty: true}}
	if v := CheckReceiverTypes("t-x", decode(t, "receiver: {type: pagerduty}\n"), odd); len(v) != 1 ||
		v[0].Constraint != ConstraintAllowed {
		t.Errorf("non-string-only allowed list: %+v, want one allowed violation", v)
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
		"_domain_policy.yml":         "domain_policies:\n  ops: {tenants: [t-b], constraints: {allowed_receiver_types: [email]}}\n  odd: {tenants: [t-c], constraints: {allowed_receiver_types: [true, ~, 1], forbidden_receiver_types: [1]}}\n",
		"tenant.yaml":                "_routing_defaults: {receiver: {type: slack}}\nrouting_profiles: {x: {}}\n",
		"sub/_routing_profiles.yaml": "routing_profiles:\n  nested: {receiver: {type: slack}}\n",
		".hidden.yaml":               "_routing_defaults: {receiver: {type: slack}}\n",
	})
	layers, pols, probs := LoadRoot(dir, nil)
	// The stripped routes are named even though a later file replaces the
	// whole block (the Python reader records the WARN per file too). #2326:
	// profile "b" in both root spellings is a duplicate name, named on the
	// later file.
	wantProbs := [][3]string{
		{ProblemRoutingDefaultsRoutes, "_defaults.yaml", "_routing_defaults.routes"},
		{ProblemRoutingProfileDuplicate, "_routing_profiles.yml", "routing_profiles.b"},
	}
	var gotProbs [][3]string
	for _, p := range probs {
		gotProbs = append(gotProbs, [3]string{p.Kind, p.File, p.Field})
	}
	if !reflect.DeepEqual(gotProbs, wantProbs) {
		t.Fatalf("problems: %+v", probs)
	}
	if !reflect.DeepEqual(layers.Defaults, map[string]any{"group_wait": "5s"}) {
		t.Errorf("defaults = %#v", layers.Defaults)
	}
	// #2326 (ADR-007 amendment (c)): the first definition is kept — before,
	// the later file silently replaced it.
	if got := ReceiverType(layers.Profiles["b"]["receiver"]); got != "email" {
		t.Errorf("profile b = %q, want the first file's email", got)
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
		{Domain: "finance", Tenants: []string{"t-a", "010"}, ForbiddenReceiverTypes: []string{"slack"},
			AllowedReceiverTypes: []string{"email"}, AllowedListNonEmpty: true},
		{Domain: "odd", Tenants: []string{"t-c"}, AllowedListNonEmpty: true},
		{Domain: "ops", Tenants: []string{"t-b"}, AllowedReceiverTypes: []string{"email"}, AllowedListNonEmpty: true},
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
		!reflect.DeepEqual(pols[0].AllowedReceiverTypes, []string{"email"}) || !pols[0].AllowedListNonEmpty {
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

// TestWithPyYAMLReceivers_NonStringKeysAndFailClosed (#2295 review): the
// PyYAML side is read by its string key `receiver` even when another key is
// not a string (map[any]any), and a receiver whose PyYAML reading is not
// found is Unmatched — never the yaml.v3 value it had.
func TestWithPyYAMLReceivers_NonStringKeysAndFailClosed(t *testing.T) {
	v3 := map[string]any{"true": "x", "receiver": "v3", "overrides": []any{map[string]any{"1": "y", "receiver": "v3"}}}
	cases := []struct {
		name        string
		py          any
		main, over0 any
	}{
		{"non-string keys on the PyYAML side", map[any]any{true: "x", "receiver": "py",
			"overrides": []any{map[any]any{1: "y", "receiver": "py"}}}, "py", "py"},
		{"no PyYAML reading", nil, Unmatched, Unmatched},
		{"PyYAML routing not a mapping", "no", Unmatched, Unmatched},
		{"list of another length", map[string]any{"receiver": "py", "overrides": []any{}}, "py", Unmatched},
		{"list not a list", map[string]any{"receiver": "py", "overrides": "no"}, "py", Unmatched},
		{"entry not a mapping", map[string]any{"receiver": "py", "overrides": []any{"no"}}, "py", Unmatched},
		{"receiver key missing", map[string]any{"overrides": []any{map[string]any{}}}, Unmatched, Unmatched},
	}
	for _, tc := range cases {
		got, _ := WithPyYAMLReceivers(v3, tc.py).(map[string]any)
		over0 := got["overrides"].([]any)[0].(map[string]any)["receiver"]
		if !reflect.DeepEqual(got["receiver"], tc.main) || !reflect.DeepEqual(over0, tc.over0) {
			t.Errorf("%s: receiver %#v, overrides[0].receiver %#v; want %#v, %#v", tc.name, got["receiver"], over0, tc.main, tc.over0)
		}
	}
	if v3["receiver"] != "v3" || v3["overrides"].([]any)[0].(map[string]any)["receiver"] != "v3" {
		t.Error("WithPyYAMLReceivers modified its routing argument")
	}
}

// TestParseDoc_GeneratorRepeatedKeyRefusesTheFile (#2295): a key the route
// generator counts as written twice and yaml.v3 does not (an alias key beside
// its anchor, two `<<`) fails the whole document, as a plain repeat does — no
// block of it is read, whatever mapping the repeat is in.
func TestParseDoc_GeneratorRepeatedKeyRefusesTheFile(t *testing.T) {
	for name, src := range map[string]string{
		"policy alias key":   "domain_policies:\n  d1:\n    &c constraints :\n      forbidden_receiver_types: [webhook]\n    *c : {}\n",
		"profiles alias key": "routing_profiles:\n  &p p1 :\n    receiver: {type: webhook}\n  *p : {}\n",
		"repeat elsewhere":   "unrelated:\n  &k a : 1\n  *k : 2\nrouting_profiles:\n  p1: {receiver: {type: webhook}}\n",
		"two merge keys":     "x: &x {a: 1}\ny: &y {b: 1}\nz:\n  <<: *x\n  <<: *y\n",
	} {
		policy := strings.HasPrefix(src, "domain_policies:") // read as a _domain_policy.yaml
		if _, err := parseDoc([]byte(src), policy); err == nil || !strings.Contains(err.Error(), "already defined") {
			t.Errorf("%s: err = %v, want the repeated key named", name, err)
		}
	}
	tenant := "tenants:\n  &a t1 :\n    _routing: {receiver: {type: webhook}}\n  *a :\n    _routing: {receiver: {type: email}}\n"
	if got := PyYAMLRoutingByTenant([]byte(tenant)); got != nil {
		t.Errorf("PyYAMLRoutingByTenant = %v, want nil (no PyYAML reading: Unmatched downstream)", got)
	}
	// A merge key overridden by an explicit key is no repeat.
	if _, err := parseDoc([]byte("x: &x {a: 1}\nz:\n  <<: *x\n  a: 2\n"), false); err != nil {
		t.Errorf("merge override: %v", err)
	}
}

// TestAliasKeyNamesAreTheAnchoredText (#2437): a profile or domain named by an
// alias key is named by its anchor's text, as PyYAML reads it — not by the
// anchor's name, which no tenant references.
func TestAliasKeyNamesAreTheAnchoredText(t *testing.T) {
	profiles, present, err := ParseRoutingProfiles([]byte(
		"x: &p p1\nrouting_profiles:\n  *p :\n    receiver: {type: webhook, url: \"https://a\"}\n"))
	if err != nil || !present {
		t.Fatalf("ParseRoutingProfiles: present %v, err %v", present, err)
	}
	if _, ok := profiles["p1"]; !ok || len(profiles) != 1 {
		t.Errorf("profile names = %v, want only p1", sortedKeys(profiles))
	}
	pols, probs, err := ParseDomainPolicies([]byte(
		"x: &d fin\ndomain_policies:\n  *d :\n    tenants: [t1]\n    constraints: {forbidden_receiver_types: [slack]}\n"))
	if err != nil || len(probs) != 0 || len(pols) != 1 || pols[0].Domain != "fin" {
		t.Errorf("ParseDomainPolicies = %+v, %v, %v; want one policy for domain fin", pols, probs, err)
	}
}
