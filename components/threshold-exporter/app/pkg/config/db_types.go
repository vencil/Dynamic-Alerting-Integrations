package config

// `_metadata.db_type` as a list (#3771).
//
// A tenant can run more than one kind of database exporter (mariadb and
// redis behind two Services). Liveness (TenantExporterAbsent) needs one
// expectation per kind, so `db_type` is read as a list: a scalar is a
// one-element list (the pre-#3771 form keeps its meaning), a sequence is
// the list itself. The schema (tenant-config.schema.json) accepts both
// spellings through oneOf and refuses an empty or repeated list; the
// reader here is lenient where the schema is strict — it drops empty
// entries and repeats, keeping first-seen order — because a repeated
// db_type would otherwise make the collector emit the same
// tenant_expected_exporter series twice, which fails the scrape.
//
// Layering is unchanged: `_metadata` merges PER KEY and a value is
// REPLACED WHOLE (metadata_layers.go), so a tenant file writing
// `db_type: [redis]` over a platform layer's `db_type: [mariadb]` reads
// `[redis]` — the lists are not unioned. The contract is the schema's: a
// layer writes a string or a non-empty list. An empty list is refused there;
// what this reader makes of one is not a contract and is not tested.

import (
	"fmt"

	"gopkg.in/yaml.v3"
)

// DBTypes is `_metadata.db_type`: the database kinds a tenant declares,
// deduplicated, in the order written. nil means "not declared".
type DBTypes []string

// UnmarshalYAML accepts a scalar (one kind; "" and null are none) or a
// sequence of scalars. Any other shape is an error, which fails the
// `_metadata` decode as a mistyped field always has.
func (d *DBTypes) UnmarshalYAML(node *yaml.Node) error {
	if node.Kind == yaml.AliasNode && node.Alias != nil {
		node = node.Alias
	}
	var raw []string
	switch node.Kind {
	case yaml.ScalarNode:
		if node.Tag == "!!null" {
			*d = nil
			return nil
		}
		raw = []string{node.Value}
	case yaml.SequenceNode:
		if err := node.Decode(&raw); err != nil {
			return err
		}
	default:
		return fmt.Errorf("line %d: db_type: want a string or a list of strings", node.Line)
	}
	*d = NormalizeDBTypes(raw)
	return nil
}

// NormalizeDBTypes drops empty entries and repeats, keeping first-seen
// order. nil when nothing is left.
func NormalizeDBTypes(in []string) DBTypes {
	var out DBTypes
	seen := make(map[string]bool, len(in))
	for _, v := range in {
		if v == "" || seen[v] {
			continue
		}
		seen[v] = true
		out = append(out, v)
	}
	return out
}

// Has reports whether kind is one of the declared kinds.
func (d DBTypes) Has(kind string) bool {
	for _, v := range d {
		if v == kind {
			return true
		}
	}
	return false
}
