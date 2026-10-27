package main

// `da-guard write` takes a batch (#3206 step 10c): every file of the
// call is judged in one tree, with every other file of the batch in place;
// one refusal and nothing is written, every refused file named; a write
// that fails part-way removes the files emit created, and names the files
// edit already renamed. Tenant ids are fixture names (CLAUDE.md #9).

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

func statuses(res writeResult) []string {
	out := make([]string, len(res.Files))
	for i, f := range res.Files {
		out[i] = f.Status
		if f.Reason != nil {
			out[i] += "/" + *f.Reason
		}
	}
	return out
}

func TestEdit_OneRefusalWritesNoFileAndNamesEveryRefusal(t *testing.T) {
	root := writeTree(t, map[string]string{
		"a.yaml":   "tenants:\n  t1:\n    cpu: \"5\"\n",
		"b.yaml":   "tenants:\n  t2:\n",
		"real.txt": "tenants:\n  t3:\n    cpu: \"5\"\n",
		"d.yaml":   "tenants:\n  t4:\n    cpu: \"5\"\n",
	})
	if err := os.Symlink("real.txt", filepath.Join(root, "c.yaml")); err != nil {
		t.Skip(err)
	}
	before := snapshot(t, root)
	in := `{"files":[
		{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1","cpu"]}]},
		{"path":"b.yaml","ops":[{"op":"ensure-map","path":["tenants","t2"]}]},
		{"path":"c.yaml","ops":[{"op":"unset","path":["tenants","t3","cpu"]}]},
		{"path":"d.yaml","ops":[{"op":"unset","path":["tenants","t4","nothing"]}]}]}`
	code, res := runBatch(t, writeCmd, in, "--config-dir", root)
	want := []string{"not_written", "refused/edit_not_applicable", "refused/symlink", "not_written"}
	if code != exitFindings || res.Status != writeStatusRefused || callReason(res) != "" ||
		!reflect.DeepEqual(statuses(res), want) {
		t.Fatalf("exit %d status %s reason %q files %v, want %v", code, res.Status, callReason(res), statuses(res), want)
	}
	for i, p := range []string{"a.yaml", "b.yaml", "c.yaml", "d.yaml"} {
		if res.Files[i].Path != p {
			t.Fatalf("files[%d] is %q, want %q: the input's order", i, res.Files[i].Path, p)
		}
	}
	sameTree(t, before, snapshot(t, root))
}

func TestEdit_EveryFilePassesThenEachIsWritten(t *testing.T) {
	root := writeTree(t, map[string]string{
		"a.yaml":      "tenants:\n  t1:\n    cpu: \"5\"\n",
		"b.yaml":      "tenants:\n  t2:\n    cpu: \"6\"\n",
		"team/c.yaml": "# c\ntenants:\n  t3:\n    cpu: \"7\"\n",
	})
	in := `{"files":[
		{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1","cpu"]}]},
		{"path":"b.yaml","ops":[{"op":"unset","path":["tenants","t2","nothing"]}]},
		{"path":"team/c.yaml","ops":[{"op":"set","path":["tenants","t3","cpu"],"value":{"int":"8"}}]}]}`
	code, res := runBatch(t, writeCmd, in, "--config-dir", root)
	if want := []string{"written", "unchanged", "written"}; code != exitOK || res.Status != writeStatusWritten ||
		!reflect.DeepEqual(statuses(res), want) {
		t.Fatalf("exit %d status %s files %v, want %v (%v)", code, res.Status, statuses(res), want, res.Message)
	}
	got := snapshot(t, root)
	want := map[string]string{
		"a.yaml": "tenants:\n  t1: {}\n", "b.yaml": "tenants:\n  t2:\n    cpu: \"6\"\n",
		"team/c.yaml": "# c\ntenants:\n  t3:\n    cpu: 8\n",
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("tree %v, want %v", got, want)
	}
}

