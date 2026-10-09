package config

import (
	"path/filepath"
	"reflect"
	"slices"
	"sync"
	"testing"
	"time"
)

// #1939: the mtime fast-path hands the prior's *TreeFile to the next scan
// instead of building a copy, so one TreeFile can sit in the retained prior
// and in every later scan at once. These tests pin the two halves of that
// contract: the sharing happens (or the allocation win is gone), and nothing
// the walk or ReleaseData does afterwards writes to a shared TreeFile (or
// the retained prior silently changes under the manager).

// treeFileSnapshot is a deep copy of what a TreeFile holds, so a later write
// to the original — even through a shared slice or pointer — shows up as a
// difference.
type treeFileSnapshot struct {
	f         TreeFile
	linkStat  *FileStat
	data      []byte
	tenantIDs []string
}

func snapshotFiles(s *TreeScan) map[string]treeFileSnapshot {
	out := make(map[string]treeFileSnapshot, len(s.Files))
	for k, f := range s.Files {
		snap := treeFileSnapshot{f: *f, data: slices.Clone(f.Data), tenantIDs: slices.Clone(f.TenantIDs)}
		if f.LinkStat != nil {
			ls := *f.LinkStat
			snap.linkStat = &ls
		}
		out[k] = snap
	}
	return out
}

func requireFilesUnchanged(t *testing.T, label string, s *TreeScan, want map[string]treeFileSnapshot) {
	t.Helper()
	got := snapshotFiles(s)
	for k, w := range want {
		g, ok := got[k]
		if !ok {
			t.Errorf("%s: %s vanished from the retained scan", label, k)
			continue
		}
		if !reflect.DeepEqual(g, w) {
			t.Errorf("%s: retained scan's %s was rewritten by a later scan:\n got %+v\nwant %+v", label, k, g, w)
		}
	}
}

