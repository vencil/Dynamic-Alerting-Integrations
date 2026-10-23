package main

// write_worsen.go — the tree-scope rule of `da-guard edit` / `emit` (#3206):
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
//     a copy repairs the tree.
//
// ⚠️ Not compared: validation warnings (they do not fail validation), and
// what only the route generator judges (exit 4's evidence beyond the
// parse-tree problems compared above).

import (
	"errors"
	"fmt"
	"io"
	"sort"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// The closed set of writeWorsened.Kind values.
const (
	worsenReadersLost     = "readers_lost"
	worsenVerdict         = "verdict"
	worsenProblem         = "problem"
	worsenDuplicateTenant = "duplicate_tenant"
	worsenFinding         = "finding" // a validation error; Finding names its kind
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
func worsening(disk, after *config.TreeScan, afterDoc *parseTreeDoc, overlays []config.FileOverlay, mkdirs bool,
	batch map[string]bool) ([]writeWorsened, error) {
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
	was, wasDup, err := treeValidation(disk.AbsRoot, config.ScanOptions{}, disk)
	if err != nil {
		return nil, err
	}
	now, _, err := treeValidation(disk.AbsRoot, config.ScanOptions{Overlays: overlays, OverlayMakesDirs: mkdirs}, after)
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
	return out, nil
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
func treeValidation(root string, opts config.ScanOptions, limits *config.TreeScan) (
	out map[string]writeWorsened, duplicated map[string]bool, err error) {
	out, duplicated = map[string]writeWorsened{}, map[string]bool{}
	opts.SkipDuplicated = true
	scoped, err := config.ScopeEffectiveWith(root, opts)
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
	report, err := guard.CheckDefaultsImpact(buildCheckInput(scoped, f))
	if err != nil {
		return nil, nil, err
	}
	spellFindingsAsWritten(report, scoped)
	for _, fd := range report.Findings {
		if fd.Severity != guard.SeverityError {
			continue
		}
		out["finding\x00"+fd.Kind.String()+"\x00"+fd.TenantID+"\x00"+fd.Field] = writeWorsened{
			Kind: worsenFinding, Tenant: fd.TenantID, Finding: fd.Kind.String(), Message: fd.Message}
	}
	return out, duplicated, nil
}
