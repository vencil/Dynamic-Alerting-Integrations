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
	"unicode"
	"unicode/utf8"

	"github.com/vencil/threshold-exporter/internal/confdlayout"
	"github.com/vencil/threshold-exporter/pkg/confdname"
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
	// level (Field is ""), or a top-level `domain_policies` key in any other
	// file the generator reads (Field is "domain_policies"): the generator
	// refuses the whole tree on such a block in every mode (#3126).
	undeterminedDomainPolicy = "domain_policy"
	// undeterminedRoutingProfilesFile: a `_routing_profiles.yaml` / `.yml`
	// file, any level. Field is "".
	undeterminedRoutingProfilesFile = "routing_profiles_file"
	// undeterminedUnreadableOutsideRun: a path parse-tree names unreadable
	// (the route generator refuses the whole tree on it) that this run's
	// exit 3 does not name. Field is the parse-tree kind; a tree the walk
	// cannot scan at all is one item for "." with read_failure.
	//
	// ⚠️ NARROWER THAN `generator_unreadable`, which it replaces (ADR-036 §9,
	// step 6, #3206). That one named every such path, whether or not the
	// exporter read it. Since step 6 the exporter refuses every file whose
	// content parse-tree refuses (pkg/config parse_verdict.go), so such a file
	// is in ParseFailed and one the walk cannot read in Unreadable — exit 3 on
	// every run it bears on, and not evidence too. What is left is what exit 3
	// cannot cover: such a path outside --scope (exit 3 is the scope's; the
	// generator's verdict is the tree's), and a config-named directory or
	// directory symlink (parse-tree's not_regular_file; the walk skips it
	// without reading). Without it those runs ended in the vacuous 0 while the
	// generator refused the tree (FAIL TOWARD UNDETERMINED, above).
	undeterminedUnreadableOutsideRun = "unreadable_outside_run"
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

// configDirPlaceholder stands for --config-dir in text that has no run's value
// (the help), spelled as the usage lines spell it.
const configDirPlaceholder = "<dir>"

// generatorCommand is the route generator's command as da-guard tells a reader
// to run it, for the conf.d at configDir — the --config-dir value as the caller
// gave it, so the line is right when pasted where da-guard ran (#3420). It is
// shell-quoted (shellQuote): what the shell splits it into is what argparse
// reads. A value argparse would take for an option (a leading '-') is joined
// with '=': `--config-dir -x` is a usage error there, `--config-dir=-x` is not.
//
// ⛔ THE ONE SPELLING. Every place da-guard names this command — the report,
// stderr, the help — takes it from here (generatorHelpCommand for the help);
// before #3420 three hand-written copies all lacked the required --config-dir,
// so the line pasted as told ended in rc 2.
// tests/shared/test_da_guard_generator_hint.py parses what da-guard prints
// with the generator's own argparse parser.
func generatorCommand(configDir string) string {
	if strings.HasPrefix(configDir, "-") {
		return generatorCommandWith("--config-dir=" + shellQuote(configDir))
	}
	return generatorCommandWith("--config-dir " + shellQuote(configDir))
}

// generatorHelpCommand is generatorCommand for the help: the placeholder
// stands as written, unquoted.
func generatorHelpCommand() string {
	return generatorCommandWith("--config-dir " + configDirPlaceholder)
}

func generatorCommandWith(configDirArg string) string {
	return "da-tools generate-routes " + configDirArg + " --validate --strict"
}

// shellQuote is s as one POSIX shell word: as is when every byte is one no
// shell treats specially (Python's shlex.quote set), else in single quotes,
// each ' written '"'"'. The empty string is a pair of single quotes.
func shellQuote(s string) string {
	if s == "" {
		return "''"
	}
	for i := 0; i < len(s); i++ {
		if !shellSafeByte(s[i]) {
			return "'" + strings.ReplaceAll(s, "'", `'"'"'`) + "'"
		}
	}
	return s
}

func shellSafeByte(c byte) bool {
	return c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z' || c >= '0' && c <= '9' ||
		strings.IndexByte("@%+=:,./_-", c) >= 0
}

