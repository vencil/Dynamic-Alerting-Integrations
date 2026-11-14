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

// #3235: a `_custom_alerts` entry the exporter rejects is an error finding,
// so a call that writes one more is refused — and a call that only moves an
// entry already rejected (deleting a served one before it shifts its index)
// does not worsen the tree and is written.
func TestWrite_ARejectedCustomAlertCountsNotItsIndex(t *testing.T) {
	const served = `{recipe: threshold, name: a, metric: m1, op: ">", window: 5m, threshold: "80"}`
	const bad = `{recipe: threshold, name: w, metric: m2, op: ">", window: 5x, threshold: "80"}`
	list := func(entries ...string) string {
		return "tenants:\n  t1:\n    _custom_alerts:\n      - " + strings.Join(entries, "\n      - ") + "\n"
	}
	seq := func(names ...string) string {
		items := []string{}
		for _, n := range names {
			window := "5m"
			if n == "w" || n == "w2" {
				window = "5x"
			}
			metric := map[string]string{"a": "m1", "w": "m2", "w2": "m3"}[n]
			items = append(items, `{"map":[["recipe",{"str":"threshold"}],["name",{"str":"`+n+`"}],["metric",{"str":"`+
				metric+`"}],["op",{"str":">"}],["window",{"str":"`+window+`"}],["threshold",{"str":"80"}]]}`)
		}
		return `{"files":[{"path":"t.yaml","ops":[{"op":"set","path":["tenants","t1","_custom_alerts"],"value":{"seq":[` +
			strings.Join(items, ",") + `]}}]}]}`
	}
	t.Run("the rejected entry moves", func(t *testing.T) {
		root := writeTree(t, map[string]string{"_defaults.yaml": "defaults:\n  cpu: 5\n", "t.yaml": list(served, bad)})
		if code, res := runBatch(t, writeCmd, seq("w"), "--config-dir", root); code != exitOK || res.Status != writeStatusWritten {
			t.Fatalf("exit %d %s %v (%v)", code, res.Status, worsenedOf(res), res.Message)
		}
	})
	t.Run("one more rejected entry", func(t *testing.T) {
		root := writeTree(t, map[string]string{"_defaults.yaml": "defaults:\n  cpu: 5\n", "t.yaml": list(served, bad)})
		before := snapshot(t, root)
		code, res := runBatch(t, writeCmd, seq("a", "w", "w2"), "--config-dir", root)
		if code != exitFindings || callReason(res) != writeReasonWorsensTree {
			t.Fatalf("exit %d reason %q %v", code, callReason(res), worsenedOf(res))
		}
		sameTree(t, before, snapshot(t, root))
	})
}

// #3591: a reserved value of a tenant's own entry its reader does not read
// is refused as value_unread (the value to fix, not a key to remove) when
// the write makes it new — and replacing one unread value with another is
// new (the Ratchet's rule: the same unread value only when it reads the
// same). An unread value the write leaves alone does not block.
func TestWrite_AnUnreadValueIsRefusedWhenTheWriteMakesIt(t *testing.T) {
	tree := map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 5\n",
		"ta.yaml":        "tenants:\n  ta:\n    _silent_mode: purple\n    cpu: \"6\"\n",
	}
	set := func(key, value string) string {
		return `{"files":[{"path":"ta.yaml","ops":[{"op":"set","path":["tenants","ta","` + key +
			`"],"value":{"str":"` + value + `"}}]}]}`
	}
	for _, tc := range []struct {
		name, in string
		refused  bool
	}{
		{"another unread value", set("_silent_mode", "orange"), true},
		{"the same value, as the reader reads it", set("_silent_mode", "Purple"), false},
		{"another key, the unread value untouched", set("cpu", "7"), false},
		{"a fix", set("_silent_mode", "warning"), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, tree)
			code, res := runBatch(t, writeCmd, tc.in, "--config-dir", root, "--dry-run")
			if !tc.refused {
				if code != exitOK {
					t.Fatalf("exit %d %v (%v)", code, worsenedOf(res), res.Message)
				}
				return
			}
			if code != exitFindings || callReason(res) != writeReasonWorsensTree {
				t.Fatalf("exit %d reason %q %v", code, callReason(res), worsenedOf(res))
			}
			if !reflect.DeepEqual(worsenedOf(res), []string{"value_unread ta.yaml ta"}) {
				t.Fatalf("worsened %v", worsenedOf(res))
			}
			if m := res.Worsened[0].Message; !strings.Contains(m, "a value its reader does not read") || strings.Contains(m, "no reader reads it") {
				t.Errorf("message %q", m)
			}
		})
	}
}

