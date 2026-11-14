package main

// key_verdict.go — the shared key verdict (ADR-038 decisions 4–7) over a
// tree: da-guard reads the tree, pkg/keyverdict judges (#3489 step 2).
// `da-guard key-verdict` prints it; `da-guard write` refuses a batch that
// leaves a tenant a key no reader reads at ERROR (write_worsen.go).
//
// ⛔ NO READING OF ITS OWN. The readings handed to keyverdict.Judge are each
// the function another subcommand runs:
//   - served-values with --disable-effects (servedValuesWith,
//     addDisableEffectsWith, spellAsWritten, keyChecksOf over
//     config.LoadDirFromScan — runServedValues' own path): whether the
//     exporter reads a key, why not, the registry's other readers of a `_`
//     key, what a `disable` does;
//   - effective (config.EffectiveTreeWith, AsWritten — runEffective's): the
//     layer and file each key of a tenant's effective config comes from;
//   - the Policy-as-Code rules of the root carrier (TreeScan.RootCarrier):
//     a rule's `target` / `when.target` head names a key it reads;
//   - parse-tree's document of the tree (parseTreeOf): whether a root
//     platform file is read (decision 7), the `profiles:` of those files and
//     the top-level keys of each tenant file (#3671).
//
// It is the only verdict: the Python tools read `da-guard key-verdict`'s
// document (scripts/tools/_lib_key_verdict.py) and judge nothing themselves.
// key_verdict_golden_test.go pins the document.

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/keyverdict"
)

// keyVerdictCmd is the subcommand name: `da-guard key-verdict ...`.
const keyVerdictCmd = "key-verdict"

// keyVerdictSchema names the document's shape (see servedValuesSchema).
// v2 (#3489 step 2): the whole verdict; v1 carried the ERROR layer only.
const keyVerdictSchema = "da-guard.key-verdict/v2"

// keyError is one key the verdict reads as unread at severity ERROR — what
// `da-guard write` compares before and after a batch.
type keyError struct {
	Tenant string
	// Key as the tenant's config spells it.
	Key string
	// Reason: the verdict's reason (no_row, unregistered, not_served, …).
	Reason string
	// File: the file the tenant's value comes from; "" when none.
	File string
}

// keyVerdictDoc is `da-guard key-verdict`'s JSON document.
type keyVerdictDoc struct {
	Schema string `json:"schema"`
	At     string `json:"at"`
	// Undecidable: no root platform file was read (decision 7): a threshold
	// key's verdict is "undecidable".
	Undecidable bool `json:"undecidable"`
	// Blocks: a gate that judges the whole tree refuses it — some finding
	// blocks, or some key is undecidable (keyverdict.Blocks; #3016).
	Blocks bool `json:"blocks"`
	// UndecidableBlocks: some key is undecidable, which blocks such a gate
	// — the answer a report of the undecidable keys prints, so no reader
	// infers it from Blocks and the findings.
	UndecidableBlocks bool `json:"undecidable_blocks"`
	// Tenants: tenant → the verdict on each of its keys, sorted by key.
	Tenants map[string][]keyverdict.KeyVerdict `json:"tenants"`
	// Findings: what a report prints, error first.
	Findings []keyverdict.Finding `json:"findings"`
	// KeyChecks (ADR-038 §11): per tenant, the exporter's own messages on
	// its keys' `version` labels and retired spellings (served-values'
	// key_checks). Not part of the verdict on whether a key is read.
	KeyChecks map[string]servedKeyChecks `json:"key_checks"`
	// ParseFailed / Unreadable: the files the exporter's load drops, as
	// served-values names them (exit 3); the verdict is on the rest of the
	// tree. Always present ([] when none).
	ParseFailed []string      `json:"parse_failed"`
	Unreadable  []skippedFile `json:"unreadable"`
	tenantSubset
}

