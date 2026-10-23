package main

// A call may repair the tree, never worsen it, judged at tree scope
// (write_worsen.go): whatever the after-tree has that the before-tree did
// not — a path that loses its readers, a verdict or problem on any entry, a
// validation error — refuses the whole call, reason worsens_tree. Tenant ids
// are fixture names (CLAUDE.md #9).

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func worsenedOf(res writeResult) []string {
	out := []string{}
	for _, x := range res.Worsened {
		out = append(out, x.Kind+" "+x.Path+" "+x.Tenant)
	}
	return out
}

func TestWrite_ACallThatWorsensTheTreeWritesNothing(t *testing.T) {
	tenant := func(id string) string {
		return `{"map":[["tenants",{"map":[["` + id + `",{"map":[["cpu",{"int":"5"}]]}]]}]]}`
	}
	cases := []struct {
		name  string
		tree  map[string]string
		in    string
		wants []string // each a worsened item's "kind path tenant" prefix
	}{
		{"a carrier the batch deselects",
			map[string]string{"team/_defaults.yml": "defaults:\n  cpu: 5\n", "team/t.yaml": "tenants:\n  t1: {}\n"},
			`{"files":[{"path":"team/_defaults.yaml","create":{"map":[["defaults",{"map":[]}]]}}]}`,
			[]string{"readers_lost team/_defaults.yml"}},
		{"an emit of a tenant another file declares",
			map[string]string{"a.yaml": "tenants:\n  t1: {}\n"},
			`{"files":[{"path":"b.yaml","create":` + tenant("t1") + `}]}`,
			[]string{"duplicate_tenant b.yaml t1"}},
		{"two emits of one tenant",
			map[string]string{"x.yaml": "tenants:\n  t9: {}\n"},
			`{"files":[{"path":"a.yaml","create":` + tenant("t2") + `},{"path":"b.yaml","create":` + tenant("t2") + `}]}`,
			[]string{"duplicate_tenant b.yaml t2"}},
		{"a config-named directory the emit creates",
			map[string]string{"x.yaml": "tenants:\n  t9: {}\n"},
			`{"files":[{"path":"odd.yaml/a.yaml","create":` + tenant("t3") + `}]}`,
			[]string{"verdict odd.yaml", "problem odd.yaml"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, tc.tree)
			before := snapshot(t, root)
			code, res := runBatch(t, writeCmd, tc.in, "--config-dir", root)
			if code != exitFindings || res.Status != writeStatusRefused || callReason(res) != writeReasonWorsensTree {
				t.Fatalf("exit %d status %s reason %q worsened %v files %v", code, res.Status, callReason(res),
					worsenedOf(res), statuses(res))
			}
			got := worsenedOf(res)
			for _, w := range tc.wants {
				found := false
				for _, g := range got {
					found = found || strings.HasPrefix(g, w)
				}
				if !found {
					t.Errorf("worsened %v: want %q", got, w)
				}
			}
			for _, s := range statuses(res) {
				if s != writeStatusNotWritten {
					t.Errorf("files %v: every file not_written", statuses(res))
				}
			}
			sameTree(t, before, snapshot(t, root))
			if _, err := os.Lstat(filepath.Join(root, "odd.yaml")); !os.IsNotExist(err) {
				t.Fatalf("a directory was made: %v", err)
			}
		})
	}
}

// TestEdit_ATenantMovedBetweenTwoFilesPasses: one file of the batch drops a
// tenant, another declares it — no tree in between is ever written.
func TestEdit_ATenantMovedBetweenTwoFilesPasses(t *testing.T) {
	root := writeTree(t, map[string]string{
		"a.yaml": "tenants:\n  t1:\n    cpu: \"5\"\n  t2: {}\n",
		"b.yaml": "tenants:\n  t3: {}\n",
	})
	in := `{"files":[
		{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1"]}]},
		{"path":"b.yaml","ops":[{"op":"set","path":["tenants","t1"],"value":{"map":[["cpu",{"str":"5"}]]}}]}]}`
	code, res := runBatch(t, writeCmd, in, "--config-dir", root)
	if code != exitOK || res.Status != writeStatusWritten || len(res.Worsened) != 0 {
		t.Fatalf("exit %d %s %v %v (%v)", code, res.Status, statuses(res), worsenedOf(res), res.Message)
	}
	got := snapshot(t, root)
	if got["a.yaml"] != "tenants:\n  t2: {}\n" || got["b.yaml"] != "tenants:\n  t3: {}\n  t1:\n    cpu: \"5\"\n" {
		t.Fatalf("tree %v", got)
	}
	// Each alone would not: the second alone declares t1 twice.
	if code, res := runBatch(t, writeCmd, `{"files":[{"path":"b.yaml","ops":[{"op":"set","path":["tenants","t2"],"value":{"map":[]}}]}]}`,
		"--config-dir", root); code != exitFindings || callReason(res) != writeReasonWorsensTree {
		t.Fatalf("a duplicate by edit: exit %d %v", code, worsenedOf(res))
	}
}

