package gitops

// #2939, ADR-036 §4: every write path — and every preview of one — judges the
// tree AFTER the write; a conf.d entry the walker cannot read refuses it
// unless the write replaces that entry (refuseUnreadableTree). This file
// holds the portable shapes (a dangling symlink); the special-file,
// read_error and walk_error shapes are in the _linux twin.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// treeVerdictModes are the write and preview entry points that judge the
// tree, each run for tenantID with a valid body; a refusal is their error.
var treeVerdictModes = []string{"Write", "WriteMerged", "WritePR", "WritePRBatch", "WriteBatch", "DryRunValidate", "Diff"}

// directTreeModes leave out the PR modes, which judge the base as git holds
// it (a fixture's untracked entry is not there).
var directTreeModes = []string{"Write", "WriteMerged", "WriteBatch", "DryRunValidate", "Diff"}

// treeVerdictVia runs mode for tenantID with a valid body and returns its
// refusal. For WriteBatch / WritePRBatch the refusal is the op's own
// (BatchRejectedError's outcome), so it is compared like the others.
func treeVerdictVia(t *testing.T, mode string, w *Writer, tenantID string) error {
	t.Helper()
	ctx := context.Background()
	body := tenantBody(tenantID)
	merge := func([]byte) (string, error) { return body, nil }
	opErr := func(err error) error {
		var rej *BatchRejectedError
		if errors.As(err, &rej) {
			return rej.Outcomes[0].Err
		}
		return err
	}
	switch mode {
	case "WriteBatch":
		_, err := w.WriteBatch(ctx, []BatchOp{{TenantID: tenantID, Merge: merge}}, "op@example.com")
		return opErr(err)
	case "WritePRBatch":
		_, err := w.WritePRBatch(ctx, []PRBatchOp{{TenantID: tenantID, Merge: merge}}, "op@example.com")
		return opErr(err)
	case "DryRunValidate":
		errs, _, err := w.DryRunValidate(ctx, tenantID, body)
		if err == nil && len(errs) > 0 {
			t.Fatalf("DryRunValidate: unexpected validation errors %q", errs)
		}
		return err
	case "Diff":
		_, err := w.Diff(tenantID, body)
		return err
	}
	return writeVia(t, mode, w, tenantID)
}

// assertTreeUnreadable checks err is the whole-tree refusal naming exactly
// want.
func assertTreeUnreadable(t *testing.T, mode string, err error, want []cfg.UnreadableFile) {
	t.Helper()
	var te *TreeUnreadableError
	if !errors.As(err, &te) || !errors.Is(err, ErrTenantTreeUnreadable) {
		t.Fatalf("%s: err = %v, want *TreeUnreadableError (ErrTenantTreeUnreadable)", mode, err)
	}
	if !reflect.DeepEqual(te.Entries, want) {
		t.Errorf("%s: entries = %v, want %v", mode, te.Entries, want)
	}
}

// (c): a dangling symlink elsewhere in conf.d refuses every write and every
// preview — the route generator refuses the tree it would leave.
func TestTreeUnreadable_DanglingSymlinkElsewhereRefusesEveryPath(t *testing.T) {
	for _, mode := range treeVerdictModes {
		t.Run(mode, func(t *testing.T) {
			dir := seedTreeRepo(t, map[string]string{"dang-t.yaml": tenantBody("dang-t")})
			// Committed, so the PR modes' fresh base carries it too.
			if err := os.Symlink("missing-target.yaml", filepath.Join(dir, "gone.yaml")); err != nil {
				t.Skipf("symlink: %v", err)
			}
			gitRun(t, dir, "add", "-A")
			gitRun(t, dir, "commit", "-m", "dangling link")
			head := gitRevParse(t, dir, "HEAD")
			err := treeVerdictVia(t, mode, NewWriter(dir, dir), "dang-t")
			assertTreeUnreadable(t, mode, err, []cfg.UnreadableFile{{RelKey: "gone.yaml", Reason: cfg.UnreadableStatError}})
			if got := gitRevParse(t, dir, "HEAD"); got != head {
				t.Errorf("HEAD moved %s → %s", head, got)
			}
			if got, _ := os.ReadFile(filepath.Join(dir, "dang-t.yaml")); string(got) != tenantBody("dang-t") {
				t.Errorf("dang-t.yaml changed: %q", got)
			}
		})
	}
}

