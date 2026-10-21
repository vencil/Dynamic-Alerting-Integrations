// Package confdname is the ONE copy of "what does this conf.d name mean" in
// Go (ADR-036 §2, §7 step 9; #3206). The exporter's walker
// (pkg/config.ScanDirTree), the in-memory source behind /simulate
// (pkg/config.ScanFromConfigSource), da-guard (parse-tree and the routing
// evidence), the batch-PR allocator, the profile emitter and tenant-api (its
// tenant enumerators, write paths and refusal disclosure) all ask it; none of
// them restates a rule. It lives under pkg/ so tenant-api, a separate module,
// can import it.
//
// ⛔ WHY ONE COPY. The #1911 family is one shape repeated: a single conf.d
// tree, several enumerators, each with its own hand-written name rules, quietly
// disagreeing. #1605 measured the write plane's copy — `internal/batchpr`'s
// allocator answered differently from the exporter's walker on eleven of the
// shared matrix's twenty-three names, four of them silently. Until #3206 there
// were still four Go copies (this package, two in pkg/config, one in
// tenant-api) kept equal only by each asserting the same JSON matrix.
//
// The authority for the NAME rules is the exporter's production hot-reload
// walker, which decides which files are read at all — and that walker now
// calls this package, so the rule and its authority are the same code.
// ⛔ Nothing here answers "who is being served": the walker takes tenant
// IDENTITY from the `tenants:` keys inside each document, never from a
// filename. TenantNamedBy is the write plane's naming convention, not the
// exporter's tenant set.
//
// A reader outside Go (the Python tools) gets these answers from
// `da-guard parse-tree --list-only`, not from a second implementation.
package confdname

import (
	"path/filepath"
	"strings"
)

// yamlSuffixes are the two carrier spellings the exporter's walker accepts.
// Both ship — `conf.d/db-b.yml` sits next to `conf.d/db-a.yaml`.
var yamlSuffixes = [...]string{".yaml", ".yml"}

// SplitCarrier reports whether `base` carries a YAML extension the exporter
// would accept, and returns the stem with its ORIGINAL case intact.
//
// ⛔ The fold is done on the last few ASCII bytes with EqualFold, NOT by
// lowercasing `base` and slicing that. `strings.ToLower` is not
// length-preserving, so an offset computed against a lowercased copy indexes
// the wrong bytes of the original. Measured in Go, on this toolchain:
//
//	strings.ToLower("İSTANBUL") == "istanbul"   // 9 bytes -> 8
//	strings.ToLower("İ")        == "i"          // 2 bytes -> 1
//
// and the failure that follows from slicing on that offset:
//
//	base := "İ.yaml"; lower := strings.ToLower(base)
//	base[:len(lower)-len(".yaml")]  ==  "\xc4"   // a truncated half-rune,
//	                                             // not valid UTF-8
//
// ⛔ AN EARLIER VERSION OF THIS COMMENT GOT THE MECHANISM BACKWARDS AND SAID SO
// UNDER THE WORD "MEASURED": it claimed `İSTANBUL` lowercases to `i̇stanbul`,
// one rune LONGER, with U+0130 becoming `i` + U+0307. That is Python's result
// (`str.lower` applies full case mapping); Go's `strings.ToLower` uses simple
// case mapping and the string gets SHORTER. The design conclusion survived, by
// the opposite mechanism — which is precisely why a claim carrying the word
// "measured" has to be measured in the language it is written in.
//
// ⛔ And the stem keeps its case. `MiXeD.YmL` is tenant `MiXeD`; deriving the
// id from a lowercased copy would rename that tenant on the write plane while
// the exporter keeps serving it under whatever its `tenants:` key says — a
// second divergence built by the fix for the first.
// ⛔ The length test is `>=`, not `>`. A file literally named `.yaml` DOES
// carry a YAML extension — the shared matrix says so (`yaml_extension: true`)
// — and it is excluded from being a tenant carrier by being HIDDEN, not by
// pretending it has no extension. Measured: with `>` this predicate was the
// one cell out of twenty-three where this package disagreed with the matrix,
// and the routing-level test could not see it because the name still ended up
// dropped, just with a reason that told the operator their `.yaml` file has no
// YAML extension.
func SplitCarrier(base string) (stem string, ok bool) {
	for _, ext := range yamlSuffixes {
		if len(base) >= len(ext) && strings.EqualFold(base[len(base)-len(ext):], ext) {
			return base[:len(base)-len(ext)], true
		}
	}
	return "", false
}

