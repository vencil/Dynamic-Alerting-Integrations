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

// DimensionBaseRefused reports whether a `dimensions:` base (in either #1231
// spelling, which share the suffix) names a `<base>_critical` tier rather than a base (#3556). The
// exporter reads every `<base>_critical` key as the severity=critical tier
// of `<base>`, so a dimensional `<base>_critical{…}` is no tier: when it was
// declared it was served as a metric of its own, `<base>_critical` at
// severity=warning, which no rule pack reads and which has no #1231 twin.
// A per-dimension critical threshold is `<base>{label="v"}: "N:critical"`.
// So such a base declares nothing (dimensionDeclared), the root declaration
// is an error (da-guard's root_dimensions_critical_base) and the build logs
// it.
func DimensionBaseRefused(base string) bool {
	b, ok := strings.CutSuffix(base, criticalSuffix)
	return ok && b != ""
}

// RegexLabelSuffix is appended to a regex selector's label name on the
// /metrics series (`x{db=~"a.*"}` serves `db_re="a.*"`): the label a rule
// would group by, so the one `dimensions:` must name.
const RegexLabelSuffix = "_re"

// dimensionDeclared reports whether every label the dimensional key's row
// SERVES is declared under `dimensions:` for its base (either #1231
// spelling, on both sides): an exact matcher serves its own name, a regex
// matcher `<label>_re` (RegexLabelSuffix) — `version=~"v3.*"` is served as
// `version_re`, which a rule grouping by `version` folds into its
// version-less group. A key with no parsable label is not declared, nor is
// a key whose base is a `_critical` tier (DimensionBaseRefused).
func dimensionDeclared(key string, dims map[string][]string) bool {
	i := strings.Index(key, "{")
	if i <= 0 || !strings.HasSuffix(key, "}") {
		return false
	}
	base := canonicalDimensionBase(key[:i])
	if DimensionBaseRefused(base) {
		return false // #3556: a `_critical` tier is no base
	}
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
	for l := range exact {
		if _, ok := declared[l]; !ok {
			return false
		}
	}
	for l := range regex {
		if _, ok := declared[l+RegexLabelSuffix]; !ok {
			return false
		}
	}
	return true
}