// (a) ADR-036 §4's repair path: the write's OWN tenant file being a dangling
// symlink does not refuse it — the write replaces the directory entry, and
// the tree after it is readable. Every write path and preview agrees, the PR
// modes included. The link points OUTSIDE conf.d, so a write that followed
// it (os.WriteFile) would create a file there instead of repairing conf.d.
func TestTreeUnreadable_DanglingTargetIsTheRepairPath(t *testing.T) {
	for _, mode := range append(append([]string{}, directTreeModes...), "WritePR", "WritePRBatch") {
		t.Run(mode, func(t *testing.T) {
			dir := seedTreeRepo(t, map[string]string{"other-t.yaml": tenantBody("other-t")})
			outside := filepath.Join(t.TempDir(), "gone.yaml")
			target := filepath.Join(dir, "fix-t.yaml")
			if err := os.Symlink(outside, target); err != nil {
				t.Skipf("symlink: %v", err)
			}
			pr := mode == "WritePR" || mode == "WritePRBatch"
			if pr { // the PR modes cut from the base git holds
				gitRun(t, dir, "add", "-A")
				gitRun(t, dir, "commit", "-m", "dangling tenant file")
			}
			w := NewWriter(dir, dir)
			var branch string
			ctx := context.Background()
			body := tenantBody("fix-t")
			switch mode {
			case "WritePR":
				res, err := w.WritePR(ctx, "fix-t", "op@example.com", body)
				if err != nil {
					t.Fatalf("WritePR: %v, want accepted", err)
				}
				branch = res.BranchName
			case "WritePRBatch":
				res, err := w.WritePRBatch(ctx, []PRBatchOp{{TenantID: "fix-t",
					Merge: func([]byte) (string, error) { return body, nil }}}, "op@example.com")
				if err != nil {
					t.Fatalf("WritePRBatch: %v, want accepted", err)
				}
				branch = res.BranchName
			default:
				if err := treeVerdictVia(t, mode, w, "fix-t"); err != nil {
					t.Fatalf("%s: err = %v, want the write (or its preview) accepted", mode, err)
				}
			}
			if _, err := os.Lstat(outside); !errors.Is(err, os.ErrNotExist) {
				t.Errorf("%s wrote through the link, outside conf.d: Lstat(%s) = %v", mode, outside, err)
			}
			if mode == "DryRunValidate" || mode == "Diff" {
				return
			}
			if pr {
				// The branch carries the repair: a regular file (mode 100644,
				// not a 120000 symlink) holding the body.
				remote := gitOut(t, dir, "remote", "get-url", "origin")
				if ls := gitOut(t, remote, "ls-tree", branch, "fix-t.yaml"); !strings.HasPrefix(ls, "100644 blob ") {
					t.Fatalf("%s: fix-t.yaml on %s = %q, want a regular file", mode, branch, ls)
				}
				if got := gitOut(t, remote, "show", branch+":fix-t.yaml"); got+"\n" != body {
					t.Errorf("%s: fix-t.yaml on %s = %q, want the body", mode, branch, got)
				}
				return
			}
			fi, err := os.Lstat(target)
			if err != nil || !fi.Mode().IsRegular() {
				t.Fatalf("fix-t.yaml after the write: %v, %v; want a regular file", fi, err)
			}
			if got, _ := os.ReadFile(target); string(got) != body {
				t.Errorf("fix-t.yaml = %q, want the body", got)
			}
			scan, err := cfg.ScanDirTree(dir, nil, nil, discardScanLogger)
			if err != nil {
				t.Fatal(err)
			}
			if len(scan.Unreadable) != 0 {
				t.Errorf("tree after the repair still has unreadable entries: %v", scan.Unreadable)
			}
		})
	}
}

