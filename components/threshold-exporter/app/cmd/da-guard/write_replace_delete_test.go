package main

// `da-guard write` replace and delete (#3206): a replace is a whole new text
// at a path that may hold a file (nothing of it kept, put in place as an
// edit is), a delete removes a file and is undone by linking it back. Tenant
// ids are fixture names (CLAUDE.md #9).

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const tenantsT3 = `{"map":[["tenants",{"map":[["t3",{"map":[["cpu",{"int":"5"}]]}]]}]]}`

// seenAsRead reads as readRegular does, but hands the judgement other bytes
// than the disk holds: the file "changed" after it was read.
func seenAsRead(abs string) ([]byte, os.FileInfo, bool, error) {
	d, fi, regular, err := readRegular(abs)
	if err == nil && regular {
		d = append(d, "# as it was read\n"...)
	}
	return d, fi, regular, err
}

func TestWrite_ReplaceWritesTheWholeTextAndKeepsTheMode(t *testing.T) {
	old := "# old header\r\ntenants:\r\n  t3:\r\n    cpu: 1 # old\r\n    mem: 2\r\n"
	root := writeTree(t, map[string]string{"t.yaml": old})
	abs := filepath.Join(root, "t.yaml")
	if err := os.Chmod(abs, 0o640); err != nil {
		t.Fatal(err)
	}
	in := `{"files":[{"path":"t.yaml","create":` + tenantsT3 + `,"replace":true,"head_comment":"generated"}]}`
	code, res := runBatch(t, writeCmd, in, "--config-dir", root, "--dry-run")
	want := "# generated\n\ntenants:\n  t3:\n    cpu: 5\n"
	if code != exitOK || res.Files[0].Kind != writeKindReplace || res.Files[0].Status != writeStatusDryRun ||
		res.Files[0].Content == nil || *res.Files[0].Content != want {
		t.Fatalf("dry run: exit %d %+v", code, res)
	}
	code, res = runBatch(t, writeCmd, in, "--config-dir", root)
	if code != exitOK || res.Status != writeStatusWritten || !reflect.DeepEqual(statuses(res), []string{"written"}) {
		t.Fatalf("exit %d %v (%v)", code, statuses(res), res.Message)
	}
	// Exactly the value and its comments: no old node, no old comment, LF.
	if got := snapshot(t, root); len(got) != 1 || got["t.yaml"] != want {
		t.Fatalf("tree %q", got)
	}
	if fi, err := os.Stat(abs); err != nil || fi.Mode().Perm() != 0o640 {
		t.Fatalf("mode %v %v, want the original's 0640", fi, err)
	}
	// The same text again is unchanged: nothing written, the file as it is.
	before, _ := os.Stat(abs)
	code, res = runBatch(t, writeCmd, in, "--config-dir", root)
	if code != exitOK || res.Status != writeStatusUnchanged || !reflect.DeepEqual(statuses(res), []string{"unchanged"}) {
		t.Fatalf("again: exit %d %v (%v)", code, statuses(res), res.Message)
	}
	if after, err := os.Stat(abs); err != nil || !os.SameFile(before, after) {
		t.Fatalf("an unchanged replace wrote the file: %v", err)
	}
}

func TestWrite_ReplaceOfAMissingPathIsACreate(t *testing.T) {
	root := writeTree(t, map[string]string{"x.yaml": "tenants:\n  t9: {}\n"})
	in := `{"files":[{"path":"new/t.yaml","create":` + tenantsT3 + `,"replace":true}]}`
	code, res := runBatch(t, writeCmd, in, "--config-dir", root)
	if code != exitOK || res.Status != writeStatusWritten || res.Files[0].Kind != writeKindReplace {
		t.Fatalf("exit %d %+v (%v)", code, statuses(res), res.Message)
	}
	if got := snapshot(t, root)["new/t.yaml"]; got != "tenants:\n  t3:\n    cpu: 5\n" {
		t.Fatalf("written %q", got)
	}
	if fi, err := os.Stat(filepath.Join(root, "new", "t.yaml")); err != nil || fi.Mode().Perm() != 0o644 {
		t.Fatalf("mode %v %v, want a create's 0644", fi, err)
	}
}

