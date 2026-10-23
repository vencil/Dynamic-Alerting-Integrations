package main

// `da-guard edit` / `da-guard emit` (ADR-036 §2 "One writer", step 10): per
// op, the file is the original with only the edit — every other node and
// comment byte for byte; every refusal writes nothing and leaves no
// temporary file; emit keeps the caller's order.

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// runW runs edit or emit with stdin and --json, and decodes its one document.
func runW(t *testing.T, cmd, stdin string, args ...string) (int, writeResult) {
	t.Helper()
	var out, errOut bytes.Buffer
	code := runWrite(cmd, append(args, "--json", "--input", "-"), strings.NewReader(stdin), &out, &errOut)
	dec := json.NewDecoder(&out)
	var res writeResult
	if err := dec.Decode(&res); err != nil {
		t.Fatalf("stdout is not one JSON document: %v (stderr %s)", err, errOut.String())
	}
	if dec.More() {
		t.Fatal("stdout holds more than one JSON document")
	}
	if res.Schema != writeSchema || res.Command != cmd {
		t.Fatalf("schema %q command %q", res.Schema, res.Command)
	}
	return code, res
}

// snapshot is every file under root (path → bytes), temporary ones included.
func snapshot(t *testing.T, root string) map[string]string {
	t.Helper()
	out := map[string]string{}
	err := filepath.Walk(root, func(p string, fi os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if fi.Mode().IsRegular() {
			b, err := os.ReadFile(p)
			if err != nil {
				return err
			}
			rel, _ := filepath.Rel(root, p)
			out[filepath.ToSlash(rel)] = string(b)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func sameTree(t *testing.T, before, after map[string]string) {
	t.Helper()
	keys := map[string]bool{}
	for k := range before {
		keys[k] = true
	}
	for k := range after {
		keys[k] = true
	}
	for k := range keys {
		if before[k] != after[k] {
			t.Errorf("%s changed: %q -> %q", k, before[k], after[k])
		}
	}
}

func reasonOf(r writeResult) string {
	if r.Reason == nil {
		return ""
	}
	return *r.Reason
}

// A tenant file in the conf.d convention, so that a re-encode writes every
// unedited line back as it was.
const editBase = `# owned by team-a
tenants:
  t1:
    # connection cap
    mysql_connections: "80" # raised in 2026
    redis_memory: "1"
    _routing:
      group_wait: "30s"
  t2:
    x: 010 # an int as written
    flow: {a: "1", b: [1, 2]}
# end
`

func TestEdit_EachOpChangesOnlyTheEdit(t *testing.T) {
	cases := []struct {
		name, ops, want string
	}{
		{"set", `[{"op":"set","path":["tenants","t1","mysql_connections"],"value":{"str":"90"}}]`,
			strings.Replace(editBase, `mysql_connections: "80"`, `mysql_connections: "90"`, 1)},
		{"set typed", `[{"op":"set","path":["tenants","t1","n"],"value":{"int":"5"}},{"op":"set","path":["tenants","t1","on"],"value":{"bool":true}}]`,
			strings.Replace(editBase, "      group_wait: \"30s\"\n", "      group_wait: \"30s\"\n    \"n\": 5\n    \"on\": true\n", 1)},
		{"unset", `[{"op":"unset","path":["tenants","t1","_routing"]}]`,
			strings.Replace(editBase, "    _routing:\n      group_wait: \"30s\"\n", "", 1)},
		{"ensure-map", `[{"op":"ensure-map","path":["tenants","t3","_routing"]},{"op":"set","path":["tenants","t3","_routing","receiver"],"value":{"map":[["type",{"str":"webhook"}],["url",{"str":"https://a.example/x"}]]}}]`,
			strings.Replace(editBase, "# end\n", "  t3:\n    _routing:\n      receiver:\n        type: \"webhook\"\n        url: \"https://a.example/x\"\n# end\n", 1)},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, map[string]string{"t1.yaml": editBase, "_defaults.yaml": "defaults: {}\n"})
			code, res := runW(t, editCmd, `{"ops":`+tc.ops+`}`, "--config-dir", root, "--path", "t1.yaml")
			if code != exitOK || res.Status != writeStatusWritten {
				t.Fatalf("exit %d, %+v (%v)", code, res, res.Message)
			}
			after := snapshot(t, root)
			if after["t1.yaml"] != tc.want {
				t.Fatalf("not the file with only the edit:\n--- got\n%s--- want\n%s", after["t1.yaml"], tc.want)
			}
			if len(after) != 2 || after["_defaults.yaml"] != "defaults: {}\n" {
				t.Fatalf("other files touched or left behind: %v", after)
			}
		})
	}
}

func TestEdit_DryRunAndUnchangedWriteNothing(t *testing.T) {
	root := writeTree(t, map[string]string{"t1.yaml": editBase})
	before := snapshot(t, root)
	code, res := runW(t, editCmd, `{"ops":[{"op":"set","path":["tenants","t1","redis_memory"],"value":{"str":"2"}}]}`,
		"--config-dir", root, "--path", "t1.yaml", "--dry-run")
	if code != exitOK || res.Status != writeStatusDryRun || res.Content == nil ||
		*res.Content != strings.Replace(editBase, `redis_memory: "1"`, `redis_memory: "2"`, 1) {
		t.Fatalf("dry run: exit %d %+v", code, res)
	}
	code, res = runW(t, editCmd, `{"ops":[{"op":"set","path":["tenants","t1","redis_memory"],"value":{"str":"1"}}]}`,
		"--config-dir", root, "--path", "t1.yaml")
	if code != exitOK || res.Status != writeStatusUnchanged {
		t.Fatalf("same value: exit %d %+v", code, res)
	}
	sameTree(t, before, snapshot(t, root))

	// Without --json the dry run prints exactly the text.
	var out, errOut bytes.Buffer
	in := `{"ops":[{"op":"unset","path":["tenants","t2"]}]}`
	if code := runWrite(editCmd, []string{"--config-dir", root, "--path", "t1.yaml", "--input", "-", "--dry-run"},
		strings.NewReader(in), &out, &errOut); code != exitOK || !strings.HasPrefix(out.String(), "# owned by team-a\n") || strings.Contains(out.String(), "t2:") {
		t.Fatalf("exit %d stdout %q stderr %q", code, out.String(), errOut.String())
	}
	sameTree(t, before, snapshot(t, root))
}

// TestWrite_RefusalsWriteNothing: every refusal is exit 1, names its
// reason, and leaves the tree as it was — no temporary file either.
func TestWrite_RefusalsWriteNothing(t *testing.T) {
	setA := `{"ops":[{"op":"set","path":["tenants","t1","a"],"value":{"str":"2"}}]}`
	cases := []struct {
		name  string
		files map[string]string
		cmd   string
		path  string
		in    string
		want  string
	}{
		{"original does not parse", map[string]string{"t1.yaml": "tenants: [\n"}, editCmd, "t1.yaml", setA, writeReasonOriginalUnreadable},
		{"original holds two documents", map[string]string{"t1.yaml": "tenants:\n  t1:\n    a: \"1\"\n---\nx: 1\n"}, editCmd, "t1.yaml", setA, writeReasonOriginalUnreadable},
		// A verdict on the original is no refusal by itself; an edit that
		// leaves the verdict in place is refused at the read-back.
		{"an edit that leaves the original's verdict in place", map[string]string{"t1.yaml": "tenants:\n  - t1\n"}, editCmd, "t1.yaml",
			`{"ops":[{"op":"set","path":["x"],"value":{"str":"1"}}]}`, writeReasonReadBackMismatch},
		{"an unselected defaults carrier", map[string]string{"_defaults.yaml": "defaults: {}\n", "_defaults.yml": "defaults: {}\n"},
			editCmd, "_defaults.yml", `{"ops":[{"op":"set","path":["x"],"value":{"str":"1"}}]}`, writeReasonNotRead},
		// Judged before any op: the path, not the op, is what is refused.
		{"an unselected defaults carrier, an op that does not apply", map[string]string{"_defaults.yaml": "defaults: {}\n", "_defaults.yml": "defaults: {}\n"},
			editCmd, "_defaults.yml", `{"ops":[{"op":"set","path":["nope","x"],"value":{"str":"1"}}]}`, writeReasonNotRead},
		{"emit to a hidden path", map[string]string{}, emitCmd, ".t9.yaml", `{"content":{"map":[["tenants",{"map":[]}]]}}`, writeReasonNotRead},
		{"emit to a name no reader reads", map[string]string{}, emitCmd, "t9.json", `{"content":{"map":[["tenants",{"map":[]}]]}}`, writeReasonNotRead},
		{"emit under a directory symlink", map[string]string{"real/x.yaml": "a: \"1\"\n"}, emitCmd, "link/t9.yaml",
			`{"content":{"map":[["tenants",{"map":[]}]]}}`, writeReasonNotRead},
		{"a re-encode that changes another tenant", map[string]string{"t1.yaml": "tenants:\n  t1:\n    a: \"1\"\n  t2:\n    k: [1:20]\n"},
			editCmd, "t1.yaml", setA, writeReasonNotRoundTrippable},
		{"an unset a merge key still supplies", map[string]string{"t1.yaml": "b: &b {a: \"1\"}\ntenants:\n  t1:\n    <<: *b\n    a: \"2\"\n"},
			editCmd, "t1.yaml", `{"ops":[{"op":"unset","path":["tenants","t1","a"]}]}`, writeReasonNotRoundTrippable},
		{"read back: a mapping a merge key supplies", map[string]string{"t1.yaml": "b: &b\n  _routing:\n    group_wait: \"1m\"\ntenants:\n  t1:\n    <<: *b\n"},
			editCmd, "t1.yaml", `{"ops":[{"op":"ensure-map","path":["tenants","t1","_routing"]}]}`, writeReasonReadBackMismatch},
		{"read back: a verdict on the written file", map[string]string{"t1.yaml": "tenants:\n  t1:\n    a: \"1\"\n"},
			editCmd, "t1.yaml", `{"ops":[{"op":"set","path":["tenants"],"value":{"seq":[{"str":"t1"}]}}]}`, writeReasonReadBackMismatch},
		{"read back: emit a tenant list", map[string]string{}, emitCmd, "t9.yaml",
			`{"content":{"map":[["tenants",{"seq":[{"str":"t9"}]}]]}}`, writeReasonReadBackMismatch},
		{"set under a scalar", map[string]string{"t1.yaml": "tenants:\n  t1:\n    a: \"1\"\n"}, editCmd, "t1.yaml",
			`{"ops":[{"op":"set","path":["tenants","t1","a","b"],"value":{"str":"2"}}]}`, writeReasonEditNotApplicable},
		{"set under a missing mapping", map[string]string{"t1.yaml": "tenants: {}\n"}, editCmd, "t1.yaml", setA, writeReasonEditNotApplicable},
		// The text in place after the call must read back clean, changed or
		// not: a no-op edit on a file validation names is no `unchanged`.
		{"a no-op edit leaves a verdict in place", map[string]string{"_defaults.yaml": "defaults:\n  cpu: \"abc\"\n  mem: 5\n"},
			editCmd, "_defaults.yaml", `{"ops":[{"op":"set","path":["defaults","mem"],"value":{"int":"5"}}]}`, writeReasonReadBackMismatch},
		{"a no-op unset leaves a verdict in place", map[string]string{"t1.yaml": "tenants:\n  t1:\n    cpu: 5\n    mem:\n      default: [1]\n"},
			editCmd, "t1.yaml", `{"ops":[{"op":"unset","path":["tenants","t1","nope"]}]}`, writeReasonReadBackMismatch},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, tc.files)
			if strings.Contains(tc.path, "link/") {
				if err := os.Symlink(filepath.Join(root, "real"), filepath.Join(root, "link")); err != nil {
					t.Skip(err)
				}
			}
			before := snapshot(t, root)
			code, res := runW(t, tc.cmd, tc.in, "--config-dir", root, "--path", tc.path)
			if code != exitFindings || res.Status != writeStatusRefused || reasonOf(res) != tc.want {
				t.Fatalf("exit %d status %s reason %q (want %s); message %v", code, res.Status, reasonOf(res), tc.want, res.Message)
			}
			sameTree(t, before, snapshot(t, root))
		})
	}
}