// IsDefaults reports whether `base` is the inheritance-chain carrier.
//
// ⛔ The comparison is against the two whole literals, not a `_defaults`
// PREFIX. `_defaults-multidb.yaml` was a real file in this repo
// (`conf.d/examples/`, until #3014) that is NOT the chain carrier; a prefix implementation
// passes every other name and was measured (#1588) to make `describe_tenant`
// reproduce the exporter's merged hash on only three of five shipped tenants.
//
// ⛔ And the comparison is `strings.ToLower` + `==`, NOT `strings.EqualFold` (#1670).
// The two are different relations: EqualFold is Unicode SIMPLE CASE FOLDING,
// which puts U+017F `ſ` (LATIN SMALL LETTER LONG S) in the same fold orbit as
// `s` and `S`, while ToLower leaves `ſ` alone because it is already lowercase.
// The exporter's hot-reload walker and the shared matrix's
// `defaults_file` definition ("name lowercased is exactly …") are both the
// ToLower relation. Measured on this toolchain:
//
//	strings.EqualFold("_defaultſ.yaml", "_defaults.yaml")       == true
//	strings.ToLower("_defaultſ.yaml") == "_defaults.yaml"       == false
//
// With EqualFold here, `_defaultſ.yaml` was the Base-PR chain carrier on the
// write plane while the exporter treats it as an ordinary reserved file whose
// `defaults:` block it never merges. How many names that is depends on the
// search, so both measurements are stated with their scope:
//
//   - substituting ONE non-ASCII rune for one byte of a defaults literal:
//     exactly two names, `_defaultſ.yaml` / `_defaultſ.yml`;
//   - every combination over each character's full fold orbit (`s`/`S`/`ſ`,
//     `a`/`A`, …): 3072 names (2048 `.yaml`, 1024 `.yml`), e.g.
//     `_DEFAULTſ.YAML`. Every one of them has `ſ` in the `s` position — the
//     only non-ASCII member of any orbit here — so the two matrix rows
//     exercise the one character that separates the relations.
//
// SplitCarrier keeps EqualFold, and there the two relations cannot differ:
// no code point other than the ASCII letters themselves lowercases to, or
// shares a fold orbit with, any byte of `.yaml` / `.yml`
// (TestExtensionBytesHaveNoNonASCIIFoldImage sweeps the whole range). That is
// why the walker's former `HasSuffix(ToLower(name), …)` and tenant-api's
// former copy of it could be replaced by SplitCarrier without changing an
// answer (#3206).
func IsDefaults(base string) bool {
	lower := strings.ToLower(base)
	return lower == "_defaults.yaml" || lower == "_defaults.yml"
}

// IsHidden reports whether the exporter's walker skips `base` outright: a
// `.`-prefixed file is not read, a `.`-prefixed directory is pruned whole.
//
// Byte prefix, no folding: no code point's lower- or upper-case image starts
// with '.' (or '_') unless it already is that byte, so folding could not
// change an answer (TestPrefixesNeedNoCaseFolding sweeps the whole range).
func IsHidden(base string) bool { return strings.HasPrefix(base, ".") }

// IsScanned reports whether the walker reads a FILE with this base name: not
// hidden, and a `.yaml` / `.yml` extension in any case. `_`-prefixed names
// are scanned (they are platform files). Whether an entry is a file at all is
// the caller's question — a name cannot answer it.
func IsScanned(base string) bool {
	_, ok := SplitCarrier(base)
	return ok && !IsHidden(base)
}

// HasHiddenSegment reports whether a directory or file on the clean,
// root-relative slash path `rel` is hidden — the walker prunes a hidden
// directory with everything under it, so `.cache/deep/x.yaml` is not read
// whatever `x.yaml` is called. A `.` segment is the current directory, not a
// hidden name; `..` is (it also starts with '.', and a path leaving the root
// is read by nothing). `rel == ""` (the root itself, never pruned) is false.
func HasHiddenSegment(rel string) bool {
	for _, seg := range strings.Split(rel, "/") {
		if seg != "." && IsHidden(seg) {
			return true
		}
	}
	return false
}

// IsScannedPath reports whether the walker reads the file at `rel`, a clean
// path relative to the conf.d root (either separator): no segment on it is
// hidden and IsScanned holds for its base name.
func IsScannedPath(rel string) bool {
	rel = filepath.ToSlash(rel)
	return !HasHiddenSegment(rel) && IsScanned(rel[strings.LastIndexByte(rel, '/')+1:])
}

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
// keys INSIDE the document (the hot-reload walker's `for tid := range
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