// TestWrite_ReplaceRepairsAFileParseTreeCannotRead: the old text is no base,
// so one parse-tree cannot read (an edit's original_unreadable) is replaced.
func TestWrite_ReplaceRepairsAFileParseTreeCannotRead(t *testing.T) {
	root := writeTree(t, map[string]string{"t.yaml": "tenants: [\n"})
	edit := `{"files":[{"path":"t.yaml","ops":[{"op":"set","path":["tenants"],"value":{"map":[]}}]}]}`
	if code, res := runBatch(t, writeCmd, edit, "--config-dir", root); code != exitFindings ||
		!reflect.DeepEqual(statuses(res), []string{"refused/original_unreadable"}) {
		t.Fatalf("edit: exit %d %v", code, statuses(res))
	}
	in := `{"files":[{"path":"t.yaml","create":` + tenantsT3 + `,"replace":true}]}`
	code, res := runBatch(t, writeCmd, in, "--config-dir", root)
	if code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %v (%v)", code, statuses(res), res.Message)
	}
	if got := snapshot(t, root)["t.yaml"]; got != "tenants:\n  t3:\n    cpu: 5\n" {
		t.Fatalf("written %q", got)
	}
}

func TestWrite_ReplaceRefusals(t *testing.T) {
	cases := []struct {
		name   string
		tree   map[string]string
		setup  func(t *testing.T, root string)
		in     string
		code   int
		reason string // the file's, or the call's for worsens_tree / bad_input
	}{
		{"a symlink", map[string]string{"real.yaml": "tenants:\n  t1: {}\n"},
			func(t *testing.T, root string) {
				if err := os.Symlink("real.yaml", filepath.Join(root, "t.yaml")); err != nil {
					t.Skip(err)
				}
			},
			`{"files":[{"path":"t.yaml","create":` + tenantsT3 + `,"replace":true}]}`, exitFindings, writeReasonSymlink},
		{"a directory", map[string]string{"t.yaml/x.yaml": "tenants:\n  t1: {}\n"}, nil,
			`{"files":[{"path":"t.yaml","create":` + tenantsT3 + `,"replace":true}]}`, exitFindings, writeReasonNotRead},
		{"a name no reader reads", map[string]string{"notes.txt": "x\n"}, nil,
			`{"files":[{"path":"notes.txt","create":` + tenantsT3 + `,"replace":true}]}`, exitFindings, writeReasonNotRead},
		// t3 is declared in another file already: the tree after it has a
		// tenant declared twice.
		{"a tenant declared twice", map[string]string{"a.yaml": "tenants:\n  t3: {}\n", "t.yaml": "tenants:\n  t1: {}\n"}, nil,
			`{"files":[{"path":"t.yaml","create":` + tenantsT3 + `,"replace":true}]}`, exitFindings, writeReasonWorsensTree},
		{"replace false", map[string]string{"t.yaml": "tenants: {}\n"}, nil,
			`{"files":[{"path":"t.yaml","create":` + tenantsT3 + `,"replace":false}]}`, exitCallerErr, "bad_input"},
		{"replace with ops", map[string]string{"t.yaml": "tenants: {}\n"}, nil,
			`{"files":[{"path":"t.yaml","ops":[],"replace":true}]}`, exitCallerErr, "bad_input"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, tc.tree)
			if tc.setup != nil {
				tc.setup(t, root)
			}
			before := snapshot(t, root)
			code, res := runBatch(t, writeCmd, tc.in, "--config-dir", root)
			got := callReason(res)
			if got == "" && len(res.Files) == 1 {
				got = reasonOf(res.Files[0])
			}
			if code != tc.code || got != tc.reason {
				t.Fatalf("exit %d reason %q (want %d %q) files %v worsened %v", code, got, tc.code, tc.reason,
					statuses(res), worsenedOf(res))
			}
			sameTree(t, before, snapshot(t, root))
		})
	}
}