// codeSpan is s as one Markdown code span: fenced with one backtick more than
// the longest run in s, so a backtick in a path cannot end it early. s is a
// generator command, so it never starts or ends with a backtick (CommonMark
// would want a space between such an s and the fence).
func codeSpan(s string) string {
	longest, run := 0, 0
	for i := 0; i < len(s); i++ {
		if s[i] == '`' {
			run++
			if run > longest {
				longest = run
			}
		} else {
			run = 0
		}
	}
	fence := strings.Repeat("`", longest+1)
	return fence + s + fence
}

// showablePath: s can stand in one line of the report and of stderr as
// written — valid UTF-8, no control character. A line break cannot be inside
// a Markdown code span (it renders as a space, so the pasted path is another
// one), and a control character in stderr is the terminal's, not the reader's.
func showablePath(s string) bool {
	if !utf8.ValidString(s) {
		return false
	}
	for _, r := range s {
		if unicode.IsControl(r) {
			return false
		}
	}
	return true
}

// undeterminedRemedy is the one remedy the report and stderr give, for the
// run's --config-dir. A path that cannot be shown (showablePath) is not
// spelled out: the command carries the help's placeholder and says why.
func undeterminedRemedy(configDir string) string {
	cmd, why := generatorCommand(configDir), ""
	if !showablePath(configDir) {
		cmd = generatorHelpCommand()
		why = " with " + configDirPlaceholder + " this run's --config-dir (the path holds characters this report cannot show)"
	}
	return "run " + codeSpan(cmd) + why + " — " +
		"da-guard no longer judges routing or domain policy (ADR-036 §9)"
}

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
// nil, so the JSON field encodes as [] when there is none. reported is the
// paths this run names under exit 3 (ParseFailed and Unreadable, relative to
// configDir): a path parse-tree names unreadable is evidence only when it is
// not one of them. A tree the walk cannot scan at all is one
// unreadable_outside_run item for "." (fail toward undetermined) unless
// reported holds "."; its reason goes to errOut unless rootReported: the
// exporter's own walk already named the root as unreadable and printed that
// same reason, and stderr names the root and its reason once (exit 3, #2627).
func undeterminedEvidence(configDir string, rootReported bool, reported map[string]bool, errOut io.Writer) []undeterminedItem {
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
		if reported["."] {
			return out
		}
		return append(out, undeterminedItem{File: ".", Field: string(parsetree.KindReadFailure), Reason: undeterminedUnreadableOutsideRun})
	}
	return append(out, undeterminedFromParseTree(doc, reported)...)
}

// undeterminedFromParseTree is undeterminedEvidence over one parse-tree
// document.
func undeterminedFromParseTree(doc *parseTreeDoc, reported map[string]bool) []undeterminedItem {
	out := []undeterminedItem{}
	declared := map[string][]string{} // tenant id → tenant files, in name order
	readers := map[string][]string{}
	for _, e := range doc.Entries {
		readers[e.Path] = e.Readers
		// pathVerdict: the rule parse-tree's "verdict" is (verdict.go).
		if pathVerdict(e, reported) == verdictTreeRefused {
			out = append(out, undeterminedItem{File: e.Path, Field: string(e.Problem.Kind), Reason: undeterminedUnreadableOutsideRun})
		}
	}
	for rel, content := range doc.Files {
		if !readByGenerator(readers[rel]) {
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
		if _, ok := content["domain_policies"]; ok {
			out = append(out, undeterminedItem{File: rel, Field: "domain_policies", Reason: undeterminedDomainPolicy})
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
func undeterminedMarkdown(configDir string, items []undeterminedItem) string {
	if len(items) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteString("### Routing and domain policy: not judged\n\n")
	b.WriteString("This tree holds what only the route generator judges, so its result is undetermined (exit 4): " +
		undeterminedRemedy(configDir) + ".\n\n")
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
func reportUndetermined(errOut io.Writer, configDir string, items []undeterminedItem) {
	fmt.Fprintf(errOut, "%s: routing and domain policy not judged: %d item(s) only the route generator judges — %s (exit 4)\n",
		programName, len(items), undeterminedRemedy(configDir))
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