// TestWrite_RepairsAFileTheExporterCannotServe: a verdict on the original is
// no refusal by itself (an edit may repair, never worsen): a tenant key whose
// value is a list (unservable) set to a scalar is written, and the written
// file carries no verdict. A syntax-broken original is still refused as
// unreadable (RefusalsWriteNothing).
func TestWrite_RepairsAFileTheExporterCannotServe(t *testing.T) {
	broken := "# kept\ntenants:\n  t1:\n    cpu:\n      default: [1, 2]\n    mem: \"5\"\n"
	root := writeTree(t, map[string]string{"t1.yaml": broken})
	code, res := runW(t, editCmd, `{"ops":[{"op":"set","path":["tenants","t1","cpu"],"value":{"int":"70"}}]}`,
		"--config-dir", root, "--path", "t1.yaml")
	if code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %+v", code, res)
	}
	got, err := os.ReadFile(filepath.Join(root, "t1.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	if want := "# kept\ntenants:\n  t1:\n    cpu: 70\n    mem: \"5\"\n"; string(got) != want {
		t.Fatalf("written %q, want %q", got, want)
	}
	// The repaired file is now one an edit judges like any other: an edit
	// that would break it again is refused at the read-back.
	before := snapshot(t, root)
	code, res = runW(t, editCmd, `{"ops":[{"op":"set","path":["tenants","t1","cpu"],"value":{"map":[["default",{"seq":[{"int":"1"}]}]]}}]}`,
		"--config-dir", root, "--path", "t1.yaml")
	if code != exitFindings || reasonOf(res) != writeReasonReadBackMismatch {
		t.Fatalf("worsening edit: exit %d %+v", code, res)
	}
	sameTree(t, before, snapshot(t, root))
}