// TestWrite_ReplaceAndDeleteRefuseAFileChangedAfterItWasRead: the bytes
// judged are compared once more before anything is put in place.
func TestWrite_ReplaceAndDeleteRefuseAFileChangedAfterItWasRead(t *testing.T) {
	for name, in := range map[string]string{
		"replace": `{"files":[{"path":"t.yaml","create":` + tenantsT3 + `,"replace":true}]}`,
		"delete":  `{"files":[{"path":"t.yaml","delete":true}]}`,
	} {
		t.Run(name, func(t *testing.T) {
			root := writeTree(t, map[string]string{"t.yaml": "tenants:\n  t1: {}\n", "x.yaml": "tenants:\n  t9: {}\n"})
			before := snapshot(t, root)
			code, res := runSeamed(t, &writer{cmd: writeCmd, read: seenAsRead}, in, "--config-dir", root)
			if code != exitFindings || res.Status != writeStatusRefused ||
				!reflect.DeepEqual(statuses(res), []string{"refused/changed_during_write"}) {
				t.Fatalf("exit %d %s %v (%v)", code, res.Status, statuses(res), res.Message)
			}
			sameTree(t, before, snapshot(t, root))
		})
	}
}

// TestWrite_AReplaceIsRenamedAsAnEditIs: a replace of an existing file goes
// with the edits — its rename is not undone when a later one fails.
func TestWrite_AReplaceIsRenamedAsAnEditIs(t *testing.T) {
	root := writeTree(t, map[string]string{"t.yaml": "tenants:\n  t3: {}\n", "a.yaml": "tenants:\n  t1:\n    cpu: \"5\"\n"})
	n := 0
	w := &writer{cmd: writeCmd, rename: func(o, nn string) error {
		if n++; n == 2 {
			return errors.New("disk full")
		}
		return os.Rename(o, nn)
	}}
	in := `{"files":[{"path":"t.yaml","create":` + tenantsT3 + `,"replace":true},` +
		`{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1","cpu"]}]}]}`
	code, res := runSeamed(t, w, in, "--config-dir", root)
	if want := []string{"written", "caller_error/write_failed"}; code != exitCallerErr ||
		callReason(res) != writeReasonPartialWrite || !reflect.DeepEqual(statuses(res), want) {
		t.Fatalf("exit %d reason %q files %v, want %v", code, callReason(res), statuses(res), want)
	}
	got := snapshot(t, root)
	if len(got) != 2 || got["t.yaml"] != "tenants:\n  t3:\n    cpu: 5\n" || got["a.yaml"] != "tenants:\n  t1:\n    cpu: \"5\"\n" {
		t.Fatalf("tree %q", got)
	}
}

func TestWrite_DeleteRemovesTheFile(t *testing.T) {
	root := writeTree(t, map[string]string{"t.yaml": "tenants:\n  t1: {}\n", "x.yaml": "tenants:\n  t9: {}\n"})
	in := `{"files":[{"path":"t.yaml","delete":true}]}`
	code, res := runBatch(t, writeCmd, in, "--config-dir", root, "--dry-run")
	if code != exitOK || res.Status != writeStatusDryRun || res.Files[0].Kind != writeKindDelete || res.Files[0].Content != nil {
		t.Fatalf("dry run: exit %d %+v", code, res)
	}
	if len(snapshot(t, root)) != 2 {
		t.Fatal("a dry run deleted")
	}
	code, res = runBatch(t, writeCmd, in, "--config-dir", root)
	if code != exitOK || res.Status != writeStatusWritten || !reflect.DeepEqual(statuses(res), []string{"written"}) {
		t.Fatalf("exit %d %v (%v)", code, statuses(res), res.Message)
	}
	// Gone, and no hidden link is left beside it.
	if got := snapshot(t, root); !reflect.DeepEqual(got, map[string]string{"x.yaml": "tenants:\n  t9: {}\n"}) {
		t.Fatalf("tree %q", got)
	}
	// Again: no file there, unchanged.
	code, res = runBatch(t, writeCmd, in, "--config-dir", root)
	if code != exitOK || res.Status != writeStatusUnchanged || !reflect.DeepEqual(statuses(res), []string{"unchanged"}) {
		t.Fatalf("again: exit %d %v (%v)", code, statuses(res), res.Message)
	}
}

