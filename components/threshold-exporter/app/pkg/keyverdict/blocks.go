package keyverdict

// blocks.go — what the gates refuse (ADR-038 decisions 4, 6, 7; #3016): the
// one place that says which severity blocks, which keys a whole-tree gate
// refuses, and when a write makes something new. validate-config, the route
// generator's `--validate` (through `da-guard key-verdict`'s `blocks`) and
// `da-guard write` all read it; none restates it.

import (
	"sort"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// Blocking reports whether a finding of severity blocks: an error does; a
// warning (a noop `disable`) and a notice (an undeclared state filter) are
// reported only.
func Blocking(severity string) bool { return severity == SeverityError }

// Block is one thing a gate refuses: a blocking finding, once per tenant it
// names (Tenant ""  for one that names none, a profile no tenant binds), or a
// key the verdict could not judge (Kind Undecidable: no root platform file
// was read, decision 7).
type Block struct {
	Kind   string
	Key    string
	File   string // the finding's file; "" when it has none
	Tenant string
	Reason string
	// Own: File is where the tenant's own entry writes Key (its tenant file,
	// or its entry under a root platform file's `tenants:`) — not a profile
	// or a defaults file it inherits Key from.
	Own bool
}

// identity is what makes two Blocks of two verdicts the same one (#3016):
//   - one about a tenant's own entry: the tenant and the key, wherever the
//     entry is, so moving a tenant to another file is no new block;
//   - one about a file the tenant inherits from: the tenant, the key and that
//     file, so a profile's new misspelling is new though the tenant's own
//     file has the same one;
//   - one about no tenant: the file and the key.
func (b Block) identity() string {
	switch {
	case b.Tenant == "":
		return "f\x00" + b.File + "\x00" + b.Key
	case b.Own:
		return "o\x00" + b.Tenant + "\x00" + b.Key
	}
	return "i\x00" + b.Tenant + "\x00" + b.Key + "\x00" + b.File
}

// ownEntry reports whether file is where tenant's own entry writes key.
func ownEntry(t Tree, tenant, key, file string) bool {
	for _, v := range t.Keys[tenant] {
		if v.Key == key && deref(v.File) == file && v.Layer != nil &&
			(*v.Layer == config.KeyLayerTenant || *v.Layer == config.KeyLayerPlatform) {
			return true
		}
	}
	return false
}

// Blocks is every Block of t, sorted by file, tenant, key.
func Blocks(t Tree) []Block {
	var out []Block
	for _, f := range t.Findings {
		if !f.Blocks {
			continue
		}
		b := Block{Kind: f.Kind, Key: f.Key, File: deref(f.File), Reason: deref(f.Reason)}
		if len(f.Tenants) == 0 {
			out = append(out, b)
			continue
		}
		for _, tenant := range f.Tenants {
			b.Tenant, b.Own = tenant, ownEntry(t, tenant, f.Key, b.File)
			out = append(out, b)
		}
	}
	for _, tenant := range sortedKeys(t.Keys) {
		for _, v := range t.Keys[tenant] {
			if v.Verdict == Undecidable {
				out = append(out, Block{Kind: Undecidable, Key: v.Key, File: deref(v.File), Tenant: tenant,
					Reason: Undecidable, Own: ownEntry(t, tenant, v.Key, deref(v.File))})
			}
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.File != b.File {
			return a.File < b.File
		}
		if a.Tenant != b.Tenant {
			return a.Tenant < b.Tenant
		}
		return a.Key < b.Key
	})
	return out
}

// NewBlocks is the Blocks of after that a write adds (decision 6): those in
// a file the write writes (written) that before did not have. One the tree
// already had is not the write's: it can fix the tree, never make it worse.
// Before had a Block when one of its Blocks has the same identity — or, for
// one about no tenant, when before had any Block on that file and key: the
// write unbound the last tenant of a profile whose key was already wrong.
func NewBlocks(before, after Tree, written func(file string) bool) []Block {
	had, hadFile := map[string]bool{}, map[string]bool{}
	for _, b := range Blocks(before) {
		had[b.identity()] = true
		hadFile[b.File+"\x00"+b.Key] = true
	}
	var out []Block
	for _, b := range Blocks(after) {
		if b.File == "" || !written(b.File) || had[b.identity()] {
			continue
		}
		if b.Tenant == "" && hadFile[b.File+"\x00"+b.Key] {
			continue
		}
		out = append(out, b)
	}
	return out
}
