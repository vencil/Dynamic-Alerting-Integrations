package config

// policy_heads: which keys a Policy-as-Code rule of the root carrier names —
// the one reading both the shared key verdict (pkg/keyverdict, through
// `da-guard key-verdict`) and the exporter's keys-unread audit
// (da_config_keys_unread, #3353) take it from. A threshold key no exporter
// row reads is still read when a rule names it (ADR-038 decision 5), so the
// exporter must not report it as unread either; one function here keeps the
// two from parting.

import (
	"strings"

	"gopkg.in/yaml.v3"
)

// PolicyHead is the first path element of a Policy-as-Code rule's
// `target` / `when.target`, and the tenants the rule leaves out.
type PolicyHead struct {
	Head     string
	Excluded map[string]bool
}

// PolicyHeadsOf is Python's `policy_targets(_tree_rules(...))` over the root
// carrier's bytes: the top-level `policies` when the document has that key
// (whatever its value), else `_policies`; each rule's `target` and
// `when.target` up to the first ".", with its `exclude_tenants` read as
// scalar source text (`010` stays "010", #2114).
//
// ⚠️ Lenient where Python is not: a document, rule list, rule or field that
// cannot be read is skipped here, while policy_engine raises
// InvalidPolicyRules (#2961) and the Python verdict fails with it. A tree
// carrying such rules is one Python judges not at all; this side judges it
// without them.
func PolicyHeadsOf(data []byte) []PolicyHead {
	var doc yaml.Node
	if err := yaml.Unmarshal(data, &doc); err != nil {
		return nil
	}
	return policyHeadsOfDoc(&doc)
}

// policyHeadsOfDoc is PolicyHeadsOf over an already parsed document node
// (decodeConfigFile's, so a file is not parsed twice).
func policyHeadsOfDoc(doc *yaml.Node) []PolicyHead {
	if doc == nil || len(doc.Content) == 0 {
		return nil
	}
	top := doc.Content[0]
	if top.Kind != yaml.MappingNode {
		return nil
	}
	rules := yamlMappingValue(top, "policies")
	if rules == nil {
		rules = yamlMappingValue(top, "_policies")
	}
	if rules == nil || rules.Kind != yaml.SequenceNode {
		return nil
	}
	var out []PolicyHead
	for _, rule := range rules.Content {
		if rule.Kind != yaml.MappingNode {
			continue
		}
		var targets []string
		if t := yamlStringScalar(yamlMappingValue(rule, "target")); t != "" {
			targets = append(targets, t)
		}
		if w := yamlMappingValue(rule, "when"); w != nil && w.Kind == yaml.MappingNode {
			if t := yamlStringScalar(yamlMappingValue(w, "target")); t != "" {
				targets = append(targets, t)
			}
		}
		excluded := map[string]bool{}
		if ex := yamlMappingValue(rule, "exclude_tenants"); ex != nil && ex.Kind == yaml.SequenceNode {
			for _, item := range ex.Content {
				if item.Kind == yaml.ScalarNode {
					excluded[item.Value] = true
				}
			}
		}
		for _, t := range targets {
			head, _, _ := strings.Cut(t, ".")
			out = append(out, PolicyHead{Head: head, Excluded: excluded})
		}
	}
	return out
}

// NamedByPolicy reports whether a rule of heads that applies to tenant names
// key itself, in either #1231 spelling (CanonicalKeyFor — the one rule every
// reader compares spellings by, Policy-as-Code included, #3556). A wildcard
// head (`mysql_*`) is compared as written, so it names only a key literally
// spelt that way — never every misspelling it would match.
func NamedByPolicy(tenant, key string, heads []PolicyHead) bool {
	c, _ := CanonicalKeyFor(key)
	for _, h := range heads {
		if h.Excluded[tenant] {
			continue
		}
		if hc, _ := CanonicalKeyFor(h.Head); hc == c {
			return true
		}
	}
	return false
}

// yamlMappingValue is the value node of key in mapping m (aliases followed),
// nil when m has no such key; a key present with a null value is a null
// node, not nil (Python's `"policies" in data`).
func yamlMappingValue(m *yaml.Node, key string) *yaml.Node {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if k := m.Content[i]; k.Kind == yaml.ScalarNode && k.Value == key {
			v := m.Content[i+1]
			for v.Kind == yaml.AliasNode && v.Alias != nil {
				v = v.Alias
			}
			return v
		}
	}
	return nil
}

// yamlStringScalar is a `!!str` scalar's text, "" for anything else (Python
// takes only a string target).
func yamlStringScalar(n *yaml.Node) string {
	if n == nil || n.Kind != yaml.ScalarNode || n.ShortTag() != "!!str" {
		return ""
	}
	return n.Value
}
