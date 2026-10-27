package main

// `da-guard write`'s copy entry (#3206 step 10, assemble_config_dir): another
// tree's file, byte for byte, at a path of this one — judged and put in place
// as a create (or, with replace, a replace). Tenant ids are fixture names
// (CLAUDE.md #9).

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
)

// copied holds comments, flow style, a non-canonical int and non-ASCII text:
// a decode and re-encode would change each of them.
const copied = "# owned by team-b — 團隊 B\ntenants:\n  t7:\n    cpu: 010 # as written\n    note: \"警報\"\n    flow: {a: \"1\"}\n"

// sourceFile writes body to a file in a directory of its own (outside any
// --config-dir) and returns its absolute path.
func sourceFile(t *testing.T, name, body string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), name)
	if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func copyEntry(path, src string, replace bool) string {
	p, _ := json.Marshal(path)
	s, _ := json.Marshal(src)
	e := `{"path":` + string(p) + `,"copy":` + string(s)
	if replace {
		e += `,"replace":true`
	}
	return e + `}`
}

func batchOf(entries ...string) string {
	return `{"files":[` + strings.Join(entries, ",") + `]}`
}

func TestWrite_ACopyCarriesTheBytesAsTheyAre(t *testing.T) {
	root := writeTree(t, map[string]string{"x.yaml": "tenants:\n  t9: {}\n"})
	src := sourceFile(t, "b.yaml", copied)
	in := batchOf(copyEntry("team/b.yaml", src, false))

	code, res := runBatch(t, writeCmd, in, "--config-dir", root, "--dry-run")
	if code != exitOK || res.Status != writeStatusDryRun || res.Files[0].Content == nil || *res.Files[0].Content != copied {
		t.Fatalf("dry run: exit %d %+v", code, res)
	}
	if _, err := os.Lstat(filepath.Join(root, "team")); !os.IsNotExist(err) {
		t.Fatalf("a dry run made a directory: %v", err)
	}

	code, res = runBatch(t, writeCmd, in, "--config-dir", root)
	if code != exitOK || res.Status != writeStatusWritten || res.Files[0].Kind != writeKindCopy || res.Files[0].From != nil {
		t.Fatalf("exit %d %v %+v (%v)", code, statuses(res), res.Files, res.Message)
	}
	want := map[string]string{"x.yaml": "tenants:\n  t9: {}\n", "team/b.yaml": copied}
	if got := snapshot(t, root); !reflect.DeepEqual(got, want) {
		t.Fatalf("tree %q, want %q", got, want)
	}
	if b, err := os.ReadFile(src); err != nil || string(b) != copied {
		t.Fatalf("the source changed: %q %v", b, err)
	}
	if runtime.GOOS != "windows" {
		if fi, err := os.Stat(filepath.Join(root, "team", "b.yaml")); err != nil || fi.Mode().Perm() != 0o600 {
			t.Fatalf("mode %v (%v), want 0600 whatever the source's (0644)", fi.Mode().Perm(), err)
		}
	}
}