// (a) for a batch: the second op's file is a dangling symlink that the
// first op's tenant does not own. The batch's targets are excluded as a set,
// so the first op is not refused for an entry the second replaces.
func TestTreeUnreadable_BatchExcludesEveryTarget(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"bt-a.yaml": tenantBody("bt-a")})
	if err := os.Symlink("missing-target.yaml", filepath.Join(dir, "bt-b.yaml")); err != nil {
		t.Skipf("symlink: %v", err)
	}
	w := NewWriter(dir, dir)
	ops := []BatchOp{
		{TenantID: "bt-a", Merge: func([]byte) (string, error) { return "tenants:\n  bt-a:\n    _silent_mode: \"critical\"\n", nil }},
		{TenantID: "bt-b", Merge: func([]byte) (string, error) { return tenantBody("bt-b"), nil }},
	}
	if _, err := w.WriteBatch(context.Background(), ops, "op@example.com"); err != nil {
		t.Fatalf("WriteBatch: %v", err)
	}
	scan, err := cfg.ScanDirTree(dir, nil, nil, discardScanLogger)
	if err != nil {
		t.Fatal(err)
	}
	if len(scan.Unreadable) != 0 {
		t.Errorf("tree after the batch still has unreadable entries: %v", scan.Unreadable)
	}
}

// (e) the preview and the write it previews give the same verdict on the
// same tree — refused with the same entries, or both accepted.
func TestTreeUnreadable_PreviewMatchesWrite(t *testing.T) {
	for _, c := range []struct {
		name, link, tenant string
		want               []cfg.UnreadableFile
	}{
		{"elsewhere", "gone.yaml", "pv-t", []cfg.UnreadableFile{{RelKey: "gone.yaml", Reason: cfg.UnreadableStatError}}},
		{"target", "pv-t.yaml", "pv-t", nil},
	} {
		t.Run(c.name, func(t *testing.T) {
			dir := seedTreeRepo(t, map[string]string{"keep-t.yaml": tenantBody("keep-t")})
			if err := os.Symlink("missing-target.yaml", filepath.Join(dir, c.link)); err != nil {
				t.Skipf("symlink: %v", err)
			}
			w := NewWriter(dir, dir)
			verdicts := map[string]error{}
			for _, mode := range []string{"DryRunValidate", "Diff", "Write"} { // previews first: Write changes the tree
				verdicts[mode] = treeVerdictVia(t, mode, w, c.tenant)
			}
			for mode, err := range verdicts {
				if c.want == nil {
					if err != nil {
						t.Errorf("%s: err = %v, want accepted", mode, err)
					}
					continue
				}
				assertTreeUnreadable(t, mode, err, c.want)
			}
		})
	}
}

// The error the handlers log (logTreeUnreadable) names every entry and its
// reason, whatever the caller is shown (#2939 D3a).
func TestTreeUnreadableError_NamesEveryEntry(t *testing.T) {
	err := refuseUnreadableTree(&cfg.TreeScan{Unreadable: []cfg.UnreadableFile{
		{RelKey: "a.yaml", Reason: cfg.UnreadableSpecialFile},
		{RelKey: "t.yaml", Reason: cfg.UnreadableStatError},
		{RelKey: "team", Reason: cfg.UnreadableWalkError},
	}}, map[string]bool{"t.yaml": true})
	want := ErrTenantTreeUnreadable.Error() + ": a.yaml (special_file), team (walk_error)"
	if err == nil || err.Error() != want {
		t.Fatalf("err = %v, want %q (the target t.yaml excluded)", err, want)
	}
}
