package main

// undetermined: what in a conf.d tree only the route generator can judge
// (#2904, ADR-036 §9 step 2). da-guard's default check no longer judges
// routing or domain policy; when the tree holds anything of that plane, the
// report says so — "undetermined" — and the run ends in exit 4 unless 3 or 1
// applies, so "not judged" is never read as "judged, nothing wrong".
//
// ⛔ ONE DEFINITION, AND NO FILE RULES OF ITS OWN. Which files the generator
// reads, and what each holds, is parse-tree's answer (parseTreeOf over the
// exporter's walk, as `da-guard parse-tree` prints it); which file names are
// domain policy / routing profiles files is confdlayout.IsDomainPolicyFile
// and routingProfilesNames, the names parse-tree already uses. This file only
// says which of those answers is a generator-only verdict.
//
// ⛔ THE WHOLE TREE, whatever --scope says: the generator refuses the whole
// tree on a blocking shape anywhere in it, as the routing loader da-guard used
// to run read it (#2326).
//
// ⛔ FAIL TOWARD UNDETERMINED. A spelling the generator would ignore still
// counts (`_routing_profle`), as does a tree the walk cannot list: reporting 4
// for a tree the generator would pass costs one generator run; reporting 0
// for one it refuses is the silent gap this exit code exists to close.

import (
	"fmt"
	"io"
	"log"
	"path"
	"sort"
	"strconv"
	"strings"

	"github.com/vencil/threshold-exporter/internal/confdlayout"
	"github.com/vencil/threshold-exporter/internal/confdname"
	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/parsetree"
	"github.com/vencil/threshold-exporter/pkg/tenantid"
)

// Closed set of undeterminedItem.Reason values (docs/cli-reference.md §guard).
const (
	// undeterminedRoutingKey: a mapping key, at any depth of a file the
	// generator reads, that config.IsRoutingLayerKey accepts (`_routing*`,
	// misspellings included, and `_severity_dedup`), or a `routing_profiles`
	// key. Field is the key's dotted path.
	undeterminedRoutingKey = "routing_key"
	// undeterminedDomainPolicy: a `_domain_policy.yaml` / `.yml` file, any
	// level. Field is "".
	undeterminedDomainPolicy = "domain_policy"
	// undeterminedRoutingProfilesFile: a `_routing_profiles.yaml` / `.yml`
	// file, any level. Field is "".
	undeterminedRoutingProfilesFile = "routing_profiles_file"
	// undeterminedGeneratorUnreadable: a file parse-tree names unreadable,
	// whether or not the exporter reads it. Before ADR-036 step 4 the
	// generator (PyYAML) does not necessarily refuse such a file; from
	// step 4 it reads parse-tree's JSON and refuses it. Field is the
	// parse-tree kind.
	undeterminedGeneratorUnreadable = "generator_unreadable"
	// undeterminedTenantID: a tenant id under `tenants:` that the tenant-id
	// rule refuses (tenantid.Valid); the generator refuses the whole tree
	// on it while the exporter serves the tenant. Field is `tenants.<id>`.
	undeterminedTenantID = "invalid_tenant_id"
	// undeterminedDuplicateTenant: a tenant id declared by two tenant files
	// (the later file in name order is named). The exporter refuses such a
	// tree on a whole-tree run (exit 2), but a --scope run need not see it
	// (measured: a scope holding neither file); the generator refuses the tree.
	// Field is `tenants.<id>`.
	undeterminedDuplicateTenant = "duplicate_tenant"
)

// undeterminedRemedy is the one remedy the report and stderr give.
const undeterminedRemedy = "run `da-tools generate-routes --validate --strict` — " +
	"da-guard no longer judges routing or domain policy (ADR-036 §9)"

// undeterminedMarkdownLimit caps the Markdown list (a sticky PR comment);
// the JSON report always lists every item.
const undeterminedMarkdownLimit = 20

// undeterminedItem is one piece of evidence that the tree holds what only
// the route generator judges. File is relative to --config-dir, with '/'.
type undeterminedItem struct {
	File   string `json:"file"`
	Field  string `json:"field"`
	Reason string `json:"reason"`
}

// undeterminedEvidence lists, for the conf.d tree at configDir, every item
// only the route generator can judge, sorted (file, field, reason); never
// nil, so the JSON field encodes as [] when there is none. A tree the walk
// cannot scan at all is one generator_unreadable item for "." (fail toward
// undetermined), its reason on errOut unless rootReported: the exporter's own
// walk already named the root as unreadable and printed that same reason, and
// stderr names the root and its reason once (exit 3, #2627).
func undeterminedEvidence(configDir string, rootReported bool, errOut io.Writer) []undeterminedItem {
	out := []undeterminedItem{}
	scan, err := config.ScanDirTreeFiles(configDir, log.New(io.Discard, "", 0))
	var doc *parseTreeDoc
	if err == nil {
		doc, err = parseTreeOf(scan)
	}
	if err != nil {
		if !rootReported {
			fmt.Fprintf(errOut, "%s: cannot read the tree as the route generator does: %v\n", programName, err)
		}
		return append(out, undeterminedItem{File: ".", Field: string(parsetree.KindReadFailure), Reason: undeterminedGeneratorUnreadable})
	}
	return append(out, undeterminedFromParseTree(doc)...)
}