// keyVerdictResult is one tree's verdict. judged is false when the tree
// cannot be judged at all — the exporter's load or effective refuses it (a
// tenant declared twice, no config file, a file the merge cannot decode, a
// /metrics that cannot be gathered); why says so.
type keyVerdictResult struct {
	judged bool
	why    error
	tree   keyverdict.Tree
	// keyChecks: served-values' key_checks per judged tenant.
	keyChecks map[string]servedKeyChecks
	// computed: the tenants the served reading computed (a --tenant
	// request's present ones).
	computed map[string]bool
	// errors: the keys the verdict reads as unread at ERROR, sorted by
	// tenant then key.
	errors []keyError
	// parseFailed / unreadable: the files the exporter's load drops
	// (LoadReport), which served-values names under exit 3.
	parseFailed []string
	unreadable  []skippedFile
	// noConfig: the tree holds no config file at all — judged, no tenant,
	// no error (a `da-guard write` that starts the tree has an empty
	// baseline). served-values refuses such a tree; `key-verdict` does too.
	noConfig bool
	// setAside: with ScanOptions.SkipDuplicated, every tenant left out of
	// the judgment because a file declaring it also declares a tenant
	// another file declares (setAsideDuplicates).
	setAside map[string]bool
}

// keyVerdictOpts is how keyVerdictOf reads the tree.
type keyVerdictOpts struct {
	// scan: Overlays (the tree a `da-guard write` batch leaves),
	// OverlayMakesDirs, SkipDuplicated (a tenant declared in two files does
	// not stop the judgment: every file that declares such a tenant is set
	// aside with every tenant it declares, the rest judged as usual —
	// without it such a tree is not judged).
	scan config.ScanOptions
	// tenants: compute and judge only these (served-values' and effective's
	// --tenant); nil for the whole tree.
	tenants []string
	// rootPlatform: a root platform file is read (hasRootPlatformFile of
	// parse-tree's document of the same tree): without one the threshold
	// keys are undecidable (decision 7). A write's baseline sets it whatever
	// the tree holds (write_worsen.go).
	rootPlatform bool
	// profiles: rootProfilesOf the same document — the profile layer; nil:
	// not judged. The facts, not the document: the document of a large tree
	// need not stay in memory through the verdict.
	profiles map[string]map[string]any
	// tenantFiles: tenantFileKeysOf the same document — each tenant file's
	// top-level keys (#3671); nil: not judged.
	tenantFiles map[string][]string
}

