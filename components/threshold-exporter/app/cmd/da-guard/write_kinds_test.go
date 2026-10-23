package main

// `da-guard write` (#3206): one batch of edits, creates and moves, judged
// once; a move carries a file byte for byte; comments in a created value are
// written as comments and are never content. Tenant ids are fixture names
// (CLAUDE.md #9).

import (
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const moved = "# kept as written\ntenants:\n  t2:\n    x: 010 # an int as written\n    flow: {a: \"1\"}\n"

func TestWrite_AMixedBatchIsAllOrNothing(t *testing.T) {
	tree := map[string]string{
		"a.yaml": "tenants:\n  t1:\n    cpu: \"5\"\n",
		"m.yaml": moved,
	}
	create := `{"path":"c.yaml","create":{"map":[["tenants",{"map":[["t3",{"map":[]}]]}]]}}`
	move := `{"path":"team/m.yaml","from":"m.yaml"}`
	t.Run("one refusal, nothing written", func(t *testing.T) {
		root := writeTree(t, tree)
		before := snapshot(t, root)
		in := `{"files":[` + create + `,` + move + `,{"path":"a.yaml","ops":[{"op":"set","path":["tenants","t1","cpu","x"],"value":{"str":"1"}}]}]}`
		code, res := runBatch(t, writeCmd, in, "--config-dir", root)
		if want := []string{"not_written", "not_written", "refused/edit_not_applicable"}; code != exitFindings ||
			!reflect.DeepEqual(statuses(res), want) {
			t.Fatalf("exit %d %v, want %v", code, statuses(res), want)
		}
		sameTree(t, before, snapshot(t, root))
		if _, err := os.Lstat(filepath.Join(root, "team")); !os.IsNotExist(err) {
			t.Fatalf("a directory was made: %v", err)
		}
	})
	t.Run("every file passes", func(t *testing.T) {
		root := writeTree(t, tree)
		in := `{"files":[` + create + `,` + move + `,{"path":"a.yaml","ops":[{"op":"set","path":["tenants","t1","cpu"],"value":{"str":"6"}}]}]}`
		code, res := runBatch(t, writeCmd, in, "--config-dir", root)
		if code != exitOK || res.Status != writeStatusWritten {
			t.Fatalf("exit %d %v %v (%v)", code, statuses(res), worsenedOf(res), res.Message)
		}
		if k := []string{res.Files[0].Kind, res.Files[1].Kind, res.Files[2].Kind}; !reflect.DeepEqual(k, []string{"create", "move", "edit"}) ||
			res.Files[1].From == nil || *res.Files[1].From != "m.yaml" || res.Files[0].From != nil {
			t.Fatalf("kinds %v from %v", k, res.Files[1].From)
		}
		want := map[string]string{
			"a.yaml":      "tenants:\n  t1:\n    cpu: \"6\"\n",
			"c.yaml":      "tenants:\n  t3: {}\n",
			"team/m.yaml": moved,
		}
		if got := snapshot(t, root); !reflect.DeepEqual(got, want) {
			t.Fatalf("tree %v, want %v", got, want)
		}
	})
}

func TestWrite_AMoveCarriesTheFileByteForByte(t *testing.T) {
	root := writeTree(t, map[string]string{"m.yaml": moved, "x.yaml": "tenants:\n  t9: {}\n"})
	src := filepath.Join(root, "m.yaml")
	if err := os.Chmod(src, 0o640); err != nil {
		t.Fatal(err)
	}
	before, err := os.Stat(src)
	if err != nil {
		t.Fatal(err)
	}
	code, res := runW(t, writeCmd, `{"from":"m.yaml"}`, "--config-dir", root, "--path", "deep/er/m.yaml", "--dry-run")
	if code != exitOK || res.Status != writeStatusDryRun || res.Content == nil || *res.Content != moved {
		t.Fatalf("dry run: exit %d %+v", code, res)
	}
	code, res = runW(t, writeCmd, `{"from":"m.yaml"}`, "--config-dir", root, "--path", "deep/er/m.yaml")
	if code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %+v (%v)", code, res, res.Message)
	}
	got := snapshot(t, root)
	if len(got) != 2 || got["deep/er/m.yaml"] != moved {
		t.Fatalf("tree %v", got)
	}
	after, err := os.Stat(filepath.Join(root, "deep", "er", "m.yaml"))
	if err != nil || !os.SameFile(before, after) || after.Mode().Perm() != 0o640 {
		t.Fatalf("not the same file: %v %v", after, err)
	}
}

