package main

// effective_overlay_test.go — `da-guard effective --overlay <rel>=<file>`
// (#2906): "these bytes at this tree path", judged by the exporter's own
// walk. The oracle for substitute and create is the same tree with the
// bytes really written there: the two documents must be byte-identical.

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// writeDraft writes content to a file outside every conf.d and returns it.
func writeDraft(t *testing.T, content string) string {
	t.Helper()
	p := filepath.Join(t.TempDir(), "draft.yaml")
	if err := os.WriteFile(p, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return p
}

func copyFiles(files map[string]string) map[string]string {
	out := make(map[string]string, len(files))
	for k, v := range files {
		out[k] = v
	}
	return out
}

var overlayBase = map[string]string{
	"_defaults.yaml":      "defaults:\n  mysql_connections: 70\n",
	"team/tenant-a.yaml":  "tenants:\n  tenant-a:\n    pg_connections: 80\n",
	"tenant-b.yaml":       "tenants:\n  tenant-b:\n    mysql_connections: 90\n",
	"team/sub/other.yaml": "tenants:\n  tenant-c: {}\n",
}

// overlayMatchesDisk: --overlay rel=<draft> on base prints exactly what the
// tree with draft's bytes written at rel prints, and leaves base untouched.
func overlayMatchesDisk(t *testing.T, rel, draft string, substitute bool) string {
	t.Helper()
	dir := writeParityTree(t, overlayBase)
	before, err := os.ReadFile(filepath.Join(dir, "tenant-b.yaml"))
	if err != nil {
		t.Fatal(err)
	}
	code, got, stderr := runOnce(t, effectiveCmd, "--config-dir", dir, "--overlay", rel+"="+writeDraft(t, draft))
	mustOK(t, code, stderr)

	written := copyFiles(overlayBase)
	written[rel] = draft
	code, want, stderr := runOnce(t, effectiveCmd, "--config-dir", writeParityTree(t, written))
	mustOK(t, code, stderr)
	if got != want {
		t.Errorf("--overlay %s differs from the tree with the file written:\n got: %s\nwant: %s", rel, got, want)
	}
	if _, err := os.Stat(filepath.Join(dir, filepath.FromSlash(rel))); !substitute && err == nil {
		t.Errorf("--overlay created %s on disk", rel)
	}
	after, err := os.ReadFile(filepath.Join(dir, "tenant-b.yaml"))
	if err != nil || string(after) != string(before) {
		t.Errorf("--overlay changed a file on disk (err=%v)", err)
	}
	return got
}

func TestEffectiveOverlay_SubstituteListedFile(t *testing.T) {
	t.Parallel()
	got := overlayMatchesDisk(t, "tenant-b.yaml", "tenants:\n  tenant-b:\n    mysql_connections: 95\n", true)
	if !strings.Contains(got, `"mysql_connections": 95`) {
		t.Errorf("the substituted value is not in the document:\n%s", got)
	}
}

func TestEffectiveOverlay_SubstituteDefaultsCarrier(t *testing.T) {
	t.Parallel()
	got := overlayMatchesDisk(t, "_defaults.yaml", "defaults:\n  mysql_connections: 55\n", true)
	if !strings.Contains(got, `"mysql_connections": 55`) {
		t.Errorf("the substituted default is not in the document:\n%s", got)
	}
}

func TestEffectiveOverlay_CreateNewChainLevel(t *testing.T) {
	t.Parallel()
	got := overlayMatchesDisk(t, "team/_defaults.yaml", "defaults:\n  mysql_connections: 60\n", false)
	if !strings.Contains(got, `"team/_defaults.yaml"`) {
		t.Errorf("the created carrier is not on any chain:\n%s", got)
	}
}

// A created tenant file takes its walk-order place: a duplicate it makes is
// named with the files in the order the walk of the written tree names them.
func TestEffectiveOverlay_CreateDuplicateNamedInWalkOrder(t *testing.T) {
	t.Parallel()
	draft := "tenants:\n  tenant-c: {}\n"
	dir := writeParityTree(t, overlayBase)
	code, _, gotErr := runOnce(t, effectiveCmd, "--config-dir", dir, "--overlay", "team/a.yaml="+writeDraft(t, draft))
	written := copyFiles(overlayBase)
	written["team/a.yaml"] = draft
	wdir := writeParityTree(t, written)
	wcode, _, wantErr := runOnce(t, effectiveCmd, "--config-dir", wdir)
	if code != exitCallerErr || wcode != exitCallerErr {
		t.Fatalf("exit = %d / %d (overlay / written), want %d", code, wcode, exitCallerErr)
	}
	if strings.ReplaceAll(gotErr, dir, "") != strings.ReplaceAll(wantErr, wdir, "") {
		t.Errorf("duplicate named differently:\n got: %s\nwant: %s", gotErr, wantErr)
	}
}

// An overlay whose bytes do not decode is the exporter's verdict on those
// bytes at that path: exit 3, named in parse_failed.
func TestEffectiveOverlay_UndecodableContentExitsThree(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, overlayBase)
	code, stdout, stderr := runOnce(t, effectiveCmd, "--config-dir", dir,
		"--overlay", "tenant-b.yaml="+writeDraft(t, "tenants:\n  tenant-b: [1]\n"))
	if code != exitParseFailed {
		t.Fatalf("exit = %d, want %d; stderr=%q", code, exitParseFailed, stderr)
	}
	if !strings.Contains(stdout, `"tenant-b.yaml"`) {
		t.Errorf("parse_failed does not name the overlaid path:\n%s", stdout)
	}
}

