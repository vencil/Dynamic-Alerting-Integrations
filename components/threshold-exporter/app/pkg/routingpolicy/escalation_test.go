package routingpolicy

// escalation_test.go — #2325 unit pins that the parity matrix does not
// carry: the constraint's value shapes, PyStr, and the ORDER and label sets
// of JudgeCriticalEscalation (the matrix pins presence and ref order across
// languages; these pin the pieces a message is built from).

import (
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

func TestParseDomainPolicies_RequireCriticalEscalationValue(t *testing.T) {
	t.Parallel()
	cases := []struct {
		value   string
		on      bool
		problem bool
	}{
		{"true", true, false},
		{"false", false, false},
		{"~", false, false},
		{`"true"`, false, true},
		{"1", false, true},
		{"[true]", false, true},
		// PyYAML (YAML 1.1) reads these plain scalars as booleans, and so
		// does DecodePyYAML; yaml.v3 alone would read them as strings.
		{"yes", true, false},
		{"On", true, false},
		{"OFF", false, false},
		{"no", false, false},
		// Quoted or !!str-tagged stays a string in PyYAML too.
		{`"yes"`, false, true},
		{"'on'", false, true},
		{"!!str yes", false, true},
		// Not in PyYAML's set: a string.
		{"yEs", false, true},
		{"y", false, true},
		// Tagged !!bool: PyYAML's construct_yaml_bool reads value.lower().
		{"!!bool yes", true, false},
		{"!!bool yEs", true, false},
		{"!!bool 'no'", false, false},
		{"!<tag:yaml.org,2002:bool> on", true, false},
		// yaml.v3 reads these, PyYAML refuses them (the generator drops the
		// file): a problem, never a silently skipped constraint.
		{"!!int 0X1F", false, true},
		{"2001-13-40", false, true},
	}
	for _, tc := range cases {
		src := "domain_policies:\n  d:\n    tenants: [t1]\n    constraints:\n      require_critical_escalation: " + tc.value + "\n"
		pols, probs, err := ParseDomainPolicies([]byte(src))
		if err != nil {
			t.Fatalf("%s: %v", tc.value, err)
		}
		if len(pols) != 1 || pols[0].RequireCriticalEscalation != tc.on {
			t.Errorf("%s: policies %+v, want RequireCriticalEscalation=%v", tc.value, pols, tc.on)
		}
		if (len(probs) > 0) != tc.problem {
			t.Errorf("%s: problems %+v, want problem=%v", tc.value, probs, tc.problem)
		}
		for _, p := range probs {
			if p.Kind != ProblemDomainPolicyUnusable || p.Field != "domain_policies.d.constraints.require_critical_escalation" {
				t.Errorf("%s: problem %+v", tc.value, p)
			}
		}
	}
}

// A value PyYAML refuses (`!!bool y`) fails PyYAML's whole safe_load, and
// the generator drops the file: da-guard refuses the whole document, and a
// PyYAMLValue field fails the struct it sits in (tenant-api's parseConfig,
// so a hot reload keeps the last good policy). One PyYAML reads but yaml.v3
// cannot (`!!int 1:30`, `!!binary 1_000`) decodes, as a non-boolean.
func TestPyYAMLValue_RefusedFailsTheDecode(t *testing.T) {
	t.Parallel()
	src := "domain_policies:\n  d:\n    tenants: [t1]\n    constraints:\n      require_critical_escalation: !!bool y\n"
	if _, _, err := ParseDomainPolicies([]byte(src)); err == nil {
		t.Errorf("ParseDomainPolicies(!!bool y): want the document refused")
	}
	type doc struct {
		E PyYAMLValue `yaml:"e"`
		S []string    `yaml:"s"`
	}
	for _, v := range []string{"!!bool y", "!!bool 1", "!!int abc", "!!int 0X1F"} {
		var d doc
		if err := yaml.Unmarshal([]byte("e: "+v+"\ns: [webhook]\n"), &d); err == nil {
			t.Errorf("%s: decoded %#v, want the decode to fail as PyYAML's does", v, d.E.Value)
		}
	}
	for v, want := range map[string]any{"!!int 1:30": "1:30", "!!binary 1_000": "1_000", "!!int 5": 5} {
		var d doc
		if err := yaml.Unmarshal([]byte("e: "+v+"\ns: [webhook]\n"), &d); err != nil {
			t.Errorf("%s: %v, want it decoded (PyYAML reads it)", v, err)
			continue
		}
		if !reflect.DeepEqual(d.E.Value, want) || !reflect.DeepEqual(d.S, []string{"webhook"}) {
			t.Errorf("%s: got %#v / %v, want %#v and [webhook]", v, d.E.Value, d.S, want)
		}
	}
}

func TestPyStr(t *testing.T) {
	t.Parallel()
	for in, want := range map[any]string{
		"MariaDBDown": "MariaDBDown", 123: "123", int64(-7): "-7", true: "True", false: "False",
		1.5: "1.5", 1.0: "1.0", 1e16: "1e+16", 1.5e-05: "1.5e-05", 0.0001: "0.0001",
	} {
		if got := PyStr(in); got != want {
			t.Errorf("PyStr(%#v) = %q, want %q", in, got, want)
		}
	}
	if got := PyStr([]any{"a", 1}); got != "['a', 1]" {
		t.Errorf("PyStr(list) = %q", got)
	}
}

func esc(main string, overrides []any, routes ...any) map[string]any {
	r := map[string]any{"receiver": map[string]any{"type": main}}
	if overrides != nil {
		r["overrides"] = overrides
	}
	if routes != nil {
		r["routes"] = routes
	}
	return r
}

func route(match map[string]any, typ string) map[string]any {
	return map[string]any{"match": match, "receiver": map[string]any{"type": typ}}
}

func TestJudgeCriticalEscalation(t *testing.T) {
	t.Parallel()
	partial := route(map[string]any{"severity": "critical", "team": "db"}, "pagerduty")
	full := route(map[string]any{"severity": "critical"}, "pagerduty")

	v, ok := JudgeCriticalEscalation("t1", esc("slack", nil, route(map[string]any{"severity": "critical"}, "email")))
	if !ok || v.Compliant() || v.Leaks != nil {
		t.Errorf("non-pagerduty critical route: %+v ok=%v, want non-compliant", v, ok)
	}
	if _, ok := JudgeCriticalEscalation("t1", map[string]any{"receiver": ""}); ok {
		t.Error("empty main receiver judged")
	}

	// Render order: overrides, routes, then the main receiver; each with
	// its own match and the caught label set.
	v, _ = JudgeCriticalEscalation("t1", esc("webhook",
		[]any{map[string]any{"metric_group": "g", "receiver": map[string]any{"type": "email"}}},
		partial, route(map[string]any{"team": "app"}, "slack")))
	want := []EscalationLeak{
		{Ref: "overrides[0]", ReceiverType: "email", Match: map[string]string{"metric_group": "g"},
			Caught: map[string]string{"severity": "critical", "metric_group": "g"}},
		{Ref: "routes[1]", ReceiverType: "slack", Match: map[string]string{"team": "app"},
			Caught: map[string]string{"severity": "critical", "team": "app"}},
		{Ref: "receiver", ReceiverType: "webhook", Caught: map[string]string{"severity": "critical"}},
	}
	if v.Target != "routes[0]" || !reflect.DeepEqual(v.Leaks, want) {
		t.Errorf("got %+v, want target routes[0] and %+v", v, want)
	}
	if got := FormatLabels(want[1].Caught); got != "severity=critical, team=app" {
		t.Errorf("FormatLabels = %q", got)
	}

	// Subset skip and the tenant label.
	for name, tc := range map[string]struct {
		routing map[string]any
		leaks   []string
	}{
		"full escalation takes everything after it": {
			esc("slack", nil, full, route(map[string]any{"team": "app"}, "slack")), nil},
		"own tenant label takes the main receiver's alerts": {
			esc("slack", nil, partial, route(map[string]any{"tenant": "t1"}, "slack")), []string{"routes[1]"}},
		"another tenant's route receives nothing": {
			esc("pagerduty", nil, route(map[string]any{"tenant": "t2"}, "slack")), nil},
		"a non-critical severity receives nothing": {
			esc("pagerduty", nil, route(map[string]any{"severity": "warning"}, "slack")), nil},
		"int override value renders as the route's string": {
			esc("pagerduty", []any{map[string]any{"alertname": 123, "receiver": map[string]any{"type": "pagerduty"}}},
				route(map[string]any{"alertname": "123"}, "slack")), nil},
	} {
		v, _ := JudgeCriticalEscalation("t1", tc.routing)
		var refs []string
		for _, l := range v.Leaks {
			refs = append(refs, l.Ref)
		}
		if !v.Compliant() || !reflect.DeepEqual(refs, tc.leaks) {
			t.Errorf("%s: %+v, want leaks %v", name, v, tc.leaks)
		}
	}
}

func TestCheckCriticalEscalation_PerRequiringDomain(t *testing.T) {
	t.Parallel()
	pols := []Policy{
		{Domain: "z", Tenants: []string{"t1"}, RequireCriticalEscalation: true},
		{Domain: "a", Tenants: []string{"t1"}, RequireCriticalEscalation: true},
		{Domain: "off", Tenants: []string{"t1"}},
		{Domain: "other", Tenants: []string{"t2"}, RequireCriticalEscalation: true},
	}
	got := CheckCriticalEscalation("t1", esc("slack", nil), pols)
	if len(got) != 2 || got[0].Domain != "a" || got[1].Domain != "z" || got[0].Verdict.Compliant() {
		t.Errorf("got %+v, want non-compliant for a then z", got)
	}
}

// A `!!null`-tagged value (#2325): yaml.v3 decodes it itself, never through
// PyYAMLValue. PyYAML reads a tagged scalar as None — the constraint is off,
// the rest of the policy applies, as the generator enforces it — and refuses
// a tagged collection, dropping the whole file.
func TestParseDomainPolicies_TaggedNullEscalation(t *testing.T) {
	t.Parallel()
	for v, refused := range map[string]bool{
		"!!null x": false, "!<tag:yaml.org,2002:null> x": false, `!!null ""`: false,
		"!!null {}": true, "!!null [1]": true,
	} {
		src := "domain_policies:\n  d:\n    tenants: [t1]\n    constraints:\n      require_critical_escalation: " +
			v + "\n      forbidden_receiver_types: [slack]\n"
		pols, probs, err := ParseDomainPolicies([]byte(src))
		if refused {
			if err == nil {
				t.Errorf("%s: policies %+v, want the document refused (PyYAML refuses it)", v, pols)
			}
			continue
		}
		if err != nil || len(probs) != 0 {
			t.Errorf("%s: err %v, problems %+v; want None read as an absent constraint", v, err, probs)
			continue
		}
		if len(pols) != 1 || pols[0].RequireCriticalEscalation || !reflect.DeepEqual(pols[0].ForbiddenReceiverTypes, []string{"slack"}) {
			t.Errorf("%s: policies %+v, want escalation off and forbid [slack]", v, pols)
		}
	}
}

// A mapping or sequence value — an alias cycle and a fan-out included —
// fails closed without a crash and fast (#2325): the document is refused
// (yaml.v3's own alias checks) or the constraint is reported and off while
// the rest of the policy still applies.
func TestParseDomainPolicies_CollectionEscalationFailsClosed(t *testing.T) {
	t.Parallel()
	fan := "&l0 [x,x,x,x,x,x,x,x,x,x]"
	for i := 1; i <= 8; i++ {
		fan += fmt.Sprintf(", &l%d [%s]", i, strings.TrimSuffix(strings.Repeat(fmt.Sprintf("*l%d,", i-1), 10), ","))
	}
	for _, v := range []string{"&x [*x]", "&x {b: *x}", "[" + fan + "]", "!!omap [{[1]: 2}]", "{<<: !foo {b: 1}}"} {
		src := "domain_policies:\n  d:\n    tenants: [t1]\n    constraints:\n      require_critical_escalation: " +
			v + "\n      forbidden_receiver_types: [slack]\n"
		start := time.Now()
		pols, probs, err := ParseDomainPolicies([]byte(src))
		if d := time.Since(start); d > time.Second {
			t.Errorf("%.40s: took %v", v, d)
		}
		if err != nil {
			continue // refused whole: fail-closed
		}
		if len(pols) != 1 || pols[0].RequireCriticalEscalation || !reflect.DeepEqual(pols[0].ForbiddenReceiverTypes, []string{"slack"}) {
			t.Errorf("%.40s: policies %+v, want escalation off and forbid [slack]", v, pols)
		}
		if len(probs) != 1 || probs[0].Field != "domain_policies.d.constraints.require_critical_escalation" {
			t.Errorf("%.40s: problems %+v, want the constraint reported", v, probs)
		}
	}
}
