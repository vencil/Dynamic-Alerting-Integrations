package config

// The `dimensions:` declaration (ADR-038 §11, #3475): which labels a
// dimensional key of a base threshold may carry and still be served.
//
// The shipped rule packs normalise thresholds `max by(tenant)` and drop every
// label but the ones they group by, so a dimensional key whose label no rule
// keeps would be served and then merged into one tenant-wide threshold — a
// setting that does nothing it claims to. The platform therefore names, per
// base, the labels its rules read (the shipped list is generated from the
// threshold registry, pinned to the packs by a test), and the exporter serves
// a dimensional key only when every label of its selector is named. Anything
// else has no row and is unconsumed: the shared key verdict reads it unread.

import (
	"sort"
	"strings"
)

// unionLabels is a ∪ b, sorted, without duplicates.
func unionLabels(a, b []string) []string {
	seen := make(map[string]struct{}, len(a)+len(b))
	for _, l := range a {
		seen[l] = struct{}{}
	}
	for _, l := range b {
		seen[l] = struct{}{}
	}
	out := make([]string, 0, len(seen))
	for l := range seen {
		out = append(out, l)
	}
	sort.Strings(out)
	return out
}

// canonicalDimensionBase is a declared base in its canonical (#1231)
// spelling, so a declaration under a retired spelling names the same base.
func canonicalDimensionBase(base string) string {
	if canon, ok := canonicalKeyFor(base); ok {
		return canon
	}
	return base
}

// dimensionDeclared reports whether every label of dimensional key's
// selector is declared under `dimensions:` for its base (either #1231
// spelling, on both sides). A key with no parsable label is not declared.
func dimensionDeclared(key string, dims map[string][]string) bool {
	i := strings.Index(key, "{")
	if i <= 0 || !strings.HasSuffix(key, "}") {
		return false
	}
	base := canonicalDimensionBase(key[:i])
	declared := map[string]struct{}{}
	for b, labels := range dims {
		if canonicalDimensionBase(b) != base {
			continue
		}
		for _, l := range labels {
			declared[l] = struct{}{}
		}
	}
	exact, regex := parseLabelsStringWithOp(key[i+1 : len(key)-1])
	if len(exact)+len(regex) == 0 {
		return false
	}
	for _, m := range []map[string]string{exact, regex} {
		for l := range m {
			if _, ok := declared[l]; !ok {
				return false
			}
		}
	}
	return true
}