// TestWrite_ACommentsOnlyFileIsEditedAsAnEmptyOne: a file holding only
// comments has no document, as a blank file has none: the ops apply to an
// empty mapping and the comments are written back verbatim ahead of it.
func TestWrite_ACommentsOnlyFileIsEditedAsAnEmptyOne(t *testing.T) {
	for _, tc := range []struct{ name, text, want string }{
		{"LF", "# nothing yet\n# second line\n", "# nothing yet\n# second line\ntenants:\n  t1:\n    a: \"1\"\n"},
		{"no final newline", "# c", "# c\ntenants:\n  t1:\n    a: \"1\"\n"},
		{"CRLF", "# c\r\n", "# c\r\ntenants:\r\n  t1:\r\n    a: \"1\"\r\n"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, map[string]string{"t1.yaml": tc.text})
			code, res := runW(t, editCmd, `{"ops":[{"op":"ensure-map","path":["tenants","t1"]},`+
				`{"op":"set","path":["tenants","t1","a"],"value":{"str":"1"}}]}`, "--config-dir", root, "--path", "t1.yaml")
			if code != exitOK || res.Status != writeStatusWritten {
				t.Fatalf("exit %d %+v", code, res)
			}
			got, err := os.ReadFile(filepath.Join(root, "t1.yaml"))
			if err != nil {
				t.Fatal(err)
			}
			if string(got) != tc.want {
				t.Fatalf("written %q, want %q", got, tc.want)
			}
		})
	}
	// An edit that means nothing on it is `unchanged`, the file untouched.
	root := writeTree(t, map[string]string{"t1.yaml": "# only\n"})
	before := snapshot(t, root)
	code, res := runW(t, editCmd, `{"ops":[{"op":"unset","path":["tenants"]}]}`, "--config-dir", root, "--path", "t1.yaml")
	if code != exitOK || res.Status != writeStatusUnchanged {
		t.Fatalf("exit %d %+v", code, res)
	}
	sameTree(t, before, snapshot(t, root))
}