func TestWrite_DeleteRefusals(t *testing.T) {
	cases := []struct {
		name   string
		tree   map[string]string
		setup  func(t *testing.T, root string)
		in     string
		code   int
		reason string
	}{
		// The selected root carrier deleted: the other spelling becomes the
		// root's carrier, and the exporter cannot read it.
		{"deleting the selected carrier", map[string]string{
			"_defaults.yaml": "defaults:\n  cpu: 5\n", "_defaults.yml": "defaults:\n  - 1\n", "t.yaml": "tenants:\n  t1: {}\n"}, nil,
			`{"files":[{"path":"_defaults.yaml","delete":true}]}`, exitFindings, writeReasonWorsensTree},
		{"a symlink", map[string]string{"real.yaml": "tenants:\n  t1: {}\n"},
			func(t *testing.T, root string) {
				if err := os.Symlink("real.yaml", filepath.Join(root, "t.yaml")); err != nil {
					t.Skip(err)
				}
			},
			`{"files":[{"path":"t.yaml","delete":true}]}`, exitFindings, writeReasonSymlink},
		{"a directory", map[string]string{"t.yaml/x.yaml": "tenants:\n  t1: {}\n"}, nil,
			`{"files":[{"path":"t.yaml","delete":true}]}`, exitFindings, writeReasonNotRead},
		{"a file parse-tree cannot read", map[string]string{"t.yaml": "tenants: [\n"}, nil,
			`{"files":[{"path":"t.yaml","delete":true}]}`, exitFindings, writeReasonOriginalUnreadable},
		{"a name no reader reads", map[string]string{"notes.txt": "x\n"}, nil,
			`{"files":[{"path":"notes.txt","delete":true}]}`, exitFindings, writeReasonNotRead},
		{"delete false", map[string]string{"t.yaml": "tenants: {}\n"}, nil,
			`{"files":[{"path":"t.yaml","delete":false}]}`, exitCallerErr, "bad_input"},
		{"delete with ops", map[string]string{"t.yaml": "tenants: {}\n"}, nil,
			`{"files":[{"path":"t.yaml","ops":[],"delete":true}]}`, exitCallerErr, "bad_input"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, tc.tree)
			if tc.setup != nil {
				tc.setup(t, root)
			}
			before := snapshot(t, root)
			code, res := runBatch(t, writeCmd, tc.in, "--config-dir", root)
			got := callReason(res)
			if got == "" && len(res.Files) == 1 {
				got = reasonOf(res.Files[0])
			}
			if code != tc.code || got != tc.reason {
				t.Fatalf("exit %d reason %q (want %d %q) files %v worsened %v", code, got, tc.code, tc.reason,
					statuses(res), worsenedOf(res))
			}
			sameTree(t, before, snapshot(t, root))
		})
	}
}

func TestWrite_ADeletedPathIsAPathOfTheBatch(t *testing.T) {
	root := writeTree(t, map[string]string{"a.yaml": "tenants:\n  t1: {}\n"})
	before := snapshot(t, root)
	for name, in := range map[string]string{
		"deleted twice":             `{"files":[{"path":"a.yaml","delete":true},{"path":"a.yaml","delete":true}]}`,
		"deleted and edited":        `{"files":[{"path":"a.yaml","delete":true},{"path":"a.yaml","ops":[]}]}`,
		"deleted and replaced":      `{"files":[{"path":"a.yaml","delete":true},{"path":"a.yaml","create":{"map":[]},"replace":true}]}`,
		"deleted and a move source": `{"files":[{"path":"a.yaml","delete":true},{"path":"n.yaml","from":"a.yaml"}]}`,
	} {
		t.Run(name, func(t *testing.T) {
			code, res := runBatch(t, writeCmd, in, "--config-dir", root)
			if code != exitCallerErr || callReason(res) != "bad_input" || res.Message == nil ||
				!strings.Contains(*res.Message, "given once") {
				t.Fatalf("exit %d %+v", code, res)
			}
			sameTree(t, before, snapshot(t, root))
		})
	}
}

