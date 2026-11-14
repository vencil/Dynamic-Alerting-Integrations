package main

// write_worsen.go — the tree-scope rule of `da-guard write` (#3206):
// a call may repair the tree, never worsen it. Each file of the batch is
// judged on its own entry (write.go judgeTree); this is the rest of the
// tree. The tree before the call (the disk) and after it (the batch's texts
// in place, one walk) are compared, and the whole call is refused,
// reason worsens_tree, nothing written, when the after-tree has anything the
// before-tree did not:
//   - a path outside the batch that loses readers (a carrier the batch
//     deselects);
//   - a verdict or a problem on any parse-tree entry — a file or a directory,
//     one the batch creates included — that the path did not have;
//   - an error of da-guard's own tree validation (what `da-guard
//     --config-dir <dir>` reports with no other flag: the same ScopeEffective,
//     buildCheckInput and guard check; a tenant declared in two files)
//     that the before-tree did not have, by a stable key — kind, tenant,
//     field or path — so an error already there elsewhere does not block an
//     unrelated write, and a batch that moves a tenant between two files
//     passes. A tenant declared twice is set aside, not a stop, so the rest
//     of the tree is still validated; a tenant declared twice before the
//     call has no baseline, and its findings after it are not new — removing
//     a copy repairs the tree;
//   - a tenant key the shared key verdict (ADR-038; key_verdict.go, its
//     ERROR layer) reads as unread at ERROR that it did not before the call
//     (#3489), in a tenant file the batch writes: a misspelt key, a
//     dimensional key whose labels the root `dimensions:` does not declare,
//     an unregistered `_` key. Keyed by (tenant, key), so an error already
//     there does not block an unrelated write and a fix passes; a batch that
//     writes only platform files is not judged on other tenants' keys, as
//     tenant-api's gate is not (newlyUnread).
//
// ⚠️ Not compared: validation warnings (they do not fail validation), and
// what only the route generator judges (exit 4's evidence beyond the
// parse-tree problems compared above).

import (
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
	"time"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/keyverdict"
)

// The closed set of writeWorsened.Kind values.
const (
	worsenReadersLost     = "readers_lost"
	worsenVerdict         = "verdict"
	worsenProblem         = "problem"
	worsenDuplicateTenant = "duplicate_tenant"
	worsenFinding         = "finding" // a validation error; Finding names its kind
	// worsenUnread (#3489): a tenant key the shared key verdict reads as
	// unread at ERROR (key_verdict.go); Finding is the verdict's reason
	// (no_row, unregistered, …), Path the file the tenant's value comes from.
	worsenUnread = "unread"
	// worsenValueUnread (#3591): a reserved value of a tenant's own entry
	// its reader does not read (the verdict's value_unread); Finding is the
	// reader's problem. The key is read: its value is to fix, not the key
	// to remove.
	worsenValueUnread = "value_unread"
	// worsenDeadEntry (#3502): a tenant key's schedule with an entry
	// /metrics serves at no minute of the day that the tree did not have
	// (the verdict's dead_entry, by the entries as written); Finding is the
	// first entry's reason. The key is read: the entry is to remove or the
	// windows to reorder, not the key.
	worsenDeadEntry = "dead_entry"
	// worsenScheduleUnread (#3834): a reserved key of a tenant's own entry
	// written with a schedule its readers ignore — they read its `default`
	// alone (the verdict's schedule_unread, by the schedule as written);
	// Finding is the problem. The key and its default are read: the window
	// or expiry is to remove, not the key.
	worsenScheduleUnread = "schedule_unread"
)

// writeReasonWorsensTree is the call's reason (status refused, exit 1) when
// the batch would worsen the tree outside its own files' entries.
const writeReasonWorsensTree = "worsens_tree"

// writeWorsened is one thing the after-tree has that the before-tree did not.
type writeWorsened struct {
	Kind string `json:"kind"`
	// Path: the parse-tree entry's path, or the file a validation error
	// names ("" when it names none).
	Path string `json:"path"`
	// Tenant: the tenant a validation error is about ("" for none).
	Tenant string `json:"tenant"`
	// Finding: the validation finding's kind (kind finding), else "".
	Finding string `json:"finding"`
	Message string `json:"message"`
}

