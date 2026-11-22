package config

// `_expected_exporters` (#4097): the exporters a tenant says it runs.
//
// A top-level reserved key whose value is a YAML list of entries —
// `_expected_exporters: [mariadb@east, mariadb@west, redis]`. An entry is a
// KIND, or `kind@cluster`. A kind is a rule pack of
// rule-packs/threshold-registry.yaml that has a presence metric; the table
// of them (expectedExporterKinds) is generated from that file. /metrics
// serves one tenant_expected_exporter series per entry, the left-hand side
// of TenantExporterAbsent's anti-join, so declaring is what turns the
// absence alert on. Absent, null or an empty list declares nothing.
//
// The two forms ask two questions (ADR-006 contract 6). A kind alone: is an
// exporter of this kind answering SOMEWHERE — one cluster reporting is
// enough. `kind@cluster`: is one answering IN THAT CLUSTER, the cluster
// being the `da_cluster` of the Prometheus its series come through. One
// value asks one of them per kind: a kind written both ways is refused,
// since the bare entry would be satisfied by the very cluster the other
// entry says must not be enough. Whether a named cluster exists is not
// judged here — nothing in a conf.d tree lists the clusters; a cluster
// nobody reports from shows as TenantExporterAbsent naming it.
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

// ExpectedExporter is one entry of `_expected_exporters`.
type ExpectedExporter struct {
	// Kind: the exporter kind (a rule pack with a presence metric).
	Kind string
	// Cluster: the `da_cluster` the kind must report from; "" for a kind
	// written alone (reporting from any cluster is enough).
	Cluster string
}

// String is the entry as it is written: `kind` or `kind@cluster`.
func (e ExpectedExporter) String() string {
	if e.Cluster == "" {
		return e.Kind
	}
	return e.Kind + expectedClusterSep + e.Cluster
}

// expectedClusterSep parts an entry's kind from its cluster.
const expectedClusterSep = "@"

// ExpectedExporterKind is the kind of an entry as written (`mariadb@east` →
// `mariadb`): what anything that filters or groups BY KIND compares. It
// does not judge the entry.
func ExpectedExporterKind(entry string) string {
	kind, _, _ := strings.Cut(entry, expectedClusterSep)
	return kind
}

// expectedClusterMaxLen bounds a cluster id; clusterIDRuleText says the
// rule isExpectedClusterID holds, for the problem sentence.
const (
	expectedClusterMaxLen = 63
	clusterIDRuleText     = "1 to 63 characters of ASCII letters, digits, `.`, `_` and `-`, the first and the last a letter or a digit"
)

// isExpectedClusterID reports whether id is a cluster id the exporter will
// put in a label and the rules will match on:
// ^[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$. The repo has no other
// rule for a `da_cluster` value (the scrape-config lint and
// federation-check only ask that one is set), so this is the narrow one: no
// space, quote, brace or regex metacharacter other than `.`, which cannot
// reach a matcher — the rules compare the label by equality.
func isExpectedClusterID(id string) bool {
	if id == "" || len(id) > expectedClusterMaxLen {
		return false
	}
	alnum := func(c byte) bool {
		return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9'
	}
	if !alnum(id[0]) || !alnum(id[len(id)-1]) {
		return false
	}
	for i := 1; i < len(id)-1; i++ {
		if c := id[i]; !alnum(c) && c != '.' && c != '_' && c != '-' {
			return false
		}
	}
	return true
}