// keyVerdictOf is the verdict on the tree at dir as opts makes it read, at
// `at`. dir names the tree in the load (LoadDirReport's argument).
func keyVerdictOf(dir string, opts keyVerdictOpts, at time.Time) (keyVerdictResult, error) {
	discard := log.New(io.Discard, "", 0)
	overlays := opts.scan.Overlays
	rootPlatform := opts.rootPlatform
	scan, err := config.ScanDirTreeWith(dir, nil, nil, discard, config.ScanOptions{
		Overlays: overlays, OverlayMakesDirs: opts.scan.OverlayMakesDirs})
	if err != nil {
		return keyVerdictResult{}, err
	}
	var setAside, setAsideFiles map[string]bool
	if scan.Conflict != nil && opts.scan.SkipDuplicated {
		setAsideFiles, setAside = duplicateDeclarers(scan)
		if overlays, err = setAsideOverlays(scan.AbsRoot, overlays, setAsideFiles); err != nil {
			return keyVerdictResult{}, err
		}
		if scan, err = config.ScanDirTreeWith(dir, nil, nil, discard, config.ScanOptions{
			Overlays: overlays, OverlayMakesDirs: opts.scan.OverlayMakesDirs}); err != nil {
			return keyVerdictResult{}, err
		}
	}
	unjudged := func(err error) (keyVerdictResult, error) {
		return keyVerdictResult{why: err, setAside: setAside}, nil
	}
	empty := func() keyVerdictResult {
		return keyVerdictResult{judged: true, tree: keyverdict.Tree{Keys: map[string][]keyverdict.KeyVerdict{},
			Findings: []keyverdict.Finding{}, Undecidable: !rootPlatform},
			keyChecks: map[string]servedKeyChecks{}, computed: map[string]bool{}, errors: []keyError{},
			setAside: setAside}
	}
	var heads []keyverdict.PolicyHead
	if _, data, ok := scan.RootCarrier(); ok {
		heads = policyHeadsOf(data)
	}
	cfg, rep, written, err := config.LoadDirFromScan(scan, dir, discard)
	if errors.Is(err, config.ErrNoYAMLFiles) && len(rep.Unreadable) == 0 && rep.RootListErr == nil {
		// No config file at all: no tenant, so no error.
		res := empty()
		res.why, res.noConfig = err, true
		return res, nil
	}
	if errors.Is(err, config.ErrNoYAMLFiles) && len(rep.Unreadable) > 0 {
		// Every config file unreadable: no tenant, the files named
		// (loadJSONTree's allUnreadable).
		res := empty()
		res.unreadable = unreadableEntries(rep.Unreadable)
		return res, nil
	}
	if err != nil {
		return unjudged(err)
	}
	if err := checkUTF8(cfg); err != nil {
		return unjudged(err)
	}
	// Quiet: every resolver WARN discarded. This reading is a judgment's,
	// not a report: `da-guard write`'s stderr stays what it was before the
	// key verdict joined it, and `key-verdict` prints JSON only (#3489).
	sub := subsetConfig(cfg, opts.tenants)
	served, err := servedValuesWith(sub, at, rep.Undeliverable, false, written, refusedTextsOf(rep), true)
	if err == nil {
		err = addDisableEffectsWith(sub, at, served, written, true)
	}
	if err != nil {
		return unjudged(err)
	}
	spellAsWritten(served, written)
	keyChecksOf(sub, served)
	effOpts := config.ScanOptions{Overlays: overlays, OverlayMakesDirs: opts.scan.OverlayMakesDirs, QuietBuild: true}
	if opts.tenants != nil {
		effOpts.Tenants = map[string]bool{}
		for _, t := range opts.tenants {
			effOpts.Tenants[t] = true
		}
	}
	tree, err := config.EffectiveTreeWith(dir, discard, effOpts, at)
	if err != nil {
		return unjudged(err)
	}
	in := keyverdict.Input{
		Served:      servedForVerdict(served),
		Effective:   make(map[string]keyverdict.EffectiveTenant, len(tree.Tenants)),
		Heads:       heads,
		Undecidable: !rootPlatform,
	}
	for _, ec := range tree.Tenants {
		ec = ec.AsWritten() // #2031: as `da-guard effective` prints it
		// A YAML `.inf` / `.nan` is text, as effective prints it.
		in.Effective[ec.TenantID] = keyverdict.EffectiveTenant{Config: config.NonFiniteAsText(ec.EffectiveConfig),
			Sources: ec.KeySources, Profile: ec.BoundProfile, DefaultsChain: ec.DefaultsChain,
			NotServed: ec.NotServed}
	}
	in.RootProfiles = opts.profiles
	in.Values = rep.ValueProblems
	in.Schedules = rep.ScheduleProblems
	in.TenantFiles = judgedTenantFiles(opts.tenantFiles, scan, opts.tenants, setAsideFiles)
	// A profile no tenant binds is known only over every tenant (#3395):
	// not with a --tenant subset, and not when a file the load drops, or a
	// tenant set aside, may be the one binding it.
	in.Whole = opts.tenants == nil && len(rep.ParseFailed) == 0 && len(rep.Unreadable) == 0 && len(setAside) == 0
	res := keyVerdictResult{judged: true, tree: keyverdict.Judge(in),
		keyChecks: map[string]servedKeyChecks{}, computed: map[string]bool{},
		parseFailed: rep.ParseFailed, unreadable: unreadableEntries(rep.Unreadable), setAside: setAside}
	for t, sv := range served {
		res.keyChecks[t] = sv.KeyChecks
		res.computed[t] = true
	}
	res.errors = errorsOf(res.tree)
	return res, nil
}