// worsening is everything the after-tree (after, its document afterDoc) has
// that the disk tree (disk) does not, sorted; batch is the call's paths.
// note takes the one line saying a check could not run (newlyUnread).
func worsening(disk, after *config.TreeScan, afterDoc *parseTreeDoc, overlays []config.FileOverlay, mkdirs bool,
	batch map[string]bool, note func(string)) ([]writeWorsened, error) {
	beforeDoc, err := parseTreeOf(disk)
	if err == nil {
		err = setVerdicts(beforeDoc.Entries, treeDropped(disk))
	}
	if err != nil {
		return nil, err
	}
	before := make(map[string]*parseTreeEntry, len(beforeDoc.Entries))
	for i := range beforeDoc.Entries {
		before[beforeDoc.Entries[i].Path] = &beforeDoc.Entries[i]
	}
	var out []writeWorsened
	for i := range afterDoc.Entries {
		a := &afterDoc.Entries[i]
		b := before[a.Path]
		if b != nil && !batch[a.Path] && len(a.Readers) < len(b.Readers) {
			out = append(out, writeWorsened{Kind: worsenReadersLost, Path: a.Path,
				Message: fmt.Sprintf("read by %v before the call, by %v after it", b.Readers, a.Readers)})
		}
		if a.Verdict != "" && (b == nil || b.Verdict != a.Verdict) {
			out = append(out, writeWorsened{Kind: worsenVerdict, Path: a.Path,
				Message: fmt.Sprintf("validation would name it under verdict %s", a.Verdict)})
		}
		if a.Problem != nil && (b == nil || b.Problem == nil || b.Problem.Kind != a.Problem.Kind) {
			out = append(out, writeWorsened{Kind: worsenProblem, Path: a.Path,
				Message: fmt.Sprintf("parse-tree would name a problem (%s: %s)", a.Problem.Kind, a.Problem.Message)})
		}
	}
	// One instant for both trees (#3368): a comparison across two clock
	// reads could name an `expires:` passing between them as the batch's.
	at := time.Now().UTC()
	was, wasDup, err := treeValidation(disk.AbsRoot, config.ScanOptions{}, disk, at)
	if err != nil {
		return nil, err
	}
	now, _, err := treeValidation(disk.AbsRoot, config.ScanOptions{Overlays: overlays, OverlayMakesDirs: mkdirs}, after, at)
	if err != nil {
		return nil, err
	}
	keys := make([]string, 0, len(now))
	for k, x := range now {
		if _, there := was[k]; there {
			continue
		}
		// A tenant declared twice before the call was not validated there:
		// its findings now have no baseline, and the call that makes it
		// validatable (removing a copy) repairs the tree.
		if x.Kind == worsenFinding && wasDup[x.Tenant] {
			continue
		}
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		out = append(out, now[k])
	}
	unread, err := newlyUnread(disk.AbsRoot, afterDoc, overlays, mkdirs, at, note)
	if err != nil {
		return nil, err
	}
	return append(out, unread...), nil
}