// TestEmit_TheBatchIsJudgedAsOneTree: two carrier spellings of one directory
// pass each alone (each would be the directory's only carrier), but in the
// tree with both written the directory's chain reads one of them: the other
// has no reader, and nothing is written.
func TestEmit_TheBatchIsJudgedAsOneTree(t *testing.T) {
	root := writeTree(t, map[string]string{"x.yaml": "tenants:\n  t1: {}\n"})
	body := `{"map":[["defaults",{"map":[["cpu",{"int":"5"}]]}]]}`
	for _, p := range []string{"team/_defaults.yaml", "team/_defaults.yml"} {
		if code, res := runBatch(t, writeCmd, `{"files":[{"path":"`+p+`","create":`+body+`}]}`,
			"--config-dir", root, "--dry-run"); code != exitOK || res.Status != writeStatusDryRun {
			t.Fatalf("%s alone: exit %d %+v", p, code, res)
		}
	}
	before := snapshot(t, root)
	in := `{"files":[{"path":"team/_defaults.yaml","create":` + body + `},` +
		`{"path":"team/_defaults.yml","create":` + body + `}]}`
	code, res := runBatch(t, writeCmd, in, "--config-dir", root)
	if want := []string{"not_written", "refused/not_read"}; code != exitFindings || !reflect.DeepEqual(statuses(res), want) {
		t.Fatalf("exit %d files %v, want %v", code, statuses(res), want)
	}
	// The entry is the tree's (decoded raw: parseTreeEntry has no
	// UnmarshalJSON).
	var out, errOut bytes.Buffer
	runWrite(writeCmd, []string{"--config-dir", root, "--json", "--input", "-"}, strings.NewReader(in), &out, &errOut)
	var raw struct {
		Files []struct {
			Entry map[string]any `json:"entry"`
		} `json:"files"`
	}
	if err := json.Unmarshal(out.Bytes(), &raw); err != nil || len(raw.Files) != 2 ||
		raw.Files[1].Entry["not_read"] != notReadUnselectedCarrier {
		t.Fatalf("entry %v (%v): want the tree's, an unselected carrier", raw, err)
	}
	sameTree(t, before, snapshot(t, root))
	if _, err := os.Lstat(filepath.Join(root, "team")); !os.IsNotExist(err) {
		t.Fatalf("a directory was made: %v", err)
	}
}

// TestWrite_APathNotInItsCleanFormIsACallerError: `path` and `from` are
// taken as sent or refused — never cleaned in silence, so a caller that
// goes on to use its own string (a git index entry) names the file written.
func TestWrite_APathNotInItsCleanFormIsACallerError(t *testing.T) {
	root := writeTree(t, map[string]string{"a.yaml": "tenants:\n  tenant-a: {}\n"})
	before := snapshot(t, root)
	for _, p := range []string{"x//a.yaml", "x/../a.yaml", "x/./a.yaml", "x/a.yaml/", "./x/a.yaml"} {
		for kind, in := range map[string]string{
			"path":   fmt.Sprintf(`{"files":[{"path":%q,"from":"a.yaml"}]}`, p),
			"from":   fmt.Sprintf(`{"files":[{"path":"y/a.yaml","from":%q}]}`, p),
			"create": fmt.Sprintf(`{"files":[{"path":%q,"create":{"map":[]}}]}`, p),
		} {
			t.Run(kind+" "+p, func(t *testing.T) {
				code, res := runBatch(t, writeCmd, in, "--config-dir", root)
				if code != exitCallerErr || res.Status != writeStatusCallerError || callReason(res) != "bad_path" ||
					res.Message == nil || !strings.Contains(*res.Message, fmt.Sprintf("%q", p)) ||
					!strings.Contains(*res.Message, "clean form") {
					t.Fatalf("exit %d %+v %v", code, res, res.Message)
				}
				sameTree(t, before, snapshot(t, root))
			})
		}
	}
}

func TestWrite_APathGivenTwiceIsACallerError(t *testing.T) {
	root := writeTree(t, map[string]string{"a.yaml": "tenants: {}\n"})
	before := snapshot(t, root)
	for name, in := range map[string]string{
		"edit":                            `{"files":[{"path":"a.yaml","ops":[]},{"path":"a.yaml","ops":[]}]}`,
		"create":                          `{"files":[{"path":"n.yaml","create":{"map":[]}},{"path":"n.yaml","create":{"map":[]}}]}`,
		"a move's source as another path": `{"files":[{"path":"n.yaml","from":"a.yaml"},{"path":"a.yaml","ops":[]}]}`,
		"two moves of one source":         `{"files":[{"path":"n.yaml","from":"a.yaml"},{"path":"m.yaml","from":"a.yaml"}]}`,
	} {
		t.Run(name, func(t *testing.T) {
			code, res := runBatch(t, writeCmd, in, "--config-dir", root)
			if code != exitCallerErr || res.Status != writeStatusCallerError || callReason(res) != "bad_input" ||
				len(res.Files) != 0 || res.Message == nil || !strings.Contains(*res.Message, "given once") {
				t.Fatalf("exit %d %+v", code, res)
			}
			sameTree(t, before, snapshot(t, root))
		})
	}
	for name, in := range map[string]string{
		"no files":           `{"files":[]}`,
		"files null":         `{"files":null}`,
		"no files member":    `{}`,
		"a case-variant top": `{"FILES":[]}`,
		"no path":            `{"files":[{"ops":[]}]}`,
		"two kinds in one":   `{"files":[{"path":"a.yaml","ops":[],"create":{"map":[]}}]}`,
		"head_comment alone": `{"files":[{"path":"a.yaml","ops":[],"head_comment":"x"}]}`,
		"no kind":            `{"files":[{"path":"a.yaml"}]}`,
		"the old shape":      `{"ops":[]}`,
	} {
		t.Run(name, func(t *testing.T) {
			if code, res := runBatch(t, writeCmd, in, "--config-dir", root); code != exitCallerErr || callReason(res) != "bad_input" {
				t.Fatalf("exit %d %+v", code, res)
			}
		})
	}
	// --path is gone: the input names every file.
	if code, res := runBatch(t, writeCmd, `{"files":[{"path":"a.yaml","ops":[]}]}`, "--config-dir", root, "--path", "a.yaml"); code != exitCallerErr || callReason(res) != "bad_flags" {
		t.Fatalf("--path: exit %d %+v", code, res)
	}
}

