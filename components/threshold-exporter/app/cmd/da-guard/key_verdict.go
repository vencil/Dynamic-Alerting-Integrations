package main

// key_verdict.go — the ERROR layer of the shared key verdict (ADR-038
// decisions 4–7), ported from Python's `scripts/tools/_lib_key_verdict.py`
// (`judge`, `_judge_key`) so `da-guard write` can refuse a batch that leaves
// a tenant a key no reader reads (#3489 step 1: a misspelt
// `mysql_conections`, a dimensional key whose labels the root `dimensions:`
// does not declare).
//
// ⛔ NO READING OF ITS OWN. It joins the same three readings the Python
// verdict joins, each from the function the Python reader's subcommand runs:
//   - served-values with --disable-effects (servedValues, addDisableEffects,
//     spellAsWritten over config.LoadDirFromScan — runServedValues' own
//     path): whether the exporter reads a key, why not, the registry's other
//     readers of a `_` key, what a `disable` does;
//   - effective (config.EffectiveTreeWith, AsWritten — runEffective's): the
//     layer and file each key of a tenant's effective config comes from;
//   - the Policy-as-Code rules of the root carrier (TreeScan.RootCarrier):
//     a rule's `target` / `when.target` head names a key it reads.
//
// Only the ERROR severity is computed here (step 1); the warnings and
// notices, the deduplicated findings and the profile-layer judgment stay in
// Python until step 2 ports them and deletes the Python verdict.
// tests/shared/test_key_verdict_go_parity.py pins this file to the Python
// verdict over every tree the Python verdict's own tests build.

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// keyVerdictCmd is the subcommand name: `da-guard key-verdict ...`.
const keyVerdictCmd = "key-verdict"

// keyVerdictSchema names the document's shape (see servedValuesSchema).
const keyVerdictSchema = "da-guard.key-verdict/v1"

// keyError is one key the verdict reads as unread at severity ERROR.
type keyError struct {
	Tenant string `json:"tenant"`
	// Key as the tenant's config spells it (the effective config's spelling).
	Key string `json:"key"`
	// Reason: served-values' reason (no_row, unregistered, retired, …), or
	// not_served for a key served-values has no entry for; "" for none.
	Reason string `json:"reason"`
	// File: the file the tenant's value comes from (KeySource.File); "" when
	// effective has no source for the key.
	File string `json:"file"`
}

// keyVerdictDoc is `da-guard key-verdict`'s JSON document.
type keyVerdictDoc struct {
	Schema string     `json:"schema"`
	Errors []keyError `json:"errors"`
}

