package main

import (
	"bytes"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestWrite_InputIsTakenAsSent: what encoding/json would quietly reshape —
// invalid UTF-8, an unpaired surrogate escape, a member given twice, a
// member name in another case — is a caller error, and nothing is written.
func TestWrite_InputIsTakenAsSent(t *testing.T) {
	root := writeTree(t, map[string]string{"t1.yaml": "tenants:\n  t1:\n    a: \"1\"\n"})
	set := func(key string) string {
		return `{"ops":[{"op":"set","path":["tenants","t1",` + key + `],"value":{"str":"2"}}]}`
	}
	// esc is a JSON \u escape's lead, kept out of the literals below.
	esc := string(rune(92)) + "u"
	cases := map[string]string{
		"invalid UTF-8":                 set("\"b\xff\""),
		"a lone high surrogate":         set(`"b\ud800"`),
		"a lone low surrogate":          set(`"b\udc00x"`),
		"a high surrogate, no low":      set(`"b` + esc + `d800` + esc + `0041"`),
		"a member twice":                `{"ops":[{"op":"set","path":["tenants","t1","a"],"value":{"str":"2"},"value":{"str":"3"}}]}`,
		"a value type twice":            `{"ops":[{"op":"set","path":["tenants","t1","a"],"value":{"str":"2","str":"3"}}]}`,
		"a map key given twice in JSON": `{"ops":[{"op":"set","path":["tenants","t1","b"],"value":{"map":[["x",{"str":"1"}]],"map":[]}}]}`,
		"ops twice":                     `{"ops":[],"ops":[]}`,
		"a case-variant top member":     `{"OPS":[]}`,
		"a case-variant op member":      `{"ops":[{"Op":"unset","path":["tenants","t1","a"]}]}`,
	}
	for name, in := range cases {
		t.Run(name, func(t *testing.T) {
			before := snapshot(t, root)
			code, res := runW(t, writeCmd, in, "--config-dir", root, "--path", "t1.yaml")
			if code != exitCallerErr || reasonOf(res) != "bad_input" {
				t.Fatalf("exit %d %+v (%v)", code, res, res.Message)
			}
			sameTree(t, before, snapshot(t, root))
		})
	}
	// A valid pair is a character, not an error.
	code, res := runW(t, writeCmd, set(`"b`+esc+`d83d`+esc+`de00"`), "--config-dir", root, "--path", "t1.yaml", "--dry-run")
	if code != exitOK || res.Content == nil || !strings.Contains(*res.Content, `"b`+string(rune(92))+`U0001F600": "2"`) {
		t.Fatalf("a surrogate pair: exit %d %+v", code, res)
	}
	code, res = runW(t, writeCmd, `{"CREATE":{"map":[]}}`, "--config-dir", root, "--path", "t2.yaml")
	if code != exitCallerErr || reasonOf(res) != "bad_input" {
		t.Fatalf("emit, a case-variant member: exit %d %+v", code, res)
	}
}

// TestEdit_KeepsFramingAndSkipsLayoutOnlyWrites: CRLF and a BOM come back
// as they were; an edit whose result is the file's own node tree writes
// nothing even when the bytes would differ.
func TestEdit_KeepsFramingAndSkipsLayoutOnlyWrites(t *testing.T) {
	const crlf = "\xef\xbb\xbf# head\r\ntenants:\r\n  t1:\r\n    a: \"1\"\r\n"
	root := writeTree(t, map[string]string{"t1.yaml": crlf, "t2.yaml": "tenants:\n  t2:\n    a:    \"1\"    # wide\n"})
	code, res := runW(t, writeCmd, `{"ops":[{"op":"set","path":["tenants","t1","a"],"value":{"str":"2"}}]}`,
		"--config-dir", root, "--path", "t1.yaml")
	if code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %+v %v", code, res, res.Message)
	}
	if got := snapshot(t, root)["t1.yaml"]; got != strings.Replace(crlf, `a: "1"`, `a: "2"`, 1) {
		t.Fatalf("framing not kept: %q", got)
	}
	before := snapshot(t, root)
	code, res = runW(t, writeCmd, `{"ops":[{"op":"set","path":["tenants","t2","a"],"value":{"str":"1"}}]}`,
		"--config-dir", root, "--path", "t2.yaml")
	if code != exitOK || res.Status != writeStatusUnchanged {
		t.Fatalf("layout-only: exit %d %+v", code, res)
	}
	sameTree(t, before, snapshot(t, root))
}

func TestEdit_UnsetTheOnlyKeyOfABlock(t *testing.T) {
	root := writeTree(t, map[string]string{"t1.yaml": "tenants:\n  t1:\n    a: \"1\"\n    _routing:\n      group_wait: \"30s\"\n"})
	code, res := runW(t, writeCmd, `{"ops":[{"op":"unset","path":["tenants","t1","_routing","group_wait"]}]}`,
		"--config-dir", root, "--path", "t1.yaml")
	if code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %+v %v", code, res, res.Message)
	}
	if got := snapshot(t, root)["t1.yaml"]; got != "tenants:\n  t1:\n    a: \"1\"\n    _routing: {}\n" {
		t.Fatalf("got %q", got)
	}
}

// TestEmit_RemovesTheDirectoriesItCreated: a write that does not complete
// leaves no directory behind.
func TestEmit_RemovesTheDirectoriesItCreated(t *testing.T) {
	root := writeTree(t, map[string]string{"keep/x.yaml": "a: \"1\"\n"})
	var out, errOut bytes.Buffer
	w := &writer{cmd: writeCmd, jsonOut: true, stdout: &out, errOut: &errOut,
		link: func(string, string) error { return fs.ErrExist }}
	if code, res := commitOne(w, "keep/new/deeper/t9.yaml", filepath.Join(root, "keep", "new", "deeper", "t9.yaml"), []byte("a: \"1\"\n"), nil, 0o644, nil); code != exitFindings || reasonOf(res) != writeReasonChangedDuringWrite {
		t.Fatalf("exit %d %+v", code, w.res)
	}
	if _, err := os.Lstat(filepath.Join(root, "keep", "new")); !os.IsNotExist(err) {
		t.Fatalf("created directory left behind: %v", err)
	}
	if got := snapshot(t, root); len(got) != 1 || got["keep/x.yaml"] != "a: \"1\"\n" {
		t.Fatalf("tree %v", got)
	}
}