// undeterminedFromParseTree is undeterminedEvidence over one parse-tree
// document.
func undeterminedFromParseTree(doc *parseTreeDoc) []undeterminedItem {
	out := []undeterminedItem{}
	declared := map[string][]string{} // tenant id → tenant files, in name order
	for _, u := range doc.Unreadable {
		out = append(out, undeterminedItem{File: u.File, Field: string(u.Kind), Reason: undeterminedGeneratorUnreadable})
	}
	for rel, content := range doc.Files {
		if !readByGenerator(doc.Selected[rel]) {
			continue
		}
		base := path.Base(rel)
		switch {
		case confdlayout.IsDomainPolicyFile(base):
			out = append(out, undeterminedItem{File: rel, Reason: undeterminedDomainPolicy})
			continue
		case routingProfilesNames[base]:
			out = append(out, undeterminedItem{File: rel, Reason: undeterminedRoutingProfilesFile})
			continue
		}
		if tenants, ok := content["tenants"].(map[string]any); ok {
			for id := range tenants {
				if !tenantid.Valid(id) {
					out = append(out, undeterminedItem{File: rel, Field: "tenants." + id, Reason: undeterminedTenantID})
				}
				if !confdname.IsReserved(base) {
					declared[id] = append(declared[id], rel)
				}
			}
		}
		out = routingKeysIn(out, rel, "", content)
	}
	for id, files := range declared {
		sort.Strings(files)
		for _, f := range files[1:] {
			out = append(out, undeterminedItem{File: f, Field: "tenants." + id, Reason: undeterminedDuplicateTenant})
		}
	}
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.File != b.File {
			return a.File < b.File
		}
		if a.Field != b.Field {
			return a.Field < b.Field
		}
		return a.Reason < b.Reason
	})
	return out
}

func readByGenerator(readers []string) bool {
	for _, r := range readers {
		if r == readerGenerator {
			return true
		}
	}
	return false
}

// routingKeysIn appends a routing_key item for every routing key in v, at any
// depth; the value under such a key is not walked (the key already names it).
func routingKeysIn(out []undeterminedItem, rel, at string, v any) []undeterminedItem {
	switch n := v.(type) {
	case map[string]any:
		for k, sub := range n {
			p := k
			if at != "" {
				p = at + "." + k
			}
			if config.IsRoutingLayerKey(k) || k == "routing_profiles" {
				out = append(out, undeterminedItem{File: rel, Field: p, Reason: undeterminedRoutingKey})
				continue
			}
			out = routingKeysIn(out, rel, p, sub)
		}
	case []any:
		for i, sub := range n {
			out = routingKeysIn(out, rel, at+"["+strconv.Itoa(i)+"]", sub)
		}
	}
	return out
}

// undeterminedMarkdown is the report block naming the evidence; "" when there
// is none, so a tree with nothing of the routing plane reads as before.
func undeterminedMarkdown(items []undeterminedItem) string {
	if len(items) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteString("### Routing and domain policy: not judged\n\n")
	b.WriteString("This tree holds what only the route generator judges, so its result is undetermined (exit 4): " +
		undeterminedRemedy + ".\n\n")
	for i, it := range items {
		if i == undeterminedMarkdownLimit {
			fmt.Fprintf(&b, "- … and %d more (the JSON report lists every one)\n", len(items)-i)
			break
		}
		b.WriteString("- `" + it.File + "`")
		if it.Field != "" {
			b.WriteString(" `" + it.Field + "`")
		}
		b.WriteString(" (" + it.Reason + ")\n")
	}
	b.WriteString("\n")
	return b.String()
}

// reportUndetermined is the stderr line for exit 4, so a CI log says it even
// when the report went to --output.
func reportUndetermined(errOut io.Writer, items []undeterminedItem) {
	fmt.Fprintf(errOut, "%s: routing and domain policy not judged: %d item(s) only the route generator judges — %s (exit 4)\n",
		programName, len(items), undeterminedRemedy)
}

// routingRequiredField is the first --required-fields path whose first
// segment is a routing-layer key (config.IsRoutingLayerKey), or "".
func routingRequiredField(fields []string) string {
	for _, f := range fields {
		if first, _, _ := strings.Cut(f, "."); config.IsRoutingLayerKey(first) {
			return f
		}
	}
	return ""
}