// errorsOf is every key tree reads as unread at ERROR, sorted by tenant then
// key.
func errorsOf(tree keyverdict.Tree) []keyError {
	out := []keyError{}
	for _, tenant := range sortedKeys(tree.Keys) {
		for _, v := range tree.Keys[tenant] {
			if v.Verdict != keyverdict.Unread || v.Severity == nil || *v.Severity != keyverdict.SeverityError {
				continue
			}
			e := keyError{Tenant: tenant, Key: v.Key}
			if v.Reason != nil {
				e.Reason = *v.Reason
			}
			if v.File != nil {
				e.File = *v.File
			}
			out = append(out, e)
		}
	}
	return out
}

// servedForVerdict is served-values' tenants as keyverdict reads them.
func servedForVerdict(served map[string]servedTenantValues) map[string]keyverdict.ServedTenant {
	out := make(map[string]keyverdict.ServedTenant, len(served))
	for t, sv := range served {
		keys := make(map[string]keyverdict.ServedKey, len(sv.Keys))
		for k, sk := range sv.Keys {
			keys[k] = keyverdict.ServedKey{Exporter: sk.Exporter, Reason: sk.Reason, Registered: sk.Registered,
				Declared: sk.Declared, Disable: sk.Disable, SwitchedOffWith: sk.SwitchedOffWith,
				DeadEntries: sk.DeadEntries}
		}
		rows := make(map[string]bool, len(sv.Severities)+len(sv.Dropped))
		for k := range sv.Severities {
			rows[k] = true
		}
		for k := range sv.Dropped {
			rows[k] = true
		}
		out[t] = keyverdict.ServedTenant{Keys: keys, Values: sv.Values, Rows: rows}
	}
	return out
}

// rootProfilesOf is the `profiles:` mapping of each root platform file
// parse-tree reads (the files the exporter takes profiles from): file →
// profile name → body. A root file no reader reads (an unselected defaults
// spelling) or one parse-tree cannot read has no content and gives none.
func rootProfilesOf(doc *parseTreeDoc) map[string]map[string]any {
	out := map[string]map[string]any{}
	for _, e := range doc.Entries {
		if e.Role != config.RoleConfig || e.Depth != 0 || (e.Class != classDefaults && e.Class != classPlatform) {
			continue
		}
		content, ok := doc.Files[e.Path]
		if !ok {
			continue
		}
		if profiles, ok := content["profiles"].(map[string]any); ok {
			out[e.Path] = profiles
		}
	}
	return out
}

// tenantFileKeysOf is the top-level keys of each tenant file parse-tree
// reads (#3671): file → its keys, sorted, as the file spells them. A file
// parse-tree cannot read has no content and gives none (its problem is the
// load's parse_failed / unreadable).
func tenantFileKeysOf(doc *parseTreeDoc) map[string][]string {
	out := map[string][]string{}
	for _, e := range doc.Entries {
		if e.Role != config.RoleConfig || e.Class != classTenant {
			continue
		}
		if content, ok := doc.Files[e.Path]; ok {
			out[e.Path] = sortedKeys(content)
		}
	}
	return out
}