// emitBatch is three new tenant files under a missing directory.
const emitBatch = `{"files":[
	{"path":"new/a.yaml","create":{"map":[["tenants",{"map":[["t1",{"map":[]}]]}]]}},
	{"path":"new/b.yaml","create":{"map":[["tenants",{"map":[["t2",{"map":[]}]]}]]}},
	{"path":"new/c.yaml","create":{"map":[["tenants",{"map":[["t3",{"map":[]}]]}]]}}]}`

// runSeamed runs w (its seams set) on stdin with --json.
func runSeamed(t *testing.T, w *writer, stdin string, args ...string) (int, writeResult) {
	t.Helper()
	var out, errOut bytes.Buffer
	w.stdout, w.errOut, w.jsonOut = &out, &errOut, true
	code := w.run(append(args, "--json", "--input", "-"), strings.NewReader(stdin))
	var res writeResult
	if err := json.Unmarshal(out.Bytes(), &res); err != nil {
		t.Fatalf("stdout %q: %v (stderr %s)", out.String(), err, errOut.String())
	}
	return code, res
}

// TestEmit_AWriteThatStopsPartWayRemovesWhatItCreated: the second link
// fails; the first file, already linked, is removed again, and so is the
// directory the call made — nothing is left.
func TestEmit_AWriteThatStopsPartWayRemovesWhatItCreated(t *testing.T) {
	for _, tc := range []struct {
		name    string
		linkErr error
		rename  func(string, string) error
		want    []string
		status  string
		reason  string
		code    int
	}{
		{"a path created meanwhile", fs.ErrExist, nil,
			[]string{"not_written", "refused/changed_during_write", "not_written"}, writeStatusRefused, "", exitFindings},
		{"a write error", errors.New("no hard links here"), func(string, string) error { return errors.New("disk full") },
			[]string{"not_written", "caller_error/write_failed", "not_written"}, writeStatusCallerError, writeReasonWriteFailed, exitCallerErr},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := writeTree(t, map[string]string{"x.yaml": "tenants:\n  t9: {}\n"})
			before := snapshot(t, root)
			n := 0
			w := &writer{cmd: writeCmd, rename: tc.rename, link: func(o, nn string) error {
				if n++; n == 2 {
					return tc.linkErr
				}
				return os.Link(o, nn)
			}}
			code, res := runSeamed(t, w, emitBatch, "--config-dir", root)
			if code != tc.code || res.Status != tc.status || callReason(res) != tc.reason || !reflect.DeepEqual(statuses(res), tc.want) {
				t.Fatalf("exit %d status %s reason %q files %v, want %d %s %q %v", code, res.Status, callReason(res),
					statuses(res), tc.code, tc.status, tc.reason, tc.want)
			}
			if m := res.Files[0].Message; m == nil || !strings.Contains(*m, "removed again") {
				t.Fatalf("files[0] message %v: want it named as written and removed again", m)
			}
			sameTree(t, before, snapshot(t, root))
			if _, err := os.Lstat(filepath.Join(root, "new")); !os.IsNotExist(err) {
				t.Fatalf("the directory the call made is left: %v", err)
			}
		})
	}
	// A file the roll-back cannot remove is maybe_written, and the call
	// says some files may be in place.
	root := writeTree(t, map[string]string{"x.yaml": "tenants:\n  t9: {}\n"})
	n := 0
	w := &writer{cmd: writeCmd,
		link: func(o, nn string) error {
			if n++; n == 2 {
				return fs.ErrExist
			}
			return os.Link(o, nn)
		},
		remove: func(string) error { return fs.ErrPermission }}
	code, res := runSeamed(t, w, emitBatch, "--config-dir", root)
	if want := []string{"maybe_written", "refused/changed_during_write", "not_written"}; code != exitCallerErr ||
		callReason(res) != writeReasonPartialWrite || !reflect.DeepEqual(statuses(res), want) {
		t.Fatalf("exit %d reason %q files %v, want %v", code, callReason(res), statuses(res), want)
	}
}