func TestWrite_ACopysTargetMustNotExistUnlessReplaced(t *testing.T) {
	const old = "# old\ntenants:\n  t7: {}\n"
	src := sourceFile(t, "b.yaml", copied)

	t.Run("no replace: path_exists, nothing written", func(t *testing.T) {
		root := writeTree(t, map[string]string{"b.yaml": old})
		before := snapshot(t, root)
		code, res := runBatch(t, writeCmd, batchOf(copyEntry("b.yaml", src, false)), "--config-dir", root)
		if code != exitCallerErr || !reflect.DeepEqual(statuses(res), []string{"caller_error/path_exists"}) ||
			!strings.Contains(*res.Files[0].Message, `"replace": true`) {
			t.Fatalf("exit %d %v %v", code, statuses(res), res.Files[0].Message)
		}
		sameTree(t, before, snapshot(t, root))
	})
	t.Run("replace: the bytes, the old file's mode", func(t *testing.T) {
		root := writeTree(t, map[string]string{"b.yaml": old})
		abs := filepath.Join(root, "b.yaml")
		if err := os.Chmod(abs, 0o640); err != nil {
			t.Fatal(err)
		}
		code, res := runBatch(t, writeCmd, batchOf(copyEntry("b.yaml", src, true)), "--config-dir", root)
		if code != exitOK || res.Status != writeStatusWritten {
			t.Fatalf("exit %d %v (%v)", code, statuses(res), res.Message)
		}
		if got := snapshot(t, root); got["b.yaml"] != copied {
			t.Fatalf("tree %q", got)
		}
		if runtime.GOOS != "windows" {
			if fi, err := os.Stat(abs); err != nil || fi.Mode().Perm() != 0o640 {
				t.Fatalf("mode %v (%v), want the old file's 0640", fi.Mode().Perm(), err)
			}
		}
	})
	t.Run("replace, the same bytes: unchanged", func(t *testing.T) {
		root := writeTree(t, map[string]string{"b.yaml": copied})
		code, res := runBatch(t, writeCmd, batchOf(copyEntry("b.yaml", src, true)), "--config-dir", root)
		if code != exitOK || res.Status != writeStatusUnchanged || res.Files[0].Status != writeStatusUnchanged {
			t.Fatalf("exit %d %v", code, statuses(res))
		}
	})
	t.Run("replace, no file there: a create", func(t *testing.T) {
		root := writeTree(t, map[string]string{"x.yaml": "tenants:\n  t9: {}\n"})
		code, res := runBatch(t, writeCmd, batchOf(copyEntry("b.yaml", src, true)), "--config-dir", root)
		if code != exitOK || res.Status != writeStatusWritten || snapshot(t, root)["b.yaml"] != copied {
			t.Fatalf("exit %d %v", code, statuses(res))
		}
	})
}

func TestWrite_CopySourceRefusals(t *testing.T) {
	root := writeTree(t, map[string]string{"in.yaml": "tenants:\n  t1: {}\n"})
	before := snapshot(t, root)
	outside := t.TempDir()
	if err := os.Mkdir(filepath.Join(outside, "d.yaml"), 0o755); err != nil {
		t.Fatal(err)
	}
	real := sourceFile(t, "real.yaml", copied)
	link := filepath.Join(outside, "link.yaml")
	linkOK := os.Symlink(real, link) == nil
	rootLink := filepath.Join(outside, "root-link")
	rootLinkOK := os.Symlink(root, rootLink) == nil

	for _, c := range []struct {
		name, src, reason, says string
		call                    bool // the call's own caller error (no file result's)
		skip                    bool
	}{
		{name: "a relative path", src: "b.yaml", reason: writeReasonBadPath, says: "absolute", call: true},
		{name: "not in its clean form", src: outside + "/./b.yaml", reason: writeReasonBadPath, says: "clean form", call: true},
		{name: "missing", src: filepath.Join(outside, "none.yaml"), reason: writeReasonSourceMissing, says: "does not exist"},
		{name: "a directory", src: filepath.Join(outside, "d.yaml"), reason: writeReasonBadSource, says: "no regular file"},
		{name: "a symlink", src: link, reason: writeReasonBadSource, says: "symlink", skip: !linkOK},
		{name: "inside --config-dir", src: filepath.Join(root, "in.yaml"), reason: writeReasonBadPath, says: "inside --config-dir"},
		{name: "inside --config-dir through a link", src: filepath.Join(rootLink, "in.yaml"), reason: writeReasonBadPath,
			says: "inside --config-dir", skip: !rootLinkOK},
	} {
		t.Run(c.name, func(t *testing.T) {
			if c.skip {
				t.Skip("no symlinks here")
			}
			code, res := runBatch(t, writeCmd, batchOf(copyEntry("b.yaml", c.src, false)), "--config-dir", root)
			if code != exitCallerErr {
				t.Fatalf("exit %d %v", code, statuses(res))
			}
			reason, msg := callReason(res), res.Message
			if !c.call {
				reason, msg = reasonOf(res.Files[0]), res.Files[0].Message
				if res.Files[0].Status != writeStatusCallerError {
					t.Fatalf("status %s", res.Files[0].Status)
				}
			}
			if reason != c.reason || msg == nil || !strings.Contains(*msg, c.says) {
				t.Fatalf("reason %q message %v, want %q naming %q", reason, msg, c.reason, c.says)
			}
			if !c.call && !strings.Contains(*msg, c.src) {
				t.Fatalf("the message does not name the source %q: %s", c.src, *msg)
			}
			sameTree(t, before, snapshot(t, root))
		})
	}
	t.Run("replace without copy or create", func(t *testing.T) {
		code, res := runBatch(t, writeCmd, `{"files":[{"path":"b.yaml","replace":true}]}`, "--config-dir", root)
		if code != exitCallerErr || callReason(res) != writeReasonBadInput {
			t.Fatalf("exit %d %+v", code, res)
		}
	})
	t.Run("copy with from", func(t *testing.T) {
		code, res := runBatch(t, writeCmd, `{"files":[{"path":"b.yaml","from":"in.yaml","copy":"`+real+`"}]}`, "--config-dir", root)
		if code != exitCallerErr || callReason(res) != writeReasonBadInput {
			t.Fatalf("exit %d %+v", code, res)
		}
	})
}