func TestEffectiveOverlay_Refusals(t *testing.T) {
	t.Parallel()
	dir := writeParityTree(t, overlayBase)
	if err := os.MkdirAll(filepath.Join(dir, "dir.yaml"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, ".hidden.yaml"), []byte("tenants: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(filepath.Join(dir, "team"), filepath.Join(dir, "linked")); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "notes"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	draft := writeDraft(t, "tenants:\n  tenant-z: {}\n")
	for name, tc := range map[string]struct{ spec, want string }{
		"leaves the tree":     {"../x.yaml=" + draft, "inside --config-dir"},
		"absolute":            {filepath.Join(dir, "x.yaml") + "=" + draft, "relative to --config-dir"},
		"not a config name":   {"x.txt=" + draft, "not a name the walk reads"},
		"hidden name":         {".draft.yaml=" + draft, "not a name the walk reads"},
		"hidden file on disk": {".hidden.yaml=" + draft, "not a name the walk reads"},
		"hidden directory":    {".snap/x.yaml=" + draft, "not a name the walk reads"},
		"a directory":         {"dir.yaml=" + draft, "not a file the walk lists"},
		"the root":            {".=" + draft, "inside --config-dir"},
		"missing parent":      {"nope/_defaults.yaml=" + draft, "not a directory the walk descends"},
		"parent is a symlink": {"linked/_defaults.yaml=" + draft, "not a directory the walk descends"},
		"parent is a file":    {"notes/x.yaml=" + draft, "not a directory the walk descends"},
		"no '='":              {"tenant-b.yaml", "want <rel-path>=<file>"},
		"no file":             {"tenant-b.yaml=", "want <rel-path>=<file>"},
		"file cannot be read": {"tenant-b.yaml=" + filepath.Join(t.TempDir(), "missing.yaml"), "--overlay file"},
		"empty path":          {"=" + draft, "relative to --config-dir"},
	} {
		code, stdout, stderr := runOnce(t, effectiveCmd, "--config-dir", dir, "--overlay", tc.spec)
		if code != exitCallerErr {
			t.Errorf("%s: exit = %d, want %d; stderr=%q", name, code, exitCallerErr, stderr)
		}
		if stdout != "" {
			t.Errorf("%s: stdout = %q, want nothing on exit 2", name, stdout)
		}
		if !strings.Contains(stderr, tc.want) {
			t.Errorf("%s: stderr = %q, want it to say %q", name, stderr, tc.want)
		}
	}
}

// -h with --overlay exits 0: what `_lib_tenant_values` asks to tell a
// da-guard that does not know the flag from a refused tree.
func TestEffectiveOverlay_FlagKnown(t *testing.T) {
	t.Parallel()
	code, _, stderr := runOnce(t, effectiveCmd, "--config-dir", t.TempDir(), "--overlay", "a.yaml=b", "-h")
	if code != exitOK || !strings.Contains(stderr, "--overlay") {
		t.Errorf("-h: exit = %d, stderr = %q", code, stderr)
	}
}