func TestWrite_MoveRefusals(t *testing.T) {
	cases := []struct {
		name   string
		tree   map[string]string
		in     string
		code   int
		reason string // the file's, or the call's for worsens_tree
	}{
		{"onto an existing file", map[string]string{"m.yaml": moved, "n.yaml": "tenants: {}\n"},
			`{"files":[{"path":"n.yaml","from":"m.yaml"}]}`, exitCallerErr, "path_exists"},
		{"a missing source", map[string]string{"n.yaml": "tenants: {}\n"},
			`{"files":[{"path":"o.yaml","from":"m.yaml"}]}`, exitCallerErr, "source_missing"},
		{"a source parse-tree cannot read", map[string]string{"m.yaml": "tenants: [\n"},
			`{"files":[{"path":"o.yaml","from":"m.yaml"}]}`, exitFindings, writeReasonOriginalUnreadable},
		{"to a name no reader reads", map[string]string{"m.yaml": moved},
			`{"files":[{"path":"o.txt","from":"m.yaml"}]}`, exitFindings, writeReasonNotRead},
		// The selected root carrier moved away: the other spelling becomes
		// the root's carrier, and the exporter cannot read it.
		{"moving the selected carrier away", map[string]string{
			"_defaults.yaml": "defaults:\n  cpu: 5\n", "_defaults.yml": "defaults:\n  - 1\n", "t.yaml": "tenants:\n  t1: {}\n"},
			`{"files":[{"path":"team/_defaults.yaml","from":"_defaults.yaml"}]}`, exitFindings, writeReasonWorsensTree},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, tc.tree)
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

// TestWrite_AFailedWriteUndoesTheCreatesAndMoves: the edit's rename fails
// (it goes last); the create is removed and the move put back — nothing of
// the call is left.
func TestWrite_AFailedWriteUndoesTheCreatesAndMoves(t *testing.T) {
	root := writeTree(t, map[string]string{"a.yaml": "tenants:\n  t1:\n    cpu: \"5\"\n", "m.yaml": moved})
	before := snapshot(t, root)
	w := &writer{cmd: writeCmd, rename: func(string, string) error { return errors.New("disk full") }}
	in := `{"files":[{"path":"c.yaml","create":{"map":[["tenants",{"map":[["t3",{"map":[]}]]}]]}},` +
		`{"path":"team/m.yaml","from":"m.yaml"},` +
		`{"path":"a.yaml","ops":[{"op":"set","path":["tenants","t1","cpu"],"value":{"str":"6"}}]}]}`
	code, res := runSeamed(t, w, in, "--config-dir", root)
	if want := []string{"not_written", "not_written", "caller_error/write_failed"}; code != exitCallerErr ||
		callReason(res) != writeReasonWriteFailed || !reflect.DeepEqual(statuses(res), want) {
		t.Fatalf("exit %d reason %q files %v, want %v", code, callReason(res), statuses(res), want)
	}
	if m := res.Files[1].Message; m == nil || !strings.Contains(*m, "moved back") {
		t.Fatalf("move message %v", m)
	}
	sameTree(t, before, snapshot(t, root))
	if _, err := os.Lstat(filepath.Join(root, "team")); !os.IsNotExist(err) {
		t.Fatalf("the directory the call made is left: %v", err)
	}
}

func TestWrite_CommentsAreWrittenAsCommentsAndAreNoContent(t *testing.T) {
	root := writeTree(t, map[string]string{})
	in := `{"files":[{"path":"t.yaml","head_comment":"generated by a tool\n\nkeep this header",` +
		`"create":{"map":[["tenants",{"map":[["t1",{"map":[` +
		`["cpu",{"int":"80"},{"head":"⚠ counterexample: not 80%","line":"the platform default"}],` +
		`["_routing",{"map":[["group_wait",{"str":"30s"}]]},{"line":"a block"}]]}]]}]]}}]}`
	code, res := runBatch(t, writeCmd, in, "--config-dir", root)
	if code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %v (%v)", code, statuses(res), res.Message)
	}
	want := "# generated by a tool\n#\n# keep this header\n\ntenants:\n  t1:\n" +
		"    # ⚠ counterexample: not 80%\n    cpu: 80 # the platform default\n" +
		"    _routing: # a block\n      group_wait: \"30s\"\n"
	if got := snapshot(t, root)["t.yaml"]; got != want {
		t.Fatalf("written\n%s\nwant\n%s", got, want)
	}
	// An edit elsewhere in the file keeps them (they are outside it).
	code, one := runW(t, writeCmd, `{"ops":[{"op":"set","path":["tenants","t1","disk"],"value":{"int":"1"}}]}`,
		"--config-dir", root, "--path", "t.yaml")
	if code != exitOK || one.Status != writeStatusWritten {
		t.Fatalf("edit: exit %d %+v", code, one)
	}
	if got := snapshot(t, root)["t.yaml"]; got != want+"    disk: 1\n" {
		t.Fatalf("after the edit:\n%s", got)
	}
}

