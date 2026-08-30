// Package confdname holds the ONE copy of "what does this conf.d basename
// mean" that the exporter's Go-side write plane is allowed to have.
//
// ⛔ WHY IT EXISTS. The #1339 family is one shape repeated: a single conf.d
// tree, several enumerators, each with its own hand-written name rules, quietly
// disagreeing. #1605 measured the write plane's copy — `internal/batchpr`'s
// allocator answered differently from the exporter's walker on eleven of the
// shared matrix's twenty-three names, four of them silently. The moment a
// SECOND Go component needed the same rules (`internal/profile`'s emitter, so
// it can tell whether the carrier name it is about to write reads back as the
// tenant it is for), writing them out a second time would have been the family
// reproducing itself inside one binary. So the rule lives here once.
//
// The authority for the NAME rules is
// `components/threshold-exporter/app/config_hierarchy.go` — the production
// hot-reload scanner, which decides which files are read at all. ⛔ Its name
// rules are the only thing restated here: that scanner takes tenant IDENTITY
// from the `tenants:` keys inside each document, never from a filename, so
// nothing in this package answers "who is being served". The agreement is pinned
// against `tests/shared/confd_name_classification_matrix.json` by
// internal/batchpr/confd_name_classification_parity_test.go, which drives the
// allocator that these predicates back.
//
// ⛔ No function here reads another language's source text. This repo measured
// (#1448) that a guard asserting things about another implementation's source
// goes red on legitimate refactors and states the opposite of the truth when it
// does. The shared matrix is the contract; every consumer asserts that.
package confdname

import "strings"

// yamlSuffixes are the two carrier spellings the exporter's walker accepts.
// Both ship — `conf.d/db-b.yml` sits next to `conf.d/db-a.yaml`.
var yamlSuffixes = [...]string{".yaml", ".yml"}

// SplitCarrier reports whether `base` carries a YAML extension the exporter
// would accept, and returns the stem with its ORIGINAL case intact.
//
// ⛔ The fold is done on the last few ASCII bytes with EqualFold, NOT by
// lowercasing `base` and slicing that. `strings.ToLower` is not
// length-preserving — measured in this family: `"İSTANBUL"` lowercases to
// `"i̇stanbul"` (U+0130 becomes `i` + U+0307), one rune longer — so an offset
// computed against a lowercased copy indexes the wrong bytes of the original.
//
// ⛔ And the stem keeps its case. `MiXeD.YmL` is tenant `MiXeD`; deriving the
// id from a lowercased copy would rename that tenant on the write plane while
// the exporter keeps serving it under whatever its `tenants:` key says — a
// second divergence built by the fix for the first.
func SplitCarrier(base string) (stem string, ok bool) {
	for _, ext := range yamlSuffixes {
		if len(base) > len(ext) && strings.EqualFold(base[len(base)-len(ext):], ext) {
			return base[:len(base)-len(ext)], true
		}
	}
	return "", false
}

// IsDefaults reports whether `base` is the inheritance-chain carrier.
//
// ⛔ The comparison is against the two whole literals, not a `_defaults`
// PREFIX. `_defaults-multidb.yaml` is a real file in this repo
// (`conf.d/examples/`) that is NOT the chain carrier; a prefix implementation
// passes every other name and was measured (#1588) to make `describe_tenant`
// reproduce the exporter's merged hash on only three of five shipped tenants.
func IsDefaults(base string) bool {
	return strings.EqualFold(base, "_defaults.yaml") || strings.EqualFold(base, "_defaults.yml")
}

// IsHidden reports whether the exporter's walker skips `base` outright.
func IsHidden(base string) bool { return strings.HasPrefix(base, ".") }

// IsReserved reports whether `base` uses the reserved `_` prefix. Reserved
// names are hashed for change detection but yield no tenant; IsDefaults is the
// one reserved sub-case that carries meaning.
func IsReserved(base string) bool { return strings.HasPrefix(base, "_") }

// TenantNamedBy returns the tenant id a carrier named `base` is NAMED FOR, and
// whether `base` is a tenant carrier at all.
//
// The predicate half is the `tenants` projection of the shared matrix restated:
// yaml_extension AND NOT reserved_prefix AND NOT hidden.
//
// ⛔ THIS IS NOT "WHO THE EXPORTER SERVES OUT OF THIS FILE", and must never be
// used to answer that. The exporter takes tenant IDENTITY from the `tenants:`
// keys INSIDE the document (`config_hierarchy.go`: `for tid := range
// doc.Tenants`) — measured, there is no place in the exporter that derives a
// tenant id from a filename. A basename only decides CLASSIFICATION: is this
// file read at all, and is it the chain carrier.
//
// What this function answers is the WRITE PLANE's naming convention: the
// proposal emitter names each tenant's carrier `safeFilename(id)+".yaml"` and
// the batch-PR allocator recovers the id back out of that name. Those two are
// the round trip that needs checking, and this is the rule they share. A
// carrier whose filename and whose `tenants:` key disagree is legal on disk and
// the exporter will happily serve the key — it is the PR routing that loses it.
func TenantNamedBy(base string) (tenant string, ok bool) {
	stem, isYAML := SplitCarrier(base)
	if !isYAML || stem == "" || IsHidden(base) || IsReserved(base) {
		return "", false
	}
	return stem, true
}