// TestScanDirTree_CarriedTreeFileIsSharedAndNeverRewritten: two scans after a
// released prior leave every one of the prior's TreeFiles exactly as it was
// — the unchanged file (shared with both later scans) and the edited one
// (replaced in the later scans, never written in place).
//
// Mutants measured red (#1939): building the read branch's TreeFile in the
// prior's struct (`f = pf` whenever a prior entry exists) rewrites the edited
// file's Hash/Data/TenantIDs in the prior; dropping the pf.Data == nil
// condition shares a file that still holds bytes.
func TestScanDirTree_CarriedTreeFileIsSharedAndNeverRewritten(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	const age = time.Hour
	agedWrite(t, filepath.Join(root, "t-keep.yaml"), "tenants:\n  t-keep: {}\n", age)
	agedWrite(t, filepath.Join(root, "nested", "t-edit.yaml"), "tenants:\n  t-edit: {}\n", age)
	agedWrite(t, filepath.Join(root, "_defaults.yaml"), "defaults: {}\n", age)

	prior, err := ScanDirTree(root, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	prior.ReleaseData() // the manager's contract for a retained scan
	want := snapshotFiles(prior)

	// A different size, so the stat moves and the file is read.
	agedWrite(t, filepath.Join(root, "nested", "t-edit.yaml"), "tenants:\n  t-edit: {}\n  t-new: {}\n", age)

	next, err := ScanDirTree(root, prior, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"t-keep.yaml", "_defaults.yaml"} {
		if !next.Reused(k) {
			t.Fatalf("%s did not take the fast-path (fixture not aged?)", k)
		}
		if next.Files[k] != prior.Files[k] {
			t.Errorf("%s: the fast-path built a new TreeFile instead of carrying the prior's (#1939)", k)
		}
	}
	if next.Reused("nested/t-edit.yaml") || !next.Parsed("nested/t-edit.yaml") {
		t.Errorf("t-edit.yaml was not read and parsed after its size changed")
	}
	if next.Files["nested/t-edit.yaml"] == prior.Files["nested/t-edit.yaml"] {
		t.Errorf("t-edit.yaml was read but the prior's TreeFile was kept")
	}
	if got := next.Files["nested/t-edit.yaml"].TenantIDs; !reflect.DeepEqual(got, []string{"t-edit", "t-new"}) {
		t.Errorf("t-edit.yaml declarations = %v, want [t-edit t-new]", got)
	}
	next.ReleaseData()

	// A third scan from the second: still nothing written to the first.
	third, err := ScanDirTree(root, next, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	third.ReleaseData()

	requireFilesUnchanged(t, "after two later scans", prior, want)
	// The per-scan facts stay the prior's own, though the TreeFiles are shared.
	for k := range prior.Files {
		if prior.Reused(k) {
			t.Errorf("cold scan's %s reads as Reused after later scans carried it", k)
		}
	}
	if !prior.Parsed("t-keep.yaml") || next.Parsed("t-keep.yaml") || third.Parsed("t-keep.yaml") {
		t.Errorf("Parsed(t-keep.yaml) = %v/%v/%v across the three scans, want true/false/false",
			prior.Parsed("t-keep.yaml"), next.Parsed("t-keep.yaml"), third.Parsed("t-keep.yaml"))
	}
}

// TestScanDirTree_PriorHoldingBytesOrOtherRootIsNotShared: the two TreeFile
// fields the fast-path's own conditions do not imply. A prior never
// ReleaseData'd still holds Data — sharing it would hand the next scan bytes
// it did not read (and its ReleaseData would then write to the prior). A
// prior whose AbsPath differs for the same RelKey (the resolved root moved)
// would carry a stale path. Both must get a fresh TreeFile that is still a
// fast-path carry.
func TestScanDirTree_PriorHoldingBytesOrOtherRootIsNotShared(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	agedWrite(t, filepath.Join(root, "t-a.yaml"), "tenants:\n  t-a: {}\n", time.Hour)

	unreleased, err := ScanDirTree(root, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	if unreleased.Files["t-a.yaml"].Data == nil {
		t.Fatal("cold scan cached no bytes (fixture broken)")
	}
	want := snapshotFiles(unreleased)
	next, err := ScanDirTree(root, unreleased, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	if !next.Reused("t-a.yaml") {
		t.Fatal("t-a.yaml did not take the fast-path (fixture not aged?)")
	}
	if nf := next.Files["t-a.yaml"]; nf == unreleased.Files["t-a.yaml"] || nf.Data != nil {
		t.Errorf("a prior file still holding bytes was shared (same=%v, Data=%q)", nf == unreleased.Files["t-a.yaml"], nf.Data)
	}
	next.ReleaseData()
	requireFilesUnchanged(t, "unreleased prior", unreleased, want)

	moved := &TreeScan{Files: map[string]*TreeFile{}}
	pf := *next.Files["t-a.yaml"]
	pf.AbsPath = filepath.Join(t.TempDir(), "t-a.yaml")
	moved.Files["t-a.yaml"] = &pf
	again, err := ScanDirTree(root, moved, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	af := again.Files["t-a.yaml"]
	if !again.Reused("t-a.yaml") {
		t.Fatal("t-a.yaml did not take the fast-path against the moved prior")
	}
	if af == &pf || af.AbsPath != next.Files["t-a.yaml"].AbsPath {
		t.Errorf("a prior file under another root was shared: AbsPath %q, want %q", af.AbsPath, next.Files["t-a.yaml"].AbsPath)
	}
}

// TestScanDirTree_ConcurrentScansFromOneRetainedPrior is the -race half: the
// tenant-api Writer runs read and write walks concurrently from ONE retained
// prior, each ReleaseData'ing its own result, while readers read that prior.
// Any write to a shared TreeFile (an unconditional `f.Data = nil` in
// ReleaseData was the measured mutant) is a data race here.
func TestScanDirTree_ConcurrentScansFromOneRetainedPrior(t *testing.T) {
	t.Parallel()
	root := t.TempDir()
	for _, n := range []string{"t-1.yaml", "t-2.yaml", "sub/t-3.yaml"} {
		agedWrite(t, filepath.Join(root, filepath.FromSlash(n)), "tenants:\n  "+filepath.Base(n[:len(n)-5])+": {}\n", time.Hour)
	}
	prior, err := ScanDirTree(root, nil, nil, discardLogger)
	if err != nil {
		t.Fatal(err)
	}
	prior.ReleaseData()
	want := snapshotFiles(prior)

	var wg sync.WaitGroup
	for range 4 {
		wg.Add(2)
		go func() {
			defer wg.Done()
			s, err := ScanDirTree(root, prior, nil, discardLogger)
			if err != nil {
				t.Error(err)
				return
			}
			s.ReleaseData()
		}()
		go func() {
			defer wg.Done()
			for _, f := range prior.Files {
				_, _, _ = f.Hash, f.Data, f.TenantIDs
			}
		}()
	}
	wg.Wait()
	requireFilesUnchanged(t, "after concurrent scans", prior, want)
}