// keyVerdictResult is one tree's ERROR layer. judged is false when the tree
// cannot be judged at all — the exporter's load or effective refuses it (a
// tenant declared twice, no config file, a file the merge cannot decode, a
// /metrics that cannot be gathered): the Python verdict raises there, and
// why says so.
type keyVerdictResult struct {
	judged      bool
	why         error
	undecidable bool
	errors      []keyError
	// parseFailed / unreadable: the files the exporter's load drops
	// (LoadReport), which served-values names under exit 3 — a verdict over
	// a tree that lacks them is one Python does not give.
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

// Registry layers of KeySource.Layer (Python `_LAYERS`). The root
// `defaults:` (level 0) is no registry layer.
var keyRegistryLayers = map[string]string{
	config.KeyLayerTenant:   "tenant",
	config.KeyLayerPlatform: "platform_tenants",
	config.KeyLayerProfile:  "profile",
}

// policyHead is Python's PolicyHead: the first path element of a rule's
// `target` / `when.target`, and the tenants the rule leaves out.
type policyHead struct {
	head     string
	excluded map[string]bool
}

// keyVerdictOf is the ERROR layer of the tree at root as opts makes it read
// (opts.Overlays: the tree a `da-guard write` batch leaves), at `at`.
// rootPlatform: the tree has a root platform file — parse-tree's `_` config
// file at depth 0 (hasRootPlatformFile); without one the threshold keys are
// undecidable (decision 7). dir names the tree in the load (LoadDirReport's
// argument).
//
// opts.SkipDuplicated: a tenant declared in two files does not stop the
// judgment (the exporter's load refuses the whole tree) — every file that
// declares such a tenant is set aside with every tenant it declares
// (setAsideDuplicates), and the rest of the tree is judged as usual. Without
// it such a tree is not judged (`key-verdict`, as served-values refuses it).
func keyVerdictOf(dir string, opts config.ScanOptions, rootPlatform bool, at time.Time) (keyVerdictResult, error) {
	discard := log.New(io.Discard, "", 0)
	overlays := opts.Overlays
	scan, err := config.ScanDirTreeWith(dir, nil, nil, discard, config.ScanOptions{
		Overlays: overlays, OverlayMakesDirs: opts.OverlayMakesDirs})
	if err != nil {
		return keyVerdictResult{}, err
	}
	var setAside map[string]bool
	if scan.Conflict != nil && opts.SkipDuplicated {
		var files map[string]bool
		files, setAside = duplicateDeclarers(scan)
		if overlays, err = setAsideOverlays(scan.AbsRoot, overlays, files); err != nil {
			return keyVerdictResult{}, err
		}
		if scan, err = config.ScanDirTreeWith(dir, nil, nil, discard, config.ScanOptions{
			Overlays: overlays, OverlayMakesDirs: opts.OverlayMakesDirs}); err != nil {
			return keyVerdictResult{}, err
		}
	}
	unjudged := func(err error) (keyVerdictResult, error) {
		return keyVerdictResult{why: err, setAside: setAside}, nil
	}
	cfg, rep, written, err := config.LoadDirFromScan(scan, dir, discard)
	if errors.Is(err, config.ErrNoYAMLFiles) && len(rep.Unreadable) == 0 && rep.RootListErr == nil {
		// No config file at all: no tenant, so no error.
		return keyVerdictResult{judged: true, why: err, undecidable: !rootPlatform, errors: []keyError{},
			noConfig: true, setAside: setAside}, nil
	}
	if errors.Is(err, config.ErrNoYAMLFiles) && len(rep.Unreadable) > 0 {
		// Every config file unreadable: no tenant, the files named
		// (loadJSONTree's allUnreadable).
		return keyVerdictResult{judged: true, undecidable: !rootPlatform, errors: []keyError{},
			unreadable: unreadableEntries(rep.Unreadable), setAside: setAside}, nil
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
	served, err := servedValuesWith(cfg, at, rep.Undeliverable, false, written, refusedTextsOf(rep), true)
	if err == nil {
		err = addDisableEffectsWith(cfg, at, served, written, true)
	}
	if err != nil {
		return unjudged(err)
	}
	spellAsWritten(served, written)
	tree, err := config.EffectiveTreeWith(dir, discard, config.ScanOptions{
		Overlays: overlays, OverlayMakesDirs: opts.OverlayMakesDirs, QuietBuild: true}, at)
	if err != nil {
		return unjudged(err)
	}
	effective := make(map[string]*config.EffectiveConfig, len(tree.Tenants))
	for _, ec := range tree.Tenants {
		effective[ec.TenantID] = ec.AsWritten() // #2031: as `da-guard effective` prints it
	}
	var heads []policyHead
	if _, data, ok := scan.RootCarrier(); ok {
		heads = policyHeadsOf(data)
	}
	return keyVerdictResult{judged: true, undecidable: !rootPlatform,
		errors:      keyVerdictErrors(served, effective, heads, !rootPlatform),
		parseFailed: rep.ParseFailed, unreadable: unreadableEntries(rep.Unreadable), setAside: setAside}, nil
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

// keyVerdictErrors is Python's `judge` keeping only the keys it reads as
// unread at severity ERROR, sorted by tenant then key. Every key of a
// tenant's served `keys` and of its effective config is judged — a key
// served-values has no entry for (one the tenant inherits) goes through the
// same rules as in Python, though in practice it never reaches ERROR.
func keyVerdictErrors(served map[string]servedTenantValues, effective map[string]*config.EffectiveConfig,
	heads []policyHead, undecidable bool) []keyError {
	aliases := config.DeprecatedKeyAliases()
	tenants := map[string]bool{}
	for t := range served {
		tenants[t] = true
	}
	for t := range effective {
		tenants[t] = true
	}
	out := []keyError{}
	for _, tenant := range sortedKeys(tenants) {
		sv, hasSv := served[tenant]
		ef := effective[tenant]
		keys := map[string]bool{}
		if hasSv {
			for k := range sv.Keys {
				keys[k] = true
			}
		}
		var sources map[string]config.KeySource
		if ef != nil {
			for k := range ef.EffectiveConfig {
				keys[k] = true
			}
			sources = ef.KeySources
		}
		for _, key := range sortedKeys(keys) {
			var svp *servedTenantValues
			if hasSv {
				svp = &sv
			}
			src, hasSrc := sources[key]
			if e, isErr := judgeKeyError(tenant, key, svp, src, hasSrc, heads, undecidable, aliases); isErr {
				out = append(out, e)
			}
		}
	}
	return out
}

// judgeKeyError is Python's `_judge_key` (with disable effects) reduced to
// the question "unread at ERROR?", and the error when it is.
func judgeKeyError(tenant, key string, sv *servedTenantValues, src config.KeySource, hasSrc bool,
	heads []policyHead, undecidable bool, aliases map[string]string) (keyError, bool) {
	layer := ""
	if hasSrc {
		layer = src.Layer
	}
	regLayer := keyRegistryLayers[layer]
	if layer == config.KeyLayerDefaults && hasSrc && src.Level != nil && *src.Level > 0 {
		regLayer = "subtree_defaults"
	}
	reserved := strings.HasPrefix(key, "_")

	var sk servedKey
	hasSk := false
	if sv != nil {
		sk, hasSk = sv.Keys[key]
	}
	reason, disable := sk.Reason, sk.Disable
	declared := sk.Declared
	var registered *bool
	if hasSk {
		registered = sk.Registered
	} else if reserved {
		// A key the exporter's build does not take into the tenant's config
		// (no `keys` entry): the registry's own declaration.
		readers, ok := config.RegisteredReaders(key)
		declared, registered = readers, &ok
	}
	misspeltReserved := reserved && registered != nil && !*registered

	canon := pyCanonicalKey(key, aliases)
	var servedValue any
	hasRows := false
	if sv != nil {
		if v, ok := sv.Values[key]; ok {
			servedValue = v
		} else {
			servedValue = sv.Values[canon]
		}
		for _, k := range []string{key, canon} {
			_, inSev := sv.Severities[k]
			_, inDropped := sv.Dropped[k]
			hasRows = hasRows || inSev || inDropped
		}
	}

	read := false
	switch {
	case hasSk && sk.Exporter:
		read = true
	case !hasSk && hasRows:
		// A threshold the tenant inherits, which /metrics serves for it.
		read = true
	case !hasSk && reserved && containsString(declared["exporter"], regLayer) && servedValue != nil:
		read = true
	case !hasSk && strings.HasPrefix(key, "_state_") && servedValue == nil:
		reason = keyReasonStateFilterUndeclared
	}
	if hasSk && reason == keyReasonDisabled && (disable == disableEffective || disable == disablePartial) {
		read, reason = true, ""
	}
	for reader, layers := range declared {
		if reader != "exporter" && containsString(layers, regLayer) {
			read = true
		}
	}
	// Policy-as-Code reads what its view holds: every threshold key, and a
	// `_` key only when the registry lists it.
	inPolicyView := !reserved || (registered != nil && *registered)
	if inPolicyView && namedByPolicy(tenant, key, heads, aliases) {
		read = true
	}
	if read {
		return keyError{}, false
	}
	if undecidable && !reserved {
		return keyError{}, false // undecidable, not unread (decision 7)
	}
	platformSide := layer == config.KeyLayerPlatform || layer == config.KeyLayerProfile ||
		layer == config.KeyLayerDefaults
	switch {
	case misspeltReserved:
		// A `_` key the registry does not list is a misspelt reserved key:
		// ERROR in the tenant's own file, a notice on a platform file.
		if platformSide {
			return keyError{}, false
		}
		if reason != keyReasonRetired {
			reason = keyReasonUnregistered
		}
	case disable == disableNoop:
		return keyError{}, false // WARNING
	case platformSide || reason == keyReasonStateFilterUndeclared:
		return keyError{}, false // NOTICE: the platform's to fix
	}
	if reason == "" && !hasSk {
		reason = "not_served"
	}
	file := ""
	if hasSrc {
		file = src.File
	}
	return keyError{Tenant: tenant, Key: key, Reason: reason, File: file}, true
}

// pyCanonicalKey is Python's `tv.canonical_key` over the exporter's alias
// table (config.DeprecatedKeyAliases, the table served-values' `aliases`
// carries). Not config.CanonicalKeyFor: the two differ on a dimensional
// retired `_critical` (`mysql_cpu_critical{db="a"}`), which Python maps and
// Go does not; the verdict is Python's until step 2 deletes it.
func pyCanonicalKey(key string, aliases map[string]string) string {
	if len(aliases) == 0 || strings.HasPrefix(key, "_") {
		return key
	}
	base, dims, _ := strings.Cut(key, "{")
	brace := ""
	if strings.Contains(key, "{") {
		brace = "{"
	}
	if c, ok := aliases[base]; ok {
		return c + brace + dims
	}
	const critical = "_critical"
	if b, ok := strings.CutSuffix(base, critical); ok {
		if c, ok := aliases[b]; ok {
			return c + critical + brace + dims
		}
	}
	return key
}

// namedByPolicy is Python's `_named_by_policy`: a rule that applies to the
// tenant names the key itself, in either #1231 spelling. A wildcard head
// (`mysql_*`) is compared as written, so it names only a key literally
// spelt that way — never every misspelling it would match.
func namedByPolicy(tenant, key string, heads []policyHead, aliases map[string]string) bool {
	canon := pyCanonicalKey(key, aliases)
	for _, h := range heads {
		if !h.excluded[tenant] && pyCanonicalKey(h.head, aliases) == canon {
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
func policyHeadsOf(data []byte) []policyHead {
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
	var out []policyHead
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
			out = append(out, policyHead{head: head, excluded: excluded})
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

func containsString(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// runKeyVerdict is `da-guard key-verdict --config-dir <dir> [--at <RFC3339>]`.
func runKeyVerdict(args []string, stdout, errOut io.Writer) int {
	fs := flag.NewFlagSet(programName+" "+keyVerdictCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	configDir := fs.String("config-dir", "", "Path to the conf.d/ root. Required.")
	atFlag := fs.String("at", "", "Instant to resolve at, RFC3339. Empty = now.")
	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir> [--at <RFC3339>]\n", programName, keyVerdictCmd)
		fmt.Fprintf(errOut, "Print, as JSON, every tenant key the shared key verdict (ADR-038) reads as unread at\n"+
			"severity ERROR: {\"schema\": %q, \"errors\": [{\"tenant\", \"key\", \"reason\", \"file\"}]}.\n"+
			"The ERROR layer only (#3489 step 1); warnings and notices stay with the Python verdict.\n\n",
			keyVerdictSchema)
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
	list, err := config.ScanDirTreeFiles(*configDir, log.New(io.Discard, "", 0))
	if err != nil {
		return fail(err)
	}
	doc, err := parseTreeOf(list)
	if err != nil {
		return fail(err)
	}
	res, err := keyVerdictOf(*configDir, config.ScanOptions{}, hasRootPlatformFile(doc), at)
	if err != nil {
		return fail(err)
	}
	if !res.judged || res.noConfig {
		return fail(res.why)
	}
	b, err := json.MarshalIndent(keyVerdictDoc{Schema: keyVerdictSchema, Errors: res.errors}, "", "  ")
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