func TestWrite_ReadBackNamesTheValue(t *testing.T) {
	root := writeTree(t, map[string]string{"t1.yaml": "b: &b\n  _routing:\n    group_wait: \"1m\"\ntenants:\n  t1:\n    <<: *b\n"})
	_, res := runW(t, editCmd, `{"ops":[{"op":"ensure-map","path":["tenants","t1","_routing"]}]}`, "--config-dir", root, "--path", "t1.yaml")
	if res.Difference == nil || strings.Join(res.Difference.Path, ".") != "tenants.t1._routing.group_wait" ||
		res.Difference.Want == nil || res.Difference.Got != nil {
		t.Fatalf("difference %+v", res.Difference)
	}
}

func TestWrite_RefusesASymlinkedFile(t *testing.T) {
	root := writeTree(t, map[string]string{"real.yaml": "tenants:\n  t1:\n    a: \"1\"\n"})
	if err := os.Symlink(filepath.Join(root, "real.yaml"), filepath.Join(root, "t1.yaml")); err != nil {
		t.Skip(err)
	}
	before := snapshot(t, root)
	code, res := runW(t, editCmd, `{"ops":[{"op":"set","path":["tenants","t1","a"],"value":{"str":"2"}}]}`, "--config-dir", root, "--path", "t1.yaml")
	if code != exitFindings || reasonOf(res) != writeReasonSymlink {
		t.Fatalf("exit %d %+v", code, res)
	}
	sameTree(t, before, snapshot(t, root))
	if fi, err := os.Lstat(filepath.Join(root, "t1.yaml")); err != nil || fi.Mode()&os.ModeSymlink == 0 {
		t.Fatal("the link was replaced")
	}
}

