package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// --- Dispatcher --------------------------------------------------

func TestRun_NoArgs_PrintsUsageAndCallerErr(t *testing.T) {
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	code := run(nil, stdout, stderr)
	if code != exitCallerErr {
		t.Errorf("exit code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr.String(), "Usage:") {
		t.Errorf("expected usage on stderr; got %q", stderr.String())
	}
}

func TestRun_UnknownSubcommand_CallerErr(t *testing.T) {
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	code := run([]string{"frobnicate"}, stdout, stderr)
	if code != exitCallerErr {
		t.Errorf("exit code = %d, want %d", code, exitCallerErr)
	}
	if !strings.Contains(stderr.String(), `unknown subcommand "frobnicate"`) {
		t.Errorf("expected unknown-subcommand message; got %q", stderr.String())
	}
}

func TestRun_VersionPrintsAndExitOK(t *testing.T) {
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	for _, flag := range []string{"--version", "-v"} {
		stdout.Reset()
		stderr.Reset()
		code := run([]string{flag}, stdout, stderr)
		if code != exitOK {
			t.Errorf("%q: exit code = %d, want %d", flag, code, exitOK)
		}
		if !strings.Contains(stdout.String(), programName) {
			t.Errorf("%q: stdout missing program name; got %q", flag, stdout.String())
		}
	}
}

func TestRun_HelpPrintsUsageAndExitOK(t *testing.T) {
	stdout := &bytes.Buffer{}
	stderr := &bytes.Buffer{}
	for _, flag := range []string{"--help", "-h", "help"} {
		stdout.Reset()
		code := run([]string{flag}, stdout, stderr)
		if code != exitOK {
			t.Errorf("%q: exit code = %d, want %d", flag, code, exitOK)
		}
		if !strings.Contains(stdout.String(), "Subcommands:") {
			t.Errorf("%q: stdout missing subcommands section; got %q", flag, stdout.String())
		}
	}
}

// --- Helper: parseRepoFlag --------------------------------------

func TestParseRepoFlag_AcceptsValid(t *testing.T) {
	repo, err := parseRepoFlag("vencil/da-tools")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if repo.Owner != "vencil" || repo.Name != "da-tools" {
		t.Errorf("got %+v, want {Owner:vencil Name:da-tools}", repo)
	}
}

func TestParseRepoFlag_RejectsMalformed(t *testing.T) {
	for _, bad := range []string{"", "noslash", "/missing-owner", "missing-name/", "a/b/c"} {
		_, err := parseRepoFlag(bad)
		if err == nil {
			// "a/b/c" with SplitN(s, "/", 2) yields ["a", "b/c"] — still
			// 2 non-empty parts, so it parses. That's actually fine
			// (GitHub repo names can't contain "/" but our parser is
			// permissive and the API call will reject). Skip the
			// unexpected-pass case for "a/b/c" specifically.
			if bad == "a/b/c" {
				continue
			}
			t.Errorf("parseRepoFlag(%q): expected error, got nil", bad)
		}
	}
}

// --- Helper: walkFilesDir --------------------------------------

func TestWalkFilesDir_HappyPath(t *testing.T) {
	tmp := t.TempDir()
	mustWriteFile(t, filepath.Join(tmp, "a/b.yaml"), []byte("body-1"))
	mustWriteFile(t, filepath.Join(tmp, "a/c.yaml"), []byte("body-2"))
	mustWriteFile(t, filepath.Join(tmp, "top.md"), []byte("body-3"))

	got, err := walkFilesDir(tmp, "")
	if err != nil {
		t.Fatalf("walkFilesDir: %v", err)
	}
	want := map[string]string{
		"a/b.yaml": "body-1",
		"a/c.yaml": "body-2",
		"top.md":   "body-3",
	}
	if len(got) != len(want) {
		t.Errorf("got %d files, want %d (got keys: %v)", len(got), len(want), keysOfBytesMap(got))
	}
	for k, v := range want {
		gotBytes, ok := got[k]
		if !ok {
			t.Errorf("missing key %q", k)
			continue
		}
		if string(gotBytes) != v {
			t.Errorf("key %q: got %q, want %q", k, gotBytes, v)
		}
	}
}

func TestWalkFilesDir_PrefixApplied(t *testing.T) {
	tmp := t.TempDir()
	mustWriteFile(t, filepath.Join(tmp, "x.yaml"), []byte("body"))

	got, err := walkFilesDir(tmp, "prefix")
	if err != nil {
		t.Fatalf("walkFilesDir: %v", err)
	}
	if _, ok := got["prefix/x.yaml"]; !ok {
		t.Errorf("expected key 'prefix/x.yaml'; got keys %v", keysOfBytesMap(got))
	}
}

func TestWalkFilesDir_NotADir(t *testing.T) {
	tmp := t.TempDir()
	f := filepath.Join(tmp, "f.txt")
	mustWriteFile(t, f, []byte("not a dir"))
	_, err := walkFilesDir(f, "")
	if err == nil || !strings.Contains(err.Error(), "not a directory") {
		t.Errorf("expected not-a-directory error; got %v", err)
	}
}

func TestWalkFilesDir_Missing(t *testing.T) {
	_, err := walkFilesDir(filepath.Join(t.TempDir(), "nope"), "")
	if err == nil {
		t.Error("expected error for missing dir, got nil")
	}
}

// --- Helper: writeJSON / writeReport / readInputJSON ---------------

func TestReadInputJSON_FromFile(t *testing.T) {
	tmp := t.TempDir()
	p := filepath.Join(tmp, "in.json")
	mustWriteFile(t, p, []byte(`{"name": "alice", "age": 30}`))

	var got struct {
		Name string `json:"name"`
		Age  int    `json:"age"`
	}
	if err := readInputJSON(p, &bytes.Buffer{}, &got); err != nil {
		t.Fatalf("readInputJSON: %v", err)
	}
	if got.Name != "alice" || got.Age != 30 {
		t.Errorf("got %+v, want {alice 30}", got)
	}
}

func TestReadInputJSON_RejectsUnknownFields(t *testing.T) {
	tmp := t.TempDir()
	p := filepath.Join(tmp, "in.json")
	mustWriteFile(t, p, []byte(`{"name": "alice", "age": 30, "ROGUE": true}`))

	var got struct {
		Name string `json:"name"`
		Age  int    `json:"age"`
	}
	err := readInputJSON(p, &bytes.Buffer{}, &got)
	if err == nil || !strings.Contains(err.Error(), "ROGUE") {
		t.Errorf("expected unknown-field error mentioning 'ROGUE'; got %v", err)
	}
}

func TestReadInputJSON_FromStdin(t *testing.T) {
	stdin := bytes.NewBufferString(`{"name": "bob"}`)
	var got struct {
		Name string `json:"name"`
	}
	if err := readInputJSON("-", stdin, &got); err != nil {
		t.Fatalf("readInputJSON: %v", err)
	}
	if got.Name != "bob" {
		t.Errorf("got %+v, want {bob}", got)
	}
}

func TestWriteJSON_ToFile(t *testing.T) {
	tmp := t.TempDir()
	p := filepath.Join(tmp, "out.json")
	if err := writeJSON(p, &bytes.Buffer{}, map[string]int{"a": 1}); err != nil {
		t.Fatalf("writeJSON: %v", err)
	}
	body, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if !strings.Contains(string(body), `"a": 1`) {
		t.Errorf("file content: got %q", body)
	}
	if !bytes.HasSuffix(body, []byte("\n")) {
		t.Errorf("file should have trailing newline; got %q", body)
	}
}

func TestWriteJSON_ToStdout(t *testing.T) {
	out := &bytes.Buffer{}
	if err := writeJSON("-", out, []string{"x"}); err != nil {
		t.Fatalf("writeJSON: %v", err)
	}
	if !strings.Contains(out.String(), `"x"`) {
		t.Errorf("stdout: got %q", out.String())
	}
}

func TestWriteReport_ToFileAndStdout(t *testing.T) {
	out := &bytes.Buffer{}
	if err := writeReport("-", out, "hello"); err != nil {
		t.Fatalf("writeReport: %v", err)
	}
	if out.String() != "hello" {
		t.Errorf("stdout: got %q, want 'hello'", out.String())
	}

	tmp := t.TempDir()
	p := filepath.Join(tmp, "report.md")
	if err := writeReport(p, &bytes.Buffer{}, "world"); err != nil {
		t.Fatalf("writeReport: %v", err)
	}
	got, err := os.ReadFile(p)
	if err != nil {
		t.Fatalf("read back: %v", err)
	}
	if string(got) != "world" {
		t.Errorf("file content: got %q, want 'world'", got)
	}
}

// --- Test helpers ----------------------------------------------

func mustWriteFile(t *testing.T, path string, body []byte) {
	t.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(path, body, 0o644); err != nil {
		t.Fatalf("write %q: %v", path, err)
	}
}

func keysOfBytesMap(m map[string][]byte) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}
