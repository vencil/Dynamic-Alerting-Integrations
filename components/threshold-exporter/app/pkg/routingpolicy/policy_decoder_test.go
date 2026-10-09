package routingpolicy

import (
	"errors"
	"testing"
)

// TestPolicyDecoder_OnlyThePolicyReadersScanAsPyYAML (hub #2486 PR-7c round
// 6): a `_domain_policy.yaml` is scanned with PyYAML's separators (no TAB)
// and must be UTF-8 — through all three readers — while every other file
// parseDoc reads keeps yaml.v3's reading, a TAB as a separator included.
func TestPolicyDecoder_OnlyThePolicyReadersScanAsPyYAML(t *testing.T) {
	t.Parallel()
	const fin = "domain_policies:\n  fin:\n    tenants: [t1]\n    constraints:\n      forbidden_receiver_types: [slack]\n"
	tabbed := []string{
		"domain_policies:\n  fin:\n    tenants: [t1]\t\n    constraints:\n      forbidden_receiver_types: [slack]\n",
		"domain_policies:\n  fin:\n    tenants:\t[t1]\n    constraints:\n      forbidden_receiver_types: [slack]\n",
		"domain_policies: {fin: {tenants: [t1],\tconstraints: {forbidden_receiver_types: [slack]}}}\n",
		"%YAML\t1.1\n---\n" + fin,
		"%YAML 1.1#c\n---\n" + fin,
		"---\t\n" + fin,
		"domain_policies: \t# c\n  fin:\n    tenants: [t1]\n    constraints:\n      forbidden_receiver_types: [slack]\n",
		"# a\n\t# b\n" + fin,
	}
	notUTF8 := [][]byte{
		append([]byte{0xFF, 0xFE}, utf16le(fin)...),
		append([]byte("# \xe9\n"), fin...),
	}
	for _, src := range tabbed {
		data := []byte(src)
		if _, err := parseDoc(data, true); err == nil {
			t.Errorf("parseDoc(policy) read %q; PyYAML drops it", src)
		}
		if err := UnmarshalPolicy(data, &map[string]any{}); err == nil {
			t.Errorf("UnmarshalPolicy read %q; PyYAML drops it", src)
		}
		// Not a parse: DomainPoliciesShapeError leaves it to UnmarshalPolicy.
		if err := DomainPoliciesShapeError(data); err != nil {
			t.Errorf("DomainPoliciesShapeError(%q) = %v, want nil (the caller's decode refuses it)", src, err)
		}
	}
	// The same TAB in any other platform file: yaml.v3's reading, unchanged.
	for _, src := range []string{"tenants:\n  t1:\n    cpu: 1\t\n", "tenants: {t1: {cpu: 1,\tmem: 2}}\n"} {
		if top, err := parseDoc([]byte(src), false); err != nil || top == nil {
			t.Errorf("parseDoc(not a policy, %q) = %v, %v; want it read", src, top, err)
		}
	}
	for _, data := range notUTF8 {
		if _, err := parseDoc(data, true); !errors.Is(err, errNotUTF8) {
			t.Errorf("parseDoc(policy, % x…) err = %v, want errNotUTF8", data[:4], err)
		}
		if err := UnmarshalPolicy(data, &map[string]any{}); !errors.Is(err, errNotUTF8) {
			t.Errorf("UnmarshalPolicy(% x…) err = %v, want errNotUTF8", data[:4], err)
		}
	}
	// What PyYAML reads stays read: a TAB in quotes, a comment, a block scalar.
	for _, src := range []string{
		fin + "x: \"a\tb\"\n",
		fin + "# a\tb\n",
		fin + "x: |\n  a\tb\n",
		"\xef\xbb\xbf" + fin,
	} {
		pols, _, err := ParseDomainPolicies([]byte(src))
		if err != nil || len(pols) != 1 {
			t.Errorf("ParseDomainPolicies(%q) = %v, %v; want the policy", src, pols, err)
		}
	}
}

func utf16le(s string) []byte {
	var out []byte
	for _, r := range s { // ASCII only here
		out = append(out, byte(r), 0)
	}
	return out
}