// judgedTenantFiles is files restricted to the ones the verdict judges: a
// tenant file the exporter's walk (scan) kept and decoded — not one the load
// drops (parse_failed, which is reported as such) — and, with a --tenant
// subset, one that declares a requested tenant. A file set aside
// (SkipDuplicated, aside) is judged too, over the whole tree: its top-level
// keys are the file's, whatever tenant it declares, and leaving them out of a
// write's baseline makes a batch that removes the duplicate copy look as if
// it added them.
func judgedTenantFiles(files map[string][]string, scan *config.TreeScan, tenants []string,
	aside map[string]bool) map[string][]string {
	if files == nil {
		return nil
	}
	want := map[string]bool{}
	for _, t := range tenants {
		want[t] = true
	}
	out := map[string][]string{}
	for file, keys := range files {
		if aside[file] && tenants == nil {
			out[file] = keys
			continue
		}
		f, ok := scan.Files[file]
		if !ok || f.ParseFailed {
			continue
		}
		if tenants != nil && !declaresAny(f.TenantIDs, want) {
			continue
		}
		out[file] = keys
	}
	return out
}

func declaresAny(ids []string, want map[string]bool) bool {
	for _, id := range ids {
		if want[id] {
			return true
		}
	}
	return false
}

// duplicateDeclarers is, for a scan whose walk recorded a tenant declared in
// two or more files (TreeScan.Locate's DuplicateTenantError), every tenant
// file declaring such a tenant (root-relative paths) and every tenant those
// files declare. Read off TreeScan.Files, as ScopeEffective's in-scope set
// is: TreeScan.Tenants is nil under a duplicate.
func duplicateDeclarers(scan *config.TreeScan) (files, tenants map[string]bool) {
	files, tenants = map[string]bool{}, map[string]bool{}
	dup := map[string]bool{}
	for _, f := range scan.Files {
		for _, id := range f.TenantIDs {
			if _, done := dup[id]; done {
				continue
			}
			var de *config.DuplicateTenantError
			_, lerr := scan.Locate(id)
			dup[id] = errors.As(lerr, &de)
		}
	}
	for rel, f := range scan.Files {
		for _, id := range f.TenantIDs {
			if dup[id] {
				files[rel] = true
				for _, other := range f.TenantIDs {
					tenants[other] = true
				}
				break
			}
		}
	}
	return files, tenants
}

// setAsideOverlays is overlays with every file of aside out of the walk: a
// file on disk the batch leaves or writes is removed (FileOverlay.Remove), a
// file only the batch creates is not created.
func setAsideOverlays(root string, overlays []config.FileOverlay, aside map[string]bool) ([]config.FileOverlay, error) {
	out := make([]config.FileOverlay, 0, len(overlays)+len(aside))
	named := map[string]bool{}
	for _, o := range overlays {
		named[o.Rel] = true
		if !aside[o.Rel] || o.Remove {
			out = append(out, o)
			continue
		}
		if _, err := os.Lstat(filepath.Join(root, filepath.FromSlash(o.Rel))); err == nil {
			out = append(out, config.FileOverlay{Rel: o.Rel, Remove: true})
		} else if !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
	}
	for _, rel := range sortedKeys(aside) {
		if !named[rel] {
			out = append(out, config.FileOverlay{Rel: rel, Remove: true})
		}
	}
	return out, nil
}

// hasRootPlatformFile is Python's `root_platform_files(...) != []`: a
// config file (role config, readable or not) of class defaults or platform
// at depth 0 of parse-tree's document.
func hasRootPlatformFile(doc *parseTreeDoc) bool {
	for _, e := range doc.Entries {
		if e.Role == config.RoleConfig && e.Depth == 0 && (e.Class == classDefaults || e.Class == classPlatform) {
			return true
		}
	}
	return false
}