// TestWrite_ACopyIsJudgedWithTheBatch: the copy's text is read back in the
// tree with every file of the call in place — one refused copy (a tenant
// file that is no mapping parse-tree reads, a tenant another copy declares)
// and no file of the call is written.
func TestWrite_ACopyIsJudgedWithTheBatch(t *testing.T) {
	good := sourceFile(t, "a.yaml", "tenants:\n  t1: {}\n")
	t.Run("one copy parse-tree cannot read", func(t *testing.T) {
		root := writeTree(t, map[string]string{"x.yaml": "tenants:\n  t9: {}\n"})
		before := snapshot(t, root)
		bad := sourceFile(t, "b.yaml", "tenants:\n  t2: {}\n  t2: {}\n")
		code, res := runBatch(t, writeCmd, batchOf(copyEntry("a.yaml", good, false), copyEntry("b.yaml", bad, false)),
			"--config-dir", root)
		if want := []string{"not_written", "refused/read_back_mismatch"}; code != exitFindings || !reflect.DeepEqual(statuses(res), want) {
			t.Fatalf("exit %d %v, want %v", code, statuses(res), want)
		}
		sameTree(t, before, snapshot(t, root))
	})
	t.Run("two copies declaring one tenant", func(t *testing.T) {
		root := writeTree(t, map[string]string{"x.yaml": "tenants:\n  t9: {}\n"})
		before := snapshot(t, root)
		dup := sourceFile(t, "c.yaml", "tenants:\n  t1: {}\n")
		code, res := runBatch(t, writeCmd, batchOf(copyEntry("a.yaml", good, false), copyEntry("c.yaml", dup, false)),
			"--config-dir", root)
		if code != exitFindings || res.Status != writeStatusRefused || callReason(res) != writeReasonWorsensTree {
			t.Fatalf("exit %d %v %v", code, statuses(res), worsenedOf(res))
		}
		sameTree(t, before, snapshot(t, root))
	})
}

// TestWrite_AFailedWriteRemovesACopyAgain: the edit's rename fails (it goes
// last); the copy, already linked, is removed again as a create is, and so is
// the directory the call made.
func TestWrite_AFailedWriteRemovesACopyAgain(t *testing.T) {
	root := writeTree(t, map[string]string{"a.yaml": "tenants:\n  t1:\n    cpu: \"5\"\n"})
	before := snapshot(t, root)
	src := sourceFile(t, "b.yaml", copied)
	w := &writer{cmd: writeCmd, rename: func(string, string) error { return errors.New("disk full") }}
	in := batchOf(`{"path":"a.yaml","ops":[{"op":"unset","path":["tenants","t1","cpu"]}]}`, copyEntry("team/b.yaml", src, false))
	code, res := runSeamed(t, w, in, "--config-dir", root)
	if want := []string{"caller_error/write_failed", "not_written"}; code != exitCallerErr ||
		callReason(res) != writeReasonWriteFailed || !reflect.DeepEqual(statuses(res), want) {
		t.Fatalf("exit %d reason %q files %v, want %v", code, callReason(res), statuses(res), want)
	}
	sameTree(t, before, snapshot(t, root))
	if _, err := os.Lstat(filepath.Join(root, "team")); !os.IsNotExist(err) {
		t.Fatalf("the directory the call made is left: %v", err)
	}
}