// newlyUnread is the key verdict's half of the comparison (#3489, #3016):
// every Block of the after-tree (keyverdict.Blocks: a key no reader reads at
// ERROR, wherever it is written — a tenant file, a profile, a subtree
// `_defaults.yaml`, a root platform file's `tenants:`) in a file the batch writes (an overlay with
// bytes: an edit, a create, a replace, a move's target, a copy) that the
// before-tree did not have (keyverdict.NewBlocks). So a batch that writes a
// misspelt key is refused, one that fixes such a key or touches another key
// beside it is not.
//
// ⛔ Scoped to the files the batch writes (ADR-038 decision 6): a batch that
// writes only a platform file — a root `_defaults.yaml` dropping a default,
// a `dimensions:` declaration removed — and so leaves a key in some other
// file read by nothing is not refused for that here; validate-config and
// the route generator's `--validate` (CI) refuse the tree. tenant-api's
// write gate (components/tenant-api/internal/gitops/key_verdict.go) refuses
// the same files' errors.
//
// What is new is keyverdict's one rule (Block.identity): a block about a
// tenant is the tenant and the key, wherever the file is — a tenant moved
// into a batch file, or onto a renamed profile, keeps its baseline — and
// one about no tenant (a profile no tenant binds) is the file and the key.
//
// The baseline is the before-tree judged as decidable even when it has no
// root platform file: its threshold keys enter the baseline, so a batch
// that adds the root carrier is not charged with the keys already there.
// A threshold key the after-tree cannot judge (no root platform file,
// decision 7) is no block here: a write into such a tree is the tool's
// ordinary case (a scaffold before its root file, a fixture), and the
// whole-tree gates — validate-config, the route generator's `--validate` —
// refuse the tree.
//
// A tenant declared in two files is set aside on both sides
// (keyVerdictOf's SkipDuplicated), with every tenant its files declare; the
// rest of the tree is judged. A tenant set aside before the call has no
// baseline, so its blocks after it are not new — removing a copy repairs
// the tree (treeValidation's rule). An empty before-tree is judged: no
// tenant, no block. A tree the verdict still cannot judge (a /metrics that
// cannot be gathered, a file the merge cannot decode) is no silent pass:
// note gets one line saying the check had no tree to judge and why.
//
// The after-tree is judged first: when no block is in a written file,
// nothing can be new and the before-tree is not read.
func newlyUnread(root string, afterDoc *parseTreeDoc, overlays []config.FileOverlay, mkdirs bool,
	at time.Time, note func(string)) ([]writeWorsened, error) {
	noBaseline := func(which string, why error) {
		note(fmt.Sprintf("the unread-key check judged no %s tree (%s); the keys this call writes "+
			"were not checked for a reader", which, strings.ReplaceAll(fmt.Sprint(why), "\n", " ")))
	}
	now, err := keyVerdictOf(root, keyVerdictOpts{scan: config.ScanOptions{Overlays: overlays,
		OverlayMakesDirs: mkdirs, SkipDuplicated: true}, rootPlatform: hasRootPlatformFile(afterDoc),
		profiles: rootProfilesOf(afterDoc), tenantFiles: tenantFileKeysOf(afterDoc)}, at)
	if err != nil {
		return nil, err
	}
	if !now.judged {
		noBaseline("after-call", now.why)
		return nil, nil
	}
	written := map[string]bool{}
	for _, o := range overlays {
		if !o.Remove {
			written[o.Rel] = true
		}
	}
	isWritten := func(file string) bool { return written[file] }
	if len(judgedBlocks(keyverdict.NewBlocks(keyverdict.Tree{}, now.tree, isWritten))) == 0 {
		return nil, nil
	}
	opts, err := treeFacts(root)
	if err != nil {
		return nil, err
	}
	opts.scan, opts.rootPlatform = config.ScanOptions{SkipDuplicated: true}, true
	was, err := keyVerdictOf(root, opts, at)
	if err != nil {
		return nil, err
	}
	if !was.judged {
		noBaseline("before-call", was.why)
		return nil, nil
	}
	var out []writeWorsened
	for _, b := range judgedBlocks(keyverdict.NewBlocks(was.tree, now.tree, isWritten)) {
		if b.Tenant != "" && was.setAside[b.Tenant] {
			continue
		}
		why := ""
		if b.Reason != "" {
			why = " (" + b.Reason + ")"
		}
		who := "tenant " + b.Tenant
		if b.Tenant == "" {
			who = "no tenant"
		}
		kind, what := worsenUnread, "would be unread"+why+" — no reader reads it"
		switch {
		case b.Kind == keyverdict.KindUnread && b.Hint != "":
			// #3829: a retired threshold key — the exporter may still read
			// it; the registry retired it. Name what replaces it.
			what = "would be a retired key — " + b.Hint
		case b.Kind == keyverdict.KindValueUnread:
			kind, what = worsenValueUnread, "would have a value its reader does not read"+why+" — fix the value"
		case b.Kind == keyverdict.KindDeadEntry:
			kind, what = worsenDeadEntry, "would have a schedule entry /metrics serves at no minute of the day"+why+
				" — remove it or reorder the windows (the first window that contains a minute serves it)"
		case b.Kind == keyverdict.KindScheduleUnread:
			kind, what = worsenScheduleUnread, "would be written with a schedule its readers ignore"+why+
				" — only its `default` is read; remove the window or expiry"
		}
		out = append(out, writeWorsened{Kind: kind, Path: b.File, Tenant: b.Tenant, Finding: b.Reason,
			Message: fmt.Sprintf("%s: key %q %s", who, b.Key, what)})
	}
	return out, nil
}

