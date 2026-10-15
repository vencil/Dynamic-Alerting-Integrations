package routingpolicy

import (
	"os"
	"path/filepath"
	"testing"
)

// TestEnforcedFrom_MatchShape (#2989): an enabled `_routing_enforced` whose
// `match` is written and is not a list (as PyYAML reads it) is not enabled
// and is reported; absent, null and any list are enabled, as
// _grar_routes.enforced_match_invalid decides.
func TestEnforcedFrom_MatchShape(t *testing.T) {
	t.Parallel()
	for value, refused := range map[string]bool{
		"{severity: critical}":      true, // the routes[i].match spelling, the issue's case
		"{}":                        true,
		"'severity=\"critical\"'":   true, // one matcher string, not a list of them
		"''":                        true,
		"42":                        true,
		"yes":                       true, // a YAML boolean to PyYAML
		"~":                         false,
		"":                          false, // `match:` alone is null
		"[]":                        false,
		"['severity=\"critical\"']": false,
	} {
		doc := "_routing_enforced:\n  enabled: true\n  match: " + value +
			"\n  receiver: {type: webhook, url: 'https://noc.example.com/h'}\n"
		top, err := parseDoc([]byte(doc), false)
		if err != nil {
			t.Fatalf("%q: %v", value, err)
		}
		e, probs := enforcedFrom("_noc.yaml", top)
		if refused {
			if e != nil {
				t.Errorf("match: %s: enabled, want refused", value)
			}
			if len(probs) != 1 || probs[0].Kind != ProblemRoutingEnforcedMatchInvalid ||
				probs[0].File != "_noc.yaml" || probs[0].Field != "_routing_enforced.match" {
				t.Errorf("match: %s: problems %+v, want one %s at _noc.yaml:_routing_enforced.match",
					value, probs, ProblemRoutingEnforcedMatchInvalid)
			}
			continue
		}
		if e == nil || len(probs) != 0 {
			t.Errorf("match: %s: enforced=%v problems=%+v, want enabled and nothing reported", value, e, probs)
		}
	}
	// Absent: enabled, nothing reported.
	top, err := parseDoc([]byte("_routing_enforced:\n  enabled: true\n  receiver: {type: webhook}\n"), false)
	if err != nil {
		t.Fatal(err)
	}
	if e, probs := enforcedFrom("_noc.yaml", top); e == nil || len(probs) != 0 {
		t.Errorf("no match: enforced=%v problems=%+v, want enabled", e, probs)
	}
	// A disabled block is not judged at all.
	top, err = parseDoc([]byte("_routing_enforced:\n  enabled: false\n  match: {severity: critical}\n"), false)
	if err != nil {
		t.Fatal(err)
	}
	if e, probs := enforcedFrom("_noc.yaml", top); e != nil || len(probs) != 0 {
		t.Errorf("disabled: enforced=%v problems=%+v, want nothing", e, probs)
	}
}

// TestLoadTree_EnforcedMatchKeepsEarlierBlock (#2989): the refused block of a
// later root file leaves the earlier enabled one in force, and the refusal is
// a Problem that does not block the tree.
func TestLoadTree_EnforcedMatchKeepsEarlierBlock(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	write := func(name, body string) {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	write("_defaults.yaml", "_routing_enforced:\n  enabled: true\n  match: ['severity=\"critical\"']\n"+
		"  receiver: {type: webhook, url: 'https://noc.example.com/a'}\n")
	write("_noc.yaml", "_routing_enforced:\n  enabled: true\n  match: {severity: critical}\n"+
		"  receiver: {type: webhook, url: 'https://noc.example.com/b'}\n")
	write("t-a.yaml", "tenants:\n  t-a:\n    cpu_usage_percent: '85'\n")
	tree, _, probs := LoadTree(dir, nil)
	if tree.Enforced == nil || tree.Enforced.File != "_defaults.yaml" {
		t.Fatalf("Tree.Enforced = %+v, want the _defaults.yaml block", tree.Enforced)
	}
	var got []Problem
	for _, p := range probs {
		if p.Kind == ProblemRoutingEnforcedMatchInvalid {
			got = append(got, p)
		}
	}
	if len(got) != 1 || got[0].File != "_noc.yaml" {
		t.Errorf("problems %+v, want one %s naming _noc.yaml", got, ProblemRoutingEnforcedMatchInvalid)
	}
	if IsBlocking(ProblemRoutingEnforcedMatchInvalid) {
		t.Errorf("IsBlocking(%s) = true, want false", ProblemRoutingEnforcedMatchInvalid)
	}
}
