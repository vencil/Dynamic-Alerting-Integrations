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

// TestWrite_AMoveSourceIsJudgedAsAnEditsOriginal: a source the walk does not
// read — through a symlinked directory (inside the tree or out of it), under
// a hidden one, with a hidden or non-config name — is refused not_read, and
// nothing anywhere is moved or removed.
func TestWrite_AMoveSourceIsJudgedAsAnEditsOriginal(t *testing.T) {
	box := t.TempDir()
	root := filepath.Join(box, "conf.d")
	outside := filepath.Join(box, "outside")
	for p, body := range map[string]string{
		filepath.Join(outside, "secret.yaml"):         "tenants:\n  t7: {}\n",
		filepath.Join(root, "team", "_defaults.yaml"): "defaults:\n  cpu: 5\n",
		filepath.Join(root, "team", "t.yaml"):         "tenants:\n  t1: {}\n",
		filepath.Join(root, ".git", "x.yaml"):         "tenants:\n  t2: {}\n",
		filepath.Join(root, ".hidden.yaml"):           "tenants:\n  t3: {}\n",
		filepath.Join(root, "notes.txt"):              "tenants:\n  t4: {}\n",
	} {
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	if err := os.Symlink("../outside", filepath.Join(root, "evil")); err != nil {
		t.Skip(err)
	}
	if err := os.Symlink("team", filepath.Join(root, "link")); err != nil {
		t.Skip(err)
	}
	before, outBefore := snapshot(t, root), snapshot(t, outside)
	for _, from := range []string{"evil/secret.yaml", "link/_defaults.yaml", ".git/x.yaml", ".hidden.yaml", "notes.txt"} {
		t.Run(from, func(t *testing.T) {
			code, res := runBatch(t, writeCmd, `{"files":[{"path":"stolen.yaml","from":"`+from+`"}]}`, "--config-dir", root)
			if code != exitFindings || !reflect.DeepEqual(statuses(res), []string{"refused/not_read"}) {
				t.Fatalf("exit %d %v (%v)", code, statuses(res), res.Files[0].Message)
			}
			sameTree(t, before, snapshot(t, root))
			sameTree(t, outBefore, snapshot(t, outside))
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
		"a byte order mark":               `{"files":[{"path":"a.yaml","create":{"map":[["tenants",{"map":[]},{"head":"x` + "\\" + `ufeffy"}]]}}]}`, // a JSON escape
		// `{}` / `[]` are flow style after `key:`: a line comment there is
		// no YAML, so it is refused, not moved.
		"a line comment after an empty map": `{"files":[{"path":"a.yaml","create":{"map":[["tenants",{"map":[]},{"line":"l2"}]]}}]}`,
		"a line comment after an empty seq": `{"files":[{"path":"a.yaml","create":{"map":[["x",{"seq":[]},{"line":"l2"}]]}}]}`,
		"a carriage return, foot_comment":   `{"files":[{"path":"a.yaml","foot_comment":"x\ry","create":{"map":[]}}]}`,
		"U+2029, foot_comment":              `{"files":[{"path":"a.yaml","foot_comment":"x y","create":{"map":[]}}]}`,
		"a seq item line comment break":     `{"files":[{"path":"a.yaml","create":{"map":[["x",{"seq":[[{"str":"a"},{"line":"l\nt6: {}"}]]}]]}}]}`,
		"a seq item head comment CR":        `{"files":[{"path":"a.yaml","create":{"map":[["x",{"seq":[[{"str":"a"},{"head":"h\rt6: {}"}]]}]]}}]}`,
		// After a block item yaml.v3 writes the line comment elsewhere.
		"a line comment after a block item": `{"files":[{"path":"a.yaml","create":{"map":[["x",{"seq":[[{"map":[["a",{"int":"1"}]]},{"line":"l"}]]}]]}}]}`,
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

// TestWrite_AFootCommentEndsTheFile: foot_comment is written after the last
// node, after a blank line, at column 0 — after a block, after `{}` and on a
// file that is `{}` alone; a sequence item's head comment above its `- `.
// Neither is content, and an edit elsewhere keeps both.
func TestWrite_AFootCommentEndsTheFile(t *testing.T) {
	for name, tc := range map[string]struct{ create, want string }{
		"after an empty tenant": {
			`{"map":[["tenants",{"map":[["t1",{"map":[]}]]}]]}`,
			"# head\n\ntenants:\n  t1: {}\n\n# declared:\n#\n#   cpu: 80\n",
		},
		"after a block": {
			`{"map":[["defaults",{"map":[["cpu",{"int":"80"}]]}]]}`,
			"# head\n\ndefaults:\n  cpu: 80\n\n# declared:\n#\n#   cpu: 80\n",
		},
		"an empty file": {
			`{"map":[]}`,
			"# head\n\n{}\n\n# declared:\n#\n#   cpu: 80\n",
		},
		"after a sequence, an item with a head comment": {
			`{"map":[["optional_overrides",{"seq":[{"str":"a"},[{"str":"b"},{"head":"⚠ counterexample"}]]}]]}`,
			"# head\n\noptional_overrides:\n  - \"a\"\n  # ⚠ counterexample\n  - \"b\"\n\n# declared:\n#\n#   cpu: 80\n",
		},
	} {
		t.Run(name, func(t *testing.T) {
			root := writeTree(t, map[string]string{})
			in := `{"files":[{"path":"t.yaml","head_comment":"head","foot_comment":"declared:\n\n  cpu: 80","create":` + tc.create + `}]}`
			code, res := runBatch(t, writeCmd, in, "--config-dir", root)
			if code != exitOK || res.Status != writeStatusWritten {
				t.Fatalf("exit %d %v (%v)", code, statuses(res), res.Message)
			}
			if got := snapshot(t, root)["t.yaml"]; got != tc.want {
				t.Fatalf("written\n%s\nwant\n%s", got, tc.want)
			}
		})
	}
	t.Run("an edit keeps them", func(t *testing.T) {
		root := writeTree(t, map[string]string{})
		in := `{"files":[{"path":"t.yaml","foot_comment":"declared","create":{"map":[["tenants",{"map":[["t1",{"map":[` +
			`["xs",{"seq":[{"str":"a"},[{"str":"b"},{"head":"h","line":"l"}]]}]]}]]}]]}}]}`
		if code, res := runBatch(t, writeCmd, in, "--config-dir", root); code != exitOK {
			t.Fatalf("create: exit %d %v (%v)", code, statuses(res), res.Message)
		}
		code, one := runW(t, writeCmd, `{"ops":[{"op":"set","path":["tenants","t1","cpu"],"value":{"int":"1"}}]}`,
			"--config-dir", root, "--path", "t.yaml")
		if code != exitOK || one.Status != writeStatusWritten {
			t.Fatalf("edit: exit %d %+v", code, one)
		}
		want := "tenants:\n  t1:\n    xs:\n      - \"a\"\n      # h\n      - \"b\" # l\n    cpu: 1\n\n# declared\n"
		if got := snapshot(t, root)["t.yaml"]; got != want {
			t.Fatalf("after the edit:\n%s\nwant\n%s", got, want)
		}
	})
	t.Run("foot_comment goes with create only", func(t *testing.T) {
		root := writeTree(t, map[string]string{"a.yaml": "tenants: {}\n"})
		in := `{"files":[{"path":"a.yaml","foot_comment":"x","ops":[]}]}`
		if code, res := runBatch(t, writeCmd, in, "--config-dir", root); code != exitCallerErr || callReason(res) != "bad_input" {
			t.Fatalf("exit %d %+v", code, res)
		}
	})
}