// TestWrite_AFailedWriteLinksADeleteBack: the edit's rename fails (it goes
// last); the deleted file is linked back at its path — the same file — and
// no hidden link is left.
func TestWrite_AFailedWriteLinksADeleteBack(t *testing.T) {
	root := writeTree(t, map[string]string{"a.yaml": "tenants:\n  t1:\n    cpu: \"5\"\n", "d.yaml": "tenants:\n  t2: {}\n"})
	before := snapshot(t, root)
	dfi, err := os.Stat(filepath.Join(root, "d.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	w := &writer{cmd: writeCmd, rename: func(string, string) error { return errors.New("disk full") }}
	in := `{"files":[{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1","cpu"]}]},{"path":"d.yaml","delete":true}]}`
	code, res := runSeamed(t, w, in, "--config-dir", root)
	if want := []string{"caller_error/write_failed", "not_written"}; code != exitCallerErr ||
		callReason(res) != writeReasonWriteFailed || !reflect.DeepEqual(statuses(res), want) {
		t.Fatalf("exit %d reason %q files %v, want %v", code, callReason(res), statuses(res), want)
	}
	if m := res.Files[1].Message; m == nil || !strings.Contains(*m, "put back") {
		t.Fatalf("delete message %v", m)
	}
	sameTree(t, before, snapshot(t, root))
	if now, err := os.Stat(filepath.Join(root, "d.yaml")); err != nil || !os.SameFile(dfi, now) {
		t.Fatalf("d.yaml is not the file deleted: %v", err)
	}
}

// TestWrite_ADeleteThatCannotBeLinkedBackIsMaybeWritten: the link back
// fails (and so does every rename): the delete is maybe_written, the call
// partial_write, and the file is kept at its hidden name.
func TestWrite_ADeleteThatCannotBeLinkedBackIsMaybeWritten(t *testing.T) {
	root := writeTree(t, map[string]string{"a.yaml": "tenants:\n  t1:\n    cpu: \"5\"\n", "d.yaml": "tenants:\n  t2: {}\n"})
	n := 0
	w := &writer{cmd: writeCmd,
		rename: func(string, string) error { return errors.New("disk full") },
		link: func(o, nn string) error {
			if n++; n == 1 {
				return os.Link(o, nn) // the hidden link
			}
			return errors.New("no more links")
		}}
	in := `{"files":[{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1","cpu"]}]},{"path":"d.yaml","delete":true}]}`
	code, res := runSeamed(t, w, in, "--config-dir", root)
	if want := []string{"caller_error/write_failed", "maybe_written"}; code != exitCallerErr ||
		callReason(res) != writeReasonPartialWrite || !reflect.DeepEqual(statuses(res), want) {
		t.Fatalf("exit %d reason %q files %v, want %v", code, callReason(res), statuses(res), want)
	}
	got := snapshot(t, root)
	if _, there := got["d.yaml"]; there || len(got) != 2 {
		t.Fatalf("tree %q", got)
	}
	for k, v := range got {
		if k != "a.yaml" && (!strings.HasPrefix(k, ".d.yaml.da-guard-deleted-") || v != "tenants:\n  t2: {}\n") {
			t.Fatalf("the file is not kept at its hidden name: %q", got)
		}
	}
	if m := res.Files[1].Message; m == nil || !strings.Contains(*m, "kept at .d.yaml.da-guard-deleted-") {
		t.Fatalf("delete message %v", m)
	}
}

// TestWrite_AMixedBatchOfAllFiveKinds: every kind in one call, judged once.
func TestWrite_AMixedBatchOfAllFiveKinds(t *testing.T) {
	root := writeTree(t, map[string]string{
		"a.yaml": "tenants:\n  t1:\n    cpu: \"5\"\n",
		"m.yaml": "tenants:\n  t2: {}\n",
		"r.yaml": "# old\ntenants:\n  t4: {}\n",
		"d.yaml": "tenants:\n  t5: {}\n",
	})
	in := `{"files":[{"path":"a.yaml","ops":[{"op":"set","path":["tenants","t1","cpu"],"value":{"str":"6"}}]},` +
		`{"path":"c.yaml","create":` + tenantsT3 + `},` +
		`{"path":"r.yaml","create":{"map":[["tenants",{"map":[["t4",{"map":[["mem",{"int":"1"}]]}]]}]]},"replace":true},` +
		`{"path":"n/m.yaml","from":"m.yaml"},` +
		`{"path":"d.yaml","delete":true}]}`
	code, res := runBatch(t, writeCmd, in, "--config-dir", root)
	if code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %v %v (%v)", code, statuses(res), worsenedOf(res), res.Message)
	}
	var kinds []string
	for _, f := range res.Files {
		kinds = append(kinds, f.Kind)
	}
	if !reflect.DeepEqual(kinds, []string{"edit", "create", "replace", "move", "delete"}) {
		t.Fatalf("kinds %v", kinds)
	}
	want := map[string]string{
		"a.yaml":   "tenants:\n  t1:\n    cpu: \"6\"\n",
		"c.yaml":   "tenants:\n  t3:\n    cpu: 5\n",
		"r.yaml":   "tenants:\n  t4:\n    mem: 1\n",
		"n/m.yaml": "tenants:\n  t2: {}\n",
	}
	if got := snapshot(t, root); !reflect.DeepEqual(got, want) {
		t.Fatalf("tree %q, want %q", got, want)
	}
}

// TestWrite_ARenameFallbackThatMovesAnotherFileMovesItBack: with no hard
// links, a delete's file is renamed to its hidden name, and a rename moves
// whatever the path holds by then. Another writer puts a new file there
// between the check and the rename: it is renamed back, nothing is deleted
// (changed_during_write), and the new file is still at the path.
func TestWrite_ARenameFallbackThatMovesAnotherFileMovesItBack(t *testing.T) {
	root := writeTree(t, map[string]string{"d.yaml": "tenants:\n  t2: {}\n", "x.yaml": "tenants:\n  t9: {}\n"})
	abs := filepath.Join(root, "d.yaml")
	swapped := false
	w := &writer{cmd: writeCmd,
		link: func(string, string) error { return errors.New("no hard links here") },
		rename: func(o, n string) error {
			if o == abs && !swapped {
				swapped = true
				// Kept under a name no walk reads, so its inode is not reused.
				if err := os.Rename(abs, filepath.Join(root, "old.txt")); err != nil {
					return err
				}
				if err := os.WriteFile(abs, []byte("tenants:\n  t3: {}\n"), 0o644); err != nil {
					return err
				}
			}
			return os.Rename(o, n)
		}}
	code, res := runSeamed(t, w, `{"files":[{"path":"d.yaml","delete":true}]}`, "--config-dir", root)
	if code != exitFindings || res.Status != writeStatusRefused ||
		!reflect.DeepEqual(statuses(res), []string{"refused/changed_during_write"}) {
		t.Fatalf("exit %d %s %v (%v)", code, res.Status, statuses(res), res.Message)
	}
	want := map[string]string{"d.yaml": "tenants:\n  t3: {}\n", "old.txt": "tenants:\n  t2: {}\n", "x.yaml": "tenants:\n  t9: {}\n"}
	if got := snapshot(t, root); !reflect.DeepEqual(got, want) {
		t.Fatalf("tree %q, want %q", got, want)
	}
}