// policyHeadsOf is Python's `policy_targets(_tree_rules(...))` over the root
// carrier's bytes: the top-level `policies` when the document has that key
// (whatever its value), else `_policies`; each rule's `target` and
// `when.target` up to the first ".", with its `exclude_tenants` read as
// scalar source text (`010` stays "010", #2114).
//
// ⚠️ Lenient where Python is not: a document, rule list, rule or field that
// cannot be read is skipped here, while policy_engine raises
// InvalidPolicyRules (#2961) and the Python verdict fails with it. A tree
// carrying such rules is one Python judges not at all; this side judges it
// without them.
func policyHeadsOf(data []byte) []keyverdict.PolicyHead {
	var doc yaml.Node
	if err := yaml.Unmarshal(data, &doc); err != nil || len(doc.Content) == 0 {
		return nil
	}
	top := doc.Content[0]
	if top.Kind != yaml.MappingNode {
		return nil
	}
	rules := mappingValue(top, "policies")
	if rules == nil {
		rules = mappingValue(top, "_policies")
	}
	if rules == nil || rules.Kind != yaml.SequenceNode {
		return nil
	}
	var out []keyverdict.PolicyHead
	for _, rule := range rules.Content {
		if rule.Kind != yaml.MappingNode {
			continue
		}
		var targets []string
		if t := stringScalar(mappingValue(rule, "target")); t != "" {
			targets = append(targets, t)
		}
		if w := mappingValue(rule, "when"); w != nil && w.Kind == yaml.MappingNode {
			if t := stringScalar(mappingValue(w, "target")); t != "" {
				targets = append(targets, t)
			}
		}
		excluded := map[string]bool{}
		if ex := mappingValue(rule, "exclude_tenants"); ex != nil && ex.Kind == yaml.SequenceNode {
			for _, item := range ex.Content {
				if item.Kind == yaml.ScalarNode {
					excluded[item.Value] = true
				}
			}
		}
		for _, t := range targets {
			head, _, _ := strings.Cut(t, ".")
			out = append(out, keyverdict.PolicyHead{Head: head, Excluded: excluded})
		}
	}
	return out
}

// mappingValue is the value node of key in mapping m (aliases followed),
// nil when m has no such key; a key present with a null value is a null
// node, not nil (Python's `"policies" in data`).
func mappingValue(m *yaml.Node, key string) *yaml.Node {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if k := m.Content[i]; k.Kind == yaml.ScalarNode && k.Value == key {
			v := m.Content[i+1]
			for v.Kind == yaml.AliasNode && v.Alias != nil {
				v = v.Alias
			}
			return v
		}
	}
	return nil
}

