package keyverdict

// blocks.go — what the gates refuse (ADR-038 decisions 4, 6, 7; #3016): the
// one place that says which severity blocks, which keys a whole-tree gate
// refuses, and when a write makes something new. validate-config, the route
// generator's `--validate` (through `da-guard key-verdict`'s `blocks`) and
// `da-guard write` all read it; none restates it.

import "sort"

// Blocking reports whether a finding of severity blocks: an error does; a
// warning (a noop `disable`) and a notice (an undeclared state filter) are
// reported only.
func Blocking(severity string) bool { return severity == SeverityError }

// Block is one thing a gate refuses: a blocking finding, once per tenant it
// names (Tenant ""  for one that names none, a profile no tenant binds), or a
// key the verdict could not judge (Kind Undecidable: no root platform file
// was read, decision 7 — the writers refuse it too).
type Block struct {
	Kind   string
	Key    string
	File   string // the finding's file; "" when it has none
	Tenant string
	Reason string
}

// identity is what makes two Blocks of two verdicts the same one: the
// tenant and the key for a block about a tenant — wherever the file is, so
// moving a tenant to another file, or renaming the profile it binds, is no
// new block — and the file and the key for one about none.
func (b Block) identity() string {
	if b.Tenant != "" {
		return "t\x00" + b.Tenant + "\x00" + b.Key
	}
	return "f\x00" + b.File + "\x00" + b.Key
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
			b.Tenant = tenant
			out = append(out, b)
		}
	}
	for _, tenant := range sortedKeys(t.Keys) {
		for _, v := range t.Keys[tenant] {
			if v.Verdict == Undecidable {
				out = append(out, Block{Kind: Undecidable, Key: v.Key, File: deref(v.File), Tenant: tenant,
					Reason: Undecidable})
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
// a file the write writes (written) whose identity no Block of before has.
// One the tree already had — anywhere — is not the write's: it can fix the
// tree, never make it worse.
func NewBlocks(before, after Tree, written func(file string) bool) []Block {
	had := map[string]bool{}
	for _, b := range Blocks(before) {
		had[b.identity()] = true
	}
	var out []Block
	for _, b := range Blocks(after) {
		if b.File != "" && written(b.File) && !had[b.identity()] {
			out = append(out, b)
		}
	}
	return out
}
