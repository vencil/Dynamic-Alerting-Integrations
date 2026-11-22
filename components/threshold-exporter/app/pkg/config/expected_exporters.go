package config

// `_expected_exporters` (#4097): the exporters a tenant says it runs.
//
// A top-level reserved key whose value is a YAML list of KINDS —
// `_expected_exporters: [mariadb, redis, nginx]`. A kind is a rule pack of
// rule-packs/threshold-registry.yaml that has a presence metric; the table
// of them (expectedExporterKinds) is generated from that file. /metrics
// serves one tenant_expected_exporter{tenant, pack} series per declared
// kind, the left-hand side of TenantExporterAbsent's anti-join, so
// declaring a kind is what turns the absence alert for it on. Absent, null
// or an empty list declares nothing.
//
// The key is layered as every other exporter-read reserved key: a root
// platform file's `tenants.<id>` entry, the tenant's own file over it, a
// profile filling it in when neither writes it — and the value is replaced
// WHOLE, never unioned.
//
// It replaces `_metadata.db_type`, which did this job as a second job of a
// UI grouping field and whose value nothing judged. This reader judges the
// whole value (expectedExportersOf): one entry it does not read and NONE is
// served — an expectation half-read would page for the kinds that happened
// to be spelt right and stay silent for the rest.

import (
	"fmt"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"
)

// expectedExportersKey is the reserved key.
const expectedExportersKey = "_expected_exporters"

// ExpectedExporterKinds is the kinds `_expected_exporters` may name, sorted.
func ExpectedExporterKinds() []string { return sortedCopy(expectedExporterKinds) }

// IsExpectedExporterKind reports whether kind is one of them.
func IsExpectedExporterKind(kind string) bool {
	i := sort.SearchStrings(expectedExporterKinds, kind)
	return i < len(expectedExporterKinds) && expectedExporterKinds[i] == kind
}

// allowedKindsText is the tail every problem ends with.
func allowedKindsText() string {
	return "allowed kinds: " + strings.Join(ExpectedExporterKinds(), ", ")
}

// expectedExportersOf is the one reading of an `_expected_exporters` value:
// the kinds it declares, in the order written, or the reason the exporter
// reads none of it. A problem is a sentence of constants, an index and the
// allowed kinds — never the entry itself (#3703).
func expectedExportersOf(sv ScheduledValue) (kinds []string, problem string) {
	if sv.deleted {
		return nil, ""
	}
	node, structured, err := structureOf(sv)
	if !structured {
		if strings.TrimSpace(sv.Default) == "" {
			return nil, ""
		}
		return nil, "it is not a list: write a YAML list of exporter kinds (" + allowedKindsText() + ")"
	}
	if err != nil || node == nil || node.Kind != yaml.SequenceNode {
		return nil, "it is not a list: write a YAML list of exporter kinds (" + allowedKindsText() + ")"
	}
	seen := make(map[string]bool, len(node.Content))
	for i, entry := range node.Content {
		if entry.Kind != yaml.ScalarNode || entry.Tag != "!!str" {
			return nil, fmt.Sprintf("entry [%d] is not a string: every entry is an exporter kind (%s)", i, allowedKindsText())
		}
		kind := entry.Value
		switch {
		case strings.Contains(kind, "@"):
			return nil, fmt.Sprintf("entry [%d] contains `@`: per-cluster expectations (`kind@cluster`) are not read yet, "+
				"write the kind alone (%s)", i, allowedKindsText())
		case !IsExpectedExporterKind(kind):
			return nil, fmt.Sprintf("entry [%d] is not an exporter kind (%s)", i, allowedKindsText())
		case seen[kind]:
			return nil, fmt.Sprintf("entry [%d] repeats an earlier entry: name each kind once (%s)", i, allowedKindsText())
		}
		seen[kind] = true
		kinds = append(kinds, kind)
	}
	return kinds, ""
}

// expectedExportersProblem is the `expected_exporters` check's problem.
func expectedExportersProblem(_ string, sv ScheduledValue) string {
	_, problem := expectedExportersOf(sv)
	return problem
}

// expectedExportersReading is the check's reading: the declared kinds as a
// set (the series do not depend on the order), and for a value the exporter
// does not read the text itself — two such values are alike only when they
// are the same text (stateFilterValueReading's rule).
func expectedExportersReading(_ string, sv ScheduledValue) any {
	kinds, problem := expectedExportersOf(sv)
	if problem != "" {
		return "unread|raw=" + sv.Default
	}
	return strings.Join(sortedCopy(kinds), ",")
}

// ExpectedExporters is one tenant's declared exporter kinds.
type ExpectedExporters struct {
	Tenant string
	// Kinds: in the order written; never empty.
	Kinds []string
}

// ExpectedExportersOf is the kinds a tenant's merged overrides declare in
// `_expected_exporters`; nil when it declares none or the value is one the
// exporter does not read (the key verdict reports that, in the file that
// writes it).
func ExpectedExportersOf(overrides map[string]ScheduledValue) []string {
	sv, ok := overrides[expectedExportersKey]
	if !ok {
		return nil
	}
	kinds, _ := expectedExportersOf(sv)
	return kinds
}

// ResolveExpectedExporters is every tenant that declares at least one kind,
// sorted by tenant. A tenant that declares none is NOT in it: the opt-in
// that keeps tenant_expected_exporter (and so TenantExporterAbsent) off a
// tenant that never said what it runs.
func (c *ThresholdConfig) ResolveExpectedExporters() []ExpectedExporters {
	var out []ExpectedExporters
	for tenant, overrides := range c.Tenants {
		if kinds := ExpectedExportersOf(overrides); len(kinds) > 0 {
			out = append(out, ExpectedExporters{Tenant: tenant, Kinds: kinds})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Tenant < out[j].Tenant })
	return out
}