// TestWrite_WhatIsAlreadyWrongElsewhereDoesNotBlock: a file the exporter
// cannot serve and a tenant declared twice, both there before the call, do
// not refuse a write that touches neither.
func TestWrite_WhatIsAlreadyWrongElsewhereDoesNotBlock(t *testing.T) {
	root := writeTree(t, map[string]string{
		"bad.yaml":  "tenants:\n  - t9\n",
		"dup1.yaml": "tenants:\n  t8: {}\n",
		"dup2.yaml": "tenants:\n  t8: {}\n",
		"ok.yaml":   "tenants:\n  t1:\n    cpu: \"5\"\n",
	})
	code, res := runBatch(t, writeCmd, `{"files":[{"path":"new.yaml","create":{"map":[["tenants",{"map":[["t2",{"map":[]}]]}]]}}]}`,
		"--config-dir", root)
	if code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("emit: exit %d %s %v %v (%v)", code, res.Status, statuses(res), worsenedOf(res), res.Message)
	}
	code, res = runBatch(t, writeCmd, `{"files":[{"path":"ok.yaml","ops":[{"op":"set","path":["tenants","t1","cpu"],"value":{"str":"6"}}]}]}`,
		"--config-dir", root)
	if code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("edit: exit %d %s %v %v (%v)", code, res.Status, statuses(res), worsenedOf(res), res.Message)
	}
}

// TestWrite_ADuplicateTenantCanBeRepaired: a tenant declared twice does not
// stop the before-tree's validation, so the error already there elsewhere
// keeps its baseline; removing one copy passes even when the copy left has
// an error of its own (it had no baseline: it was not validated before); a
// batch that leaves the duplicate and adds nothing passes; one that adds a
// second duplicate is refused, naming only that one.
func TestWrite_ADuplicateTenantCanBeRepaired(t *testing.T) {
	tree := map[string]string{
		"d1.yaml":  "tenants:\n  t8:\n    cpu: \"abc\"\n",
		"d2.yaml":  "tenants:\n  t8: {}\n  t6: {}\n",
		"bad.yaml": "tenants:\n  t5:\n    cpu: \"abc\"\n",
		"ok.yaml":  "tenants:\n  t1:\n    cpu: \"5\"\n",
	}
	t.Run("removing a copy", func(t *testing.T) {
		root := writeTree(t, tree)
		code, res := runBatch(t, writeCmd, `{"files":[{"path":"d2.yaml","ops":[{"op":"unset","path":["tenants","t8"]}]}]}`,
			"--config-dir", root)
		if code != exitOK || res.Status != writeStatusWritten {
			t.Fatalf("exit %d %s %v %v (%v)", code, res.Status, statuses(res), worsenedOf(res), res.Message)
		}
	})
	t.Run("leaving it, touching another file", func(t *testing.T) {
		root := writeTree(t, tree)
		code, res := runBatch(t, writeCmd, `{"files":[{"path":"ok.yaml","ops":[{"op":"set","path":["tenants","t1","cpu"],"value":{"str":"6"}}]}]}`,
			"--config-dir", root)
		if code != exitOK || res.Status != writeStatusWritten {
			t.Fatalf("exit %d %s %v %v (%v)", code, res.Status, statuses(res), worsenedOf(res), res.Message)
		}
	})
	t.Run("adding a second duplicate", func(t *testing.T) {
		root := writeTree(t, tree)
		before := snapshot(t, root)
		code, res := runBatch(t, writeCmd, `{"files":[{"path":"new.yaml","create":{"map":[["tenants",{"map":[["t1",{"map":[]}]]}]]}}]}`,
			"--config-dir", root)
		if code != exitFindings || callReason(res) != writeReasonWorsensTree ||
			!reflect.DeepEqual(worsenedOf(res), []string{"duplicate_tenant ok.yaml t1"}) { // ok.yaml: second in walk order
			t.Fatalf("exit %d reason %q worsened %v", code, callReason(res), worsenedOf(res))
		}
		sameTree(t, before, snapshot(t, root))
	})
}

// TestWrite_EveryInputFileIsAnsweredOnceTheInputIsRead: a call that stops
// after reading its input (here: --config-dir is no directory) still has
// one result per input file.
func TestWrite_EveryInputFileIsAnsweredOnceTheInputIsRead(t *testing.T) {
	code, res := runBatch(t, writeCmd, `{"files":[{"path":"a.yaml","ops":[]},{"path":"b.yaml","ops":[]}]}`,
		"--config-dir", filepath.Join(t.TempDir(), "missing"))
	if code != exitCallerErr || callReason(res) != writeReasonBadPath ||
		!reflect.DeepEqual(statuses(res), []string{"not_written", "not_written"}) || res.Files[1].Path != "b.yaml" {
		t.Fatalf("exit %d %+v", code, res)
	}
}

// TestEmit_RollBackLeavesAPathAnotherWriterReplaced: the first file is
// linked, another writer replaces it, the second link fails — the roll-back
// does not remove the other writer's file, and says so.
func TestEmit_RollBackLeavesAPathAnotherWriterReplaced(t *testing.T) {
	root := writeTree(t, map[string]string{"x.yaml": "tenants:\n  t9: {}\n"})
	n := 0
	theirs := "tenants:\n  t7: {}\n"
	w := &writer{cmd: writeCmd, link: func(o, nn string) error {
		if n++; n == 2 {
			p := filepath.Join(root, "new", "a.yaml")
			if err := os.Remove(p); err != nil {
				return err
			}
			if err := os.WriteFile(p, []byte(theirs), 0o644); err != nil {
				return err
			}
			return os.ErrExist
		}
		return os.Link(o, nn)
	}}
	code, res := runSeamed(t, w, emitBatch, "--config-dir", root)
	if want := []string{"maybe_written", "refused/changed_during_write", "not_written"}; code != exitCallerErr ||
		callReason(res) != writeReasonPartialWrite || !reflect.DeepEqual(statuses(res), want) {
		t.Fatalf("exit %d reason %q files %v, want %v", code, callReason(res), statuses(res), want)
	}
	if m := res.Files[0].Message; m == nil || !strings.Contains(*m, "did not put there") {
		t.Fatalf("message %v", m)
	}
	if got := snapshot(t, root)["new/a.yaml"]; got != theirs {
		t.Fatalf("the other writer's file: %q", got)
	}
}