func TestEmit_KeepsTheCallersOrder(t *testing.T) {
	root := writeTree(t, map[string]string{})
	in := `{"content":{"map":[["tenants",{"map":[["t9",{"map":[
		["zeta",{"str":"1"}],["alpha",{"str":"yes"}],["mid",{"int":"10"}],
		["_custom_alerts",{"seq":[{"map":[["name",{"str":"x"}],["expr",{"str":"up\n== 0\n"}]]}]}]]}]]}],
		["defaults",{"map":[]}]]}}`
	code, res := runW(t, emitCmd, in, "--config-dir", root, "--path", "team/t9.yaml")
	if code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %+v %v", code, res, res.Message)
	}
	got := snapshot(t, root)
	want := "tenants:\n  t9:\n    zeta: \"1\"\n    alpha: \"yes\"\n    mid: 10\n    _custom_alerts:\n" +
		"      - name: \"x\"\n        expr: |\n          up\n          == 0\ndefaults: {}\n"
	if len(got) != 1 || got["team/t9.yaml"] != want {
		t.Fatalf("got %v\nwant\n%s", got, want)
	}
	if fi, err := os.Stat(filepath.Join(root, "team", "t9.yaml")); err != nil || fi.Mode().Perm() != 0o644 {
		t.Fatalf("mode %v %v", fi.Mode(), err)
	}
}