// TestWrite_ACommentIsNeverYAML: a head comment's lines each become `# …`
// (text that looks like YAML stays a comment); a line comment with a line
// break, a carriage return or another control character is refused.
func TestWrite_ACommentIsNeverYAML(t *testing.T) {
	root := writeTree(t, map[string]string{})
	code, res := runBatch(t, writeCmd, `{"files":[{"path":"a.yaml","create":{"map":[["tenants",{"map":[]},{"head":"x\ntenants:\n  t6: {}"}]]}}]}`,
		"--config-dir", root, "--dry-run")
	if code != exitOK || res.Files[0].Content == nil ||
		*res.Files[0].Content != "# x\n# tenants:\n#   t6: {}\ntenants: {}\n" {
		t.Fatalf("exit %d %+v", code, res)
	}
	for name, in := range map[string]string{
		"a line break in a line comment":  `{"files":[{"path":"a.yaml","create":{"map":[["tenants",{"map":[]},{"line":"x\nt6: {}"}]]}}]}`,
		"a carriage return":               `{"files":[{"path":"a.yaml","create":{"map":[["tenants",{"map":[]},{"head":"x\rt6: {}"}]]}}]}`,
		"a control character":             `{"files":[{"path":"a.yaml","create":{"map":[["tenants",{"map":[]},{"head":"x\u0000"}]]}}]}`,
		"U+2028":                          `{"files":[{"path":"a.yaml","create":{"map":[["tenants",{"map":[]},{"head":"x y"}]]}}]}`,
		"a carriage return, head_comment": `{"files":[{"path":"a.yaml","head_comment":"x\r\ny","create":{"map":[]}}]}`,
		"an unknown comment member":       `{"files":[{"path":"a.yaml","create":{"map":[["tenants",{"map":[]},{"foot":"x"}]]}}]}`,
		"a fourth element":                `{"files":[{"path":"a.yaml","create":{"map":[["tenants",{"map":[]},{},{}]]}}]}`,
	} {
		t.Run(name, func(t *testing.T) {
			if code, res := runBatch(t, writeCmd, in, "--config-dir", root); code != exitCallerErr || callReason(res) != "bad_input" {
				t.Fatalf("exit %d %+v", code, res)
			}
			if got := snapshot(t, root); len(got) != 0 {
				t.Fatalf("written: %v", got)
			}
		})
	}
}
