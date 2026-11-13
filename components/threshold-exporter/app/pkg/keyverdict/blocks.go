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
	// Hint (#3829): a retired threshold key's hint, naming the key that
	// replaces it (Finding's); "" for any other block.
	Hint string
	// Own: File is where the tenant's own entry writes Key (its tenant file,
	// or its entry under a root platform file's `tenants:`) — not a profile
	// or a defaults file it inherits Key from.
	Own bool
	// reading: a value_unread's value as its reader reads it (Finding's);
	// "" for any other kind. Part of what makes two Blocks the same one, so
	// a write replacing one unread value with another makes a new one.
	reading string
}

// own is what makes two Blocks about a tenant's own entry the same one
// (#3016): the tenant and the key, wherever the entry is, so moving a tenant
// to another file is no new block.
func (b Block) own() string { return b.Tenant + "\x00" + b.Key + "\x00" + b.reading }

// at is a Block's place: its file and its key — a finding's own unit, for
// a finding on a file is one per file and key.
func (b Block) at() string { return b.File + "\x00" + b.Key + "\x00" + b.reading }

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
		b := Block{Kind: f.Kind, Key: f.Key, File: deref(f.File), Reason: deref(f.Reason), Hint: deref(f.Hint),
			reading: f.reading}
		if len(f.Tenants) == 0 {
			out = append(out, b)
			continue
		}
		for _, tenant := range f.Tenants {
			b.Tenant, b.Own = tenant, f.own || ownEntry(t, tenant, f.Key, b.File)
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
//
// A Block about a tenant's own entry is old when before had one about that
// tenant and key — in its own entry in any file (the tenant moved), or in
// the same file whatever the entry (a finding merged from a profile and a
// tenant entry in one root platform file reads as either). Any other Block
// (a key a tenant inherits, or one naming no tenant) is old when before had
// one at the same file and key — the unit of a finding on a file, whoever it
// names, so unbinding a profile's last tenant is no new block.
func NewBlocks(before, after Tree, written func(file string) bool) []Block {
	hadAt, hadOwn, hadTenantAt := map[string]bool{}, map[string]bool{}, map[string]bool{}
	for _, b := range Blocks(before) {
		hadAt[b.at()] = true
		hadTenantAt[b.Tenant+"\x00"+b.at()] = true
		if b.Own {
			hadOwn[b.own()] = true
		}
	}
	var out []Block
	for _, b := range Blocks(after) {
		if b.File == "" || !written(b.File) {
			continue
		}
		old := hadAt[b.at()]
		if b.Own {
			old = hadOwn[b.own()] || hadTenantAt[b.Tenant+"\x00"+b.at()]
		}
		if !old {
			out = append(out, b)
		}
	}
	return out
}