// stringScalar is a `!!str` scalar's text, "" for anything else (Python
// takes only a string target).
func stringScalar(n *yaml.Node) string {
	if n == nil || n.Kind != yaml.ScalarNode || n.ShortTag() != "!!str" {
		return ""
	}
	return n.Value
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// treeFacts is what parse-tree's document of the tree at dir says for the
// verdict (keyVerdictOpts.rootPlatform, .profiles, .tenantFiles). The document itself does
// not outlive this call: a large tree's need not stay in memory through the
// verdict.
func treeFacts(dir string) (keyVerdictOpts, error) {
	list, err := config.ScanDirTreeFiles(dir, log.New(io.Discard, "", 0))
	if err != nil {
		return keyVerdictOpts{}, err
	}
	doc, err := parseTreeOf(list)
	if err != nil {
		return keyVerdictOpts{}, err
	}
	return keyVerdictOpts{rootPlatform: hasRootPlatformFile(doc), profiles: rootProfilesOf(doc),
		tenantFiles: tenantFileKeysOf(doc)}, nil
}

// runKeyVerdict is `da-guard key-verdict --config-dir <dir> [--at <RFC3339>]
// [--tenant <id> ...]`.
func runKeyVerdict(args []string, stdout, errOut io.Writer) int {
	fs := flag.NewFlagSet(programName+" "+keyVerdictCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	configDir := fs.String("config-dir", "", "Path to the conf.d/ root. Required.")
	atFlag := fs.String("at", "", "Instant to resolve at, RFC3339. Empty = now.")
	var tenants tenantList
	fs.Var(&tenants, "tenant",
		"Judge only this tenant (repeatable): the whole tree is still loaded, every platform file read;\n"+
			"the document then carries tenants_requested and tenants_absent (requested, not in the tree).\n"+
			"A profile's finding is judged over the requested tenants only.")
	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir> [--at <RFC3339>] [--tenant <id> ...]\n", programName, keyVerdictCmd)
		fmt.Fprintf(errOut, "Print, as JSON, the shared key verdict (ADR-038): for every key of every tenant whether\n"+
			"some reader reads it, and the findings a report prints:\n"+
			"{\"schema\": %q, \"at\", \"undecidable\", \"tenants\": {<tenant>: [{key, layer, file, read_by,\n"+
			"verdict, severity, reason, disable, hint, switched_off_with, value, served}]}, \"findings\": [{severity,\n"+
			"kind, key, file, tenants, reason, hint, base}], \"key_checks\": {<tenant>: {version_labels,\n"+
			"renames}}, \"parse_failed\": [<file>], \"unreadable\": [{file, reason}]}.\n\n", keyVerdictSchema)
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  ok\n  2  caller error, or a tree the verdict cannot judge (the exporter's load or\n"+
			"     effective refuses it, e.g. a tenant declared twice), or a conf.d path that is not valid UTF-8\n"+
			"  3  config files the exporter cannot decode or cannot read (as served-values names them); the\n"+
			"     JSON is still written, judged without them\n")
	}
	if err := fs.Parse(args); errors.Is(err, flag.ErrHelp) {
		return exitOK
	} else if err != nil {
		return exitCallerErr
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(errOut, "%s %s: unexpected argument %q\n", programName, keyVerdictCmd, fs.Arg(0))
		return exitCallerErr
	}
	if *configDir == "" {
		fmt.Fprintf(errOut, "%s %s: --config-dir is required\n", programName, keyVerdictCmd)
		return exitCallerErr
	}
	at := time.Now().UTC()
	if *atFlag != "" {
		var err error
		if at, err = time.Parse(time.RFC3339, *atFlag); err != nil {
			fmt.Fprintf(errOut, "%s %s: --at must be RFC3339: %v\n", programName, keyVerdictCmd, err)
			return exitCallerErr
		}
	}
	if refuseNonUTF8Tree(*configDir, programName+" "+keyVerdictCmd, errOut) {
		return exitCallerErr
	}
	fail := func(err error) int {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, keyVerdictCmd, err)
		return exitCallerErr
	}
	opts, err := treeFacts(*configDir)
	if err != nil {
		return fail(err)
	}
	requested := tenants.requested()
	opts.tenants = requested
	res, err := keyVerdictOf(*configDir, opts, at)
	if err != nil {
		return fail(err)
	}
	if !res.judged || res.noConfig {
		return fail(res.why)
	}
	out := keyVerdictDoc{Schema: keyVerdictSchema, At: at.Format(time.RFC3339), Undecidable: res.tree.Undecidable,
		Blocks: res.tree.Blocks, UndecidableBlocks: res.tree.UndecidableBlocks,
		Tenants: res.tree.Keys, Findings: res.tree.Findings, KeyChecks: res.keyChecks,
		ParseFailed: nonNilStrings(res.parseFailed), Unreadable: res.unreadable,
		tenantSubset: subset(requested, func(t string) bool { return res.computed[t] })}
	if out.Unreadable == nil {
		out.Unreadable = []skippedFile{}
	}
	if err := checkOutputUTF8("", reflect.ValueOf(out)); err != nil {
		return fail(err)
	}
	b, err := json.MarshalIndent(out, "", "  ")
	if err != nil {
		return fail(err)
	}
	if _, err := stdout.Write(append(b, '\n')); err != nil {
		return fail(err)
	}
	if len(res.parseFailed) > 0 {
		reportParseFailed(errOut, res.parseFailed)
	}
	if len(res.unreadable) > 0 {
		reportUnreadable(errOut, programName+" "+keyVerdictCmd, res.unreadable)
	}
	if len(res.parseFailed) > 0 || len(res.unreadable) > 0 {
		return exitParseFailed
	}
	return exitOK
}