// expectedExportersOf is the one reading of an `_expected_exporters` value:
// the entries it declares, in the order written, or the reason the exporter
// reads none of it. A problem is a sentence of constants, indexes and the
// allowed kinds — never the entry itself (#3703).
func expectedExportersOf(sv ScheduledValue) (entries []ExpectedExporter, problem string) {
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
	seen := make(map[ExpectedExporter]bool, len(node.Content))
	// firstOf: the index of a kind's first entry, and whether that entry
	// names a cluster — every later entry of the kind must be the same form.
	type first struct {
		index       int
		withCluster bool
	}
	firstOf := make(map[string]first, len(node.Content))
	for i, item := range node.Content {
		if item.Kind != yaml.ScalarNode || item.Tag != "!!str" {
			return nil, fmt.Sprintf("entry [%d] is not a string: every entry is an exporter kind or `kind@cluster` (%s)", i, allowedKindsText())
		}
		kind, cluster, withCluster := strings.Cut(item.Value, expectedClusterSep)
		entry := ExpectedExporter{Kind: kind, Cluster: cluster}
		switch {
		case strings.Contains(cluster, expectedClusterSep):
			return nil, fmt.Sprintf("entry [%d] has more than one `@`: an entry is an exporter kind or `kind@cluster` (%s)", i, allowedKindsText())
		case !IsExpectedExporterKind(kind):
			return nil, fmt.Sprintf("entry [%d] is not an exporter kind (%s)", i, allowedKindsText())
		case withCluster && !isExpectedClusterID(cluster):
			return nil, fmt.Sprintf("entry [%d] names a cluster after `@` that is not a cluster id (%s): "+
				"write `kind@cluster` with the cluster's `da_cluster` value, or the kind alone (%s)", i, clusterIDRuleText, allowedKindsText())
		case seen[entry]:
			return nil, fmt.Sprintf("entry [%d] repeats an earlier entry: write each entry once (%s)", i, allowedKindsText())
		}
		if f, ok := firstOf[kind]; !ok {
			firstOf[kind] = first{index: i, withCluster: withCluster}
		} else if f.withCluster != withCluster {
			return nil, fmt.Sprintf("entries [%d] and [%d] name the same kind, one with `@cluster` and one without: "+
				"the kind alone is satisfied by an exporter in any cluster, `kind@cluster` asks for that cluster — "+
				"write the kind alone, or one `kind@cluster` entry for each cluster (%s)", f.index, i, allowedKindsText())
		}
		seen[entry] = true
		entries = append(entries, entry)
	}
	return entries, ""
}

// entryStrings is entries as they are written.
func entryStrings(entries []ExpectedExporter) []string {
	if len(entries) == 0 {
		return nil
	}
	out := make([]string, len(entries))
	for i, e := range entries {
		out[i] = e.String()
	}
	return out
}

// expectedExportersProblem is the `expected_exporters` check's problem.
func expectedExportersProblem(_ string, sv ScheduledValue) string {
	_, problem := expectedExportersOf(sv)
	return problem
}

// expectedExportersReading is the check's reading: the declared entries as a
// set (the series do not depend on the order), and for a value the exporter
// does not read the text itself — two such values are alike only when they
// are the same text (stateFilterValueReading's rule).
func expectedExportersReading(_ string, sv ScheduledValue) any {
	entries, problem := expectedExportersOf(sv)
	if problem != "" {
		return "unread|raw=" + sv.Default
	}
	return strings.Join(sortedCopy(entryStrings(entries)), ",")
}

// ExpectedExporters is one tenant's declared exporters.
type ExpectedExporters struct {
	Tenant string
	// Entries: in the order written; never empty.
	Entries []ExpectedExporter
}

// ExpectedExporterEntriesOf is the entries a tenant's merged overrides
// declare in `_expected_exporters`; nil when it declares none or the value
// is one the exporter does not read (the key verdict reports that, in the
// file that writes it).
func ExpectedExporterEntriesOf(overrides map[string]ScheduledValue) []ExpectedExporter {
	sv, ok := overrides[expectedExportersKey]
	if !ok {
		return nil
	}
	entries, _ := expectedExportersOf(sv)
	return entries
}

// ExpectedExportersOf is ExpectedExporterEntriesOf with each entry as it is
// written (`mariadb`, `mariadb@east`) — what every surface that shows or
// serves the declaration carries. A consumer that compares by kind takes
// ExpectedExporterKind of an entry.
func ExpectedExportersOf(overrides map[string]ScheduledValue) []string {
	return entryStrings(ExpectedExporterEntriesOf(overrides))
}

// ResolveExpectedExporters is every tenant that declares at least one entry,
// sorted by tenant. A tenant that declares none is NOT in it: the opt-in
// that keeps tenant_expected_exporter (and so TenantExporterAbsent) off a
// tenant that never said what it runs.
func (c *ThresholdConfig) ResolveExpectedExporters() []ExpectedExporters {
	var out []ExpectedExporters
	for tenant, overrides := range c.Tenants {
		if entries := ExpectedExporterEntriesOf(overrides); len(entries) > 0 {
			out = append(out, ExpectedExporters{Tenant: tenant, Entries: entries})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Tenant < out[j].Tenant })
	return out
}