// TestEdit_AWriteThatStopsPartWayNamesTheFilesWritten: a rename is not
// undone — the file renamed before the failure is written, the call is
// partial_write, the rest not written.
func TestEdit_AWriteThatStopsPartWayNamesTheFilesWritten(t *testing.T) {
	root := writeTree(t, map[string]string{
		"a.yaml": "tenants:\n  t1:\n    cpu: \"5\"\n",
		"b.yaml": "tenants:\n  t2:\n    cpu: \"6\"\n",
		"c.yaml": "tenants:\n  t3:\n    cpu: \"7\"\n",
	})
	n := 0
	w := &writer{cmd: writeCmd, rename: func(o, nn string) error {
		if n++; n == 2 {
			return errors.New("disk full")
		}
		return os.Rename(o, nn)
	}}
	in := `{"files":[
		{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1","cpu"]}]},
		{"path":"b.yaml","ops":[{"op":"unset","path":["tenants","t2","cpu"]}]},
		{"path":"c.yaml","ops":[{"op":"unset","path":["tenants","t3","cpu"]}]}]}`
	code, res := runSeamed(t, w, in, "--config-dir", root)
	if want := []string{"written", "caller_error/write_failed", "not_written"}; code != exitCallerErr ||
		res.Status != writeStatusCallerError || callReason(res) != writeReasonPartialWrite || !reflect.DeepEqual(statuses(res), want) {
		t.Fatalf("exit %d status %s reason %q files %v, want %v", code, res.Status, callReason(res), statuses(res), want)
	}
	got := snapshot(t, root)
	keys := make([]string, 0, len(got))
	for k := range got {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	if strings.Join(keys, ",") != "a.yaml,b.yaml,c.yaml" || got["a.yaml"] != "tenants:\n  t1: {}\n" ||
		got["b.yaml"] != "tenants:\n  t2:\n    cpu: \"6\"\n" || got["c.yaml"] != "tenants:\n  t3:\n    cpu: \"7\"\n" {
		t.Fatalf("tree %v (temporary files must be gone)", got)
	}
}

// TestEdit_ChangedBeforeAnyRenameWritesNothing: every file is checked
// before the first rename, so a file changed meanwhile refuses the call
// with nothing written — not after the files before it were renamed.
func TestEdit_ChangedBeforeAnyRenameWritesNothing(t *testing.T) {
	root := writeTree(t, map[string]string{
		"a.yaml": "tenants:\n  t1:\n    cpu: \"5\"\n",
		"b.yaml": "tenants:\n  t2:\n    cpu: \"6\"\n",
	})
	var out, errOut bytes.Buffer
	w := &writer{cmd: writeCmd, jsonOut: true, stdout: &out, errOut: &errOut}
	w.res = writeResult{Schema: writeSchema, Command: writeCmd, Files: []writeFileResult{
		{Path: "a.yaml", Warnings: []string{}}, {Path: "b.yaml", Warnings: []string{}}}}
	files := []*writeFile{
		{kind: writeKindEdit, rel: "a.yaml", abs: filepath.Join(root, "a.yaml"), out: []byte("tenants:\n  t1: {}\n"),
			original: []byte("tenants:\n  t1:\n    cpu: \"5\"\n"), perm: 0o644, pending: true, res: &w.res.Files[0]},
		{kind: writeKindEdit, rel: "b.yaml", abs: filepath.Join(root, "b.yaml"), out: []byte("tenants:\n  t2: {}\n"),
			original: []byte("tenants:\n  t2:\n    cpu: \"was\"\n"), perm: 0o644, pending: true, res: &w.res.Files[1]},
	}
	before := snapshot(t, root)
	if code := w.commit(files); code != exitFindings || w.res.Status != writeStatusRefused ||
		!reflect.DeepEqual(statuses(w.res), []string{"not_written", "refused/changed_during_write"}) {
		t.Fatalf("exit %d %s %v", code, w.res.Status, statuses(w.res))
	}
	sameTree(t, before, snapshot(t, root))
}

// TestEdit_SetTheWholeTopLevel: an empty path replaces the top level with
// the map given (what a generated file's writer sends); nothing of the old
// one is kept, ops after it apply on the new one, and the read-back holds
// the result to the content given.
func TestEdit_SetTheWholeTopLevel(t *testing.T) {
	root := writeTree(t, map[string]string{"gen.yaml": "# generated\nold: &a {x: \"1\"}\nkeep: *a\n"})
	body := `{"ops":[{"op":"set","path":[],"value":{"map":[["tenants",{"map":[["t1",{"map":[["cpu",{"int":"5"}]]}]]}]]}},` +
		`{"op":"set","path":["tenants","t1","mem"],"value":{"str":"6"}}]}`
	code, res := runW(t, writeCmd, body, "--config-dir", root, "--path", "gen.yaml", "--dry-run")
	want := "tenants:\n  t1:\n    cpu: 5\n    mem: \"6\"\n"
	if code != exitOK || res.Status != writeStatusDryRun || res.Content == nil || *res.Content != want {
		t.Fatalf("exit %d %+v", code, res)
	}
	if code, res = runW(t, writeCmd, body, "--config-dir", root, "--path", "gen.yaml"); code != exitOK || res.Status != writeStatusWritten {
		t.Fatalf("exit %d %+v", code, res)
	}
	if got := snapshot(t, root)["gen.yaml"]; got != want {
		t.Fatalf("written %q, want %q", got, want)
	}
	// The same content again is unchanged.
	if code, res = runW(t, writeCmd, body, "--config-dir", root, "--path", "gen.yaml"); code != exitOK || res.Status != writeStatusUnchanged {
		t.Fatalf("again: exit %d %+v", code, res)
	}
	// The whole top level is a mapping only; a set below a key the new top
	// level does not have does not apply.
	for _, tc := range []struct{ name, body, status, reason string }{
		{"a scalar", `{"ops":[{"op":"set","path":[],"value":{"str":"x"}}]}`, writeStatusCallerError, "bad_input"},
		{"no path", `{"ops":[{"op":"set","value":{"map":[]}}]}`, writeStatusCallerError, "bad_input"},
		{"unset of the top level", `{"ops":[{"op":"unset","path":[]}]}`, writeStatusCallerError, "bad_input"},
		{"then a set under a key it lost", `{"ops":[{"op":"set","path":[],"value":{"map":[]}},` +
			`{"op":"set","path":["tenants","t1","cpu"],"value":{"int":"1"}}]}`, writeStatusRefused, writeReasonEditNotApplicable},
	} {
		t.Run(tc.name, func(t *testing.T) {
			before := snapshot(t, root)
			if _, res := runW(t, writeCmd, tc.body, "--config-dir", root, "--path", "gen.yaml"); res.Status != tc.status || reasonOf(res) != tc.reason {
				t.Fatalf("%+v (%v)", res, res.Message)
			}
			sameTree(t, before, snapshot(t, root))
		})
	}
}

// TestWrite_SchemaV3Shape: the document's members, and each file's, are
// exactly the documented ones, on a success and on a refusal.
func TestWrite_SchemaV3Shape(t *testing.T) {
	root := writeTree(t, map[string]string{"a.yaml": "tenants:\n  t1: {}\n"})
	top := []string{"command", "da_guard_version", "files", "message", "reason", "schema", "status", "worsened"}
	file := []string{"content", "difference", "entry", "from", "kind", "message", "path", "problem", "reason", "status", "warnings"}
	for name, in := range map[string]string{
		"written": `{"files":[{"path":"b.yaml","create":{"map":[["tenants",{"map":[]}]]}}]}`,
		"refused": `{"files":[{"path":"b.yaml","create":{"map":[]}},{"path":".c.yaml","create":{"map":[]}}]}`,
		"no file": `{"files":[]}`,
	} {
		t.Run(name, func(t *testing.T) {
			var out, errOut bytes.Buffer
			runWrite(writeCmd, []string{"--config-dir", root, "--json", "--input", "-"}, strings.NewReader(in), &out, &errOut)
			var doc map[string]json.RawMessage
			if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
				t.Fatal(err)
			}
			if got := keysOf(doc); !reflect.DeepEqual(got, top) {
				t.Fatalf("members %v, want %v", got, top)
			}
			if string(doc["schema"]) != `"da-guard.write/v3"` {
				t.Fatalf("schema %s", doc["schema"])
			}
			var files []map[string]json.RawMessage
			if err := json.Unmarshal(doc["files"], &files); err != nil {
				t.Fatal(err)
			}
			for _, f := range files {
				if got := keysOf(f); !reflect.DeepEqual(got, file) {
					t.Fatalf("file members %v, want %v", got, file)
				}
				if string(f["warnings"]) == "null" {
					t.Fatal("warnings is null; [] when none")
				}
			}
		})
	}
}