// #3834: a reserved key of a tenant's own entry written with a schedule its
// readers ignore is refused as schedule_unread (the window to remove, not a
// key nobody reads) when the write makes it new — and moving the window is
// new (compared as written). A schedule the write leaves alone does not
// block.
func TestWrite_AReservedKeysScheduleIsRefusedWhenTheWriteMakesIt(t *testing.T) {
	tree := map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 5\n",
		"ta.yaml": "tenants:\n  ta:\n    cpu: \"6\"\n    _silent_mode:\n      default: disable\n" +
			"      overrides:\n        - window: \"01:00-02:00\"\n          value: all\n",
	}
	schedule := func(window string) string {
		return `{"files":[{"path":"ta.yaml","ops":[{"op":"set","path":["tenants","ta","_silent_mode"],"value":{"map":[` +
			`["default",{"str":"disable"}],["overrides",{"seq":[` +
			`{"map":[["window",{"str":"` + window + `"}],["value",{"str":"all"}]]}]}]]}}]}]}`
	}
	for _, tc := range []struct {
		name, in string
		refused  bool
	}{
		{"the window moved", schedule("03:00-04:00"), true},
		{"the same schedule", schedule("01:00-02:00"), false},
		{"another key, the schedule untouched",
			`{"files":[{"path":"ta.yaml","ops":[{"op":"set","path":["tenants","ta","cpu"],"value":{"str":"7"}}]}]}`, false},
		{"a fix: the value alone",
			`{"files":[{"path":"ta.yaml","ops":[{"op":"set","path":["tenants","ta","_silent_mode"],"value":{"str":"all"}}]}]}`, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, tree)
			code, res := runBatch(t, writeCmd, tc.in, "--config-dir", root, "--dry-run")
			if !tc.refused {
				if code != exitOK {
					t.Fatalf("exit %d %v (%v)", code, worsenedOf(res), res.Message)
				}
				return
			}
			if code != exitFindings || callReason(res) != writeReasonWorsensTree {
				t.Fatalf("exit %d reason %q %v", code, callReason(res), worsenedOf(res))
			}
			if !reflect.DeepEqual(worsenedOf(res), []string{"schedule_unread ta.yaml ta"}) {
				t.Fatalf("worsened %v", worsenedOf(res))
			}
			if m := res.Worsened[0].Message; !strings.Contains(m, "only its `default` is read") || strings.Contains(m, "no reader reads it") {
				t.Errorf("message %q", m)
			}
		})
	}
}

// #3502: a schedule entry /metrics never serves is refused as dead_entry
// (the entry to remove or the windows to reorder, not a key nobody reads)
// when the write makes it new — and changing the dead entry as written is
// new. A dead entry the write leaves alone does not block.
func TestWrite_ADeadEntryIsRefusedWhenTheWriteMakesIt(t *testing.T) {
	tree := map[string]string{
		"_defaults.yaml": "defaults:\n  cpu: 5\n  mem: 5\n",
		"ta.yaml": "tenants:\n  ta:\n    mem: \"6\"\n    cpu:\n      default: \"7\"\n      overrides:\n" +
			"        - window: \"22:00-06:00\"\n          value: \"8\"\n" +
			"        - window: \"01:00-03:00\"\n          value: \"9\"\n",
	}
	schedule := func(dead string) string {
		return `{"files":[{"path":"ta.yaml","ops":[{"op":"set","path":["tenants","ta","cpu"],"value":{"map":[` +
			`["default",{"str":"7"}],["overrides",{"seq":[` +
			`{"map":[["window",{"str":"22:00-06:00"}],["value",{"str":"8"}]]},` +
			`{"map":[["window",{"str":"` + dead + `"}],["value",{"str":"9"}]]}]}]]}}]}]}`
	}
	for _, tc := range []struct {
		name, in string
		refused  bool
	}{
		{"another dead entry", schedule("02:00-04:00"), true},
		{"the same schedule", schedule("01:00-03:00"), false},
		{"another key, the dead entry untouched",
			`{"files":[{"path":"ta.yaml","ops":[{"op":"set","path":["tenants","ta","mem"],"value":{"str":"7"}}]}]}`, false},
		{"a fix: a window partly outside", schedule("05:00-08:00"), false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, tree)
			code, res := runBatch(t, writeCmd, tc.in, "--config-dir", root, "--dry-run")
			if !tc.refused {
				if code != exitOK {
					t.Fatalf("exit %d %v (%v)", code, worsenedOf(res), res.Message)
				}
				return
			}
			if code != exitFindings || callReason(res) != writeReasonWorsensTree {
				t.Fatalf("exit %d reason %q %v (%v)", code, callReason(res), worsenedOf(res), res.Message)
			}
			if !reflect.DeepEqual(worsenedOf(res), []string{"dead_entry ta.yaml ta"}) {
				t.Fatalf("worsened %v", worsenedOf(res))
			}
			w := res.Worsened[0]
			if w.Finding != "window_covered" || !strings.Contains(w.Message, "serves at no minute") ||
				strings.Contains(w.Message, "no reader reads it") {
				t.Errorf("worsened %+v", w)
			}
		})
	}
}