// judgedBlocks is blocks without the keys the verdict could not judge (see
// newlyUnread: a writer refuses what it judged, not a tree with no root
// platform file).
func judgedBlocks(blocks []keyverdict.Block) []keyverdict.Block {
	out := blocks[:0:0]
	for _, b := range blocks {
		if b.Kind != keyverdict.Undecidable {
			out = append(out, b)
		}
	}
	return out
}

// treeValidation is what `da-guard --config-dir <root>` with no other flag
// reports as an error on the tree opts makes it read, by stable key: the
// same ScopeEffective (ScopeEffectiveWith), the same flags' defaults, the
// same buildCheckInput and guard check as runWith — and the cardinality cap
// that tree's own root carrier sets (limits: a cold scan of the same tree).
// A tenant declared in two files stops validation there (exit 2); here it is
// set aside instead (ScanOptions.SkipDuplicated, the walk's own record) and
// reported as one, so every other tenant is still validated. duplicated is
// those tenant ids.
func treeValidation(root string, opts config.ScanOptions, limits *config.TreeScan, at time.Time) (
	out map[string]writeWorsened, duplicated map[string]bool, err error) {
	out, duplicated = map[string]writeWorsened{}, map[string]bool{}
	opts.SkipDuplicated = true
	scoped, err := config.ScopeEffectiveWith(root, opts, at)
	var de *config.DecodeError
	switch {
	case errors.As(err, &de):
		out["decode\x00"+de.Path] = writeWorsened{Kind: worsenFinding, Path: de.Path, Finding: "decode_error",
			Message: "the exporter's merge cannot decode it: " + de.Error()}
		return out, duplicated, nil
	case err != nil:
		return nil, nil, err
	}
	for id, dup := range scoped.Duplicated {
		duplicated[id] = true
		a, _ := relTo(limits.AbsRoot, dup.PathA)
		b, _ := relTo(limits.AbsRoot, dup.PathB)
		out["duplicate\x00"+id] = writeWorsened{Kind: worsenDuplicateTenant, Path: b, Tenant: id,
			Message: fmt.Sprintf("tenant %s is declared in %s and in %s", id, a, b)}
	}
	f, err := parseFlags([]string{"--config-dir", root}, io.Discard)
	if err != nil {
		return nil, nil, err
	}
	limit, _, lerr := limits.RootMaxMetricsPerTenant()
	if lerr != nil {
		// A root carrier the exporter drops: it enforces the built-in cap
		// (runWith's rootCarrierDropped answer).
		limit = config.EffectiveMaxMetricsPerTenant(0)
	}
	if limit < 0 {
		limit = 0
	}
	f.cardinalityLimit = limit
	report, err := guard.CheckDefaultsImpact(buildCheckInput(scoped, f, at))
	if err != nil {
		return nil, nil, err
	}
	spellFindingsAsWritten(report, scoped)
	// A `_custom_alerts[i]` finding is keyed by its ordinal among the
	// tenant's findings of that kind, not by i (#3235): deleting or adding an
	// entry before an entry the exporter already rejected shifts its index,
	// and the tree is no worse. One more such finding still is.
	ordinal := map[string]int{}
	for _, fd := range report.Findings {
		if fd.Severity != guard.SeverityError {
			continue
		}
		field := fd.Field
		if strings.HasPrefix(field, "_custom_alerts[") {
			k := fd.Kind.String() + "\x00" + fd.TenantID
			field = fmt.Sprintf("_custom_alerts#%d", ordinal[k])
			ordinal[k]++
		}
		out["finding\x00"+fd.Kind.String()+"\x00"+fd.TenantID+"\x00"+field] = writeWorsened{
			Kind: worsenFinding, Tenant: fd.TenantID, Finding: fd.Kind.String(), Message: fd.Message}
	}
	return out, duplicated, nil
}