func TestWrite_CallerErrors(t *testing.T) {
	root := writeTree(t, map[string]string{"t1.yaml": "tenants: {}\n"})
	cases := []struct {
		name, cmd, in string
		args          []string
		reason        string
	}{
		{"edit of a missing file", editCmd, `{"ops":[]}`, []string{"--config-dir", root, "--path", "t2.yaml"}, "path_missing"},
		{"emit onto an existing file", emitCmd, `{"content":{"map":[]}}`, []string{"--config-dir", root, "--path", "t1.yaml"}, "path_exists"},
		{"path outside", editCmd, `{"ops":[]}`, []string{"--config-dir", root, "--path", "../x.yaml"}, "bad_path"},
		{"no ops", editCmd, `{}`, []string{"--config-dir", root, "--path", "t1.yaml"}, "bad_input"},
		{"unknown member", editCmd, `{"ops":[],"x":1}`, []string{"--config-dir", root, "--path", "t1.yaml"}, "bad_input"},
		{"an untyped value", editCmd, `{"ops":[{"op":"set","path":["a"],"value":"1"}]}`, []string{"--config-dir", root, "--path", "t1.yaml"}, "bad_input"},
		{"a JSON object for a map", emitCmd, `{"content":{"map":{"a":{"str":"1"}}}}`, []string{"--config-dir", root, "--path", "t2.yaml"}, "bad_input"},
		{"emit of a scalar", emitCmd, `{"content":{"str":"1"}}`, []string{"--config-dir", root, "--path", "t2.yaml"}, "bad_input"},
		{"unknown op", editCmd, `{"ops":[{"op":"rename","path":["a"]}]}`, []string{"--config-dir", root, "--path", "t1.yaml"}, "bad_input"},
		{"the merge key", editCmd, `{"ops":[{"op":"unset","path":["<<"]}]}`, []string{"--config-dir", root, "--path", "t1.yaml"}, "bad_input"},
		{"no config dir", editCmd, `{"ops":[]}`, []string{"--path", "t1.yaml"}, "bad_flags"},
		{"bad flag", editCmd, `{"ops":[]}`, []string{"--nope"}, "bad_flags"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			before := snapshot(t, root)
			code, res := runW(t, tc.cmd, tc.in, tc.args...)
			if code != exitCallerErr || res.Status != writeStatusCallerError || reasonOf(res) != tc.reason {
				t.Fatalf("exit %d %+v (%v)", code, res, res.Message)
			}
			sameTree(t, before, snapshot(t, root))
		})
	}
}

// TestCommit_RefusesAFileChangedMeanwhile: the last check before the rename
// — the file still holds the bytes it was judged on.
func TestCommit_RefusesAFileChangedMeanwhile(t *testing.T) {
	root := writeTree(t, map[string]string{"t1.yaml": "tenants: {}\n"})
	var out, errOut bytes.Buffer
	w := &writer{cmd: editCmd, jsonOut: true, stdout: &out, errOut: &errOut, res: writeResult{Schema: writeSchema, Command: editCmd, Path: "t1.yaml"}}
	if code := w.commit(filepath.Join(root, "t1.yaml"), []byte("tenants:\n  t1: {}\n"), []byte("tenants: {}\n# was\n"), 0o644, nil); code != exitFindings || reasonOf(w.res) != writeReasonChangedDuringWrite {
		t.Fatalf("exit %d %+v", code, w.res)
	}
	got := snapshot(t, root)
	if len(got) != 1 || got["t1.yaml"] != "tenants: {}\n" {
		t.Fatalf("tree %v", got)
	}
	w.res = writeResult{Schema: writeSchema, Command: emitCmd, Path: "t1.yaml"}
	if code := w.commit(filepath.Join(root, "t1.yaml"), []byte("x: \"1\"\n"), nil, 0o644, nil); code != exitFindings || reasonOf(w.res) != writeReasonChangedDuringWrite {
		t.Fatalf("emit over a file created meanwhile: exit %d %+v", code, w.res)
	}
	keys := make([]string, 0)
	for k := range snapshot(t, root) {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	if strings.Join(keys, ",") != "t1.yaml" || snapshot(t, root)["t1.yaml"] != "tenants: {}\n" {
		t.Fatalf("tree %v", keys)
	}
}

func TestWrite_Help(t *testing.T) {
	for _, cmd := range []string{editCmd, emitCmd} {
		var out, errOut bytes.Buffer
		if code := run([]string{cmd, "-h"}, &out, &errOut); code != exitOK || !strings.Contains(errOut.String(), "Exit codes:") {
			t.Fatalf("%s -h: exit %d", cmd, code)
		}
	}
}
