package main

// scope_relative_test.go — #2588 (owner decision): a relative --scope is
// relative to --config-dir, never to the working directory. Before, it was
// resolved against the working directory, so `--config-dir conf.d --scope
// conf.d/db` was the working spelling and `--scope db` (what the help said)
// failed with "outside configDir" unless the working directory happened to be
// the root.
//
// ⚠️ NOT t.Parallel: the cases change the working directory (t.Chdir), which
// is process-global.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// scopeTree: <parent>/conf.d with tenant-a at the root and tenant-db under
// db/. Returns parent and the conf.d path.
func scopeTree(t *testing.T) (string, string) {
	t.Helper()
	dir := writeParityTree(t, map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n",
		"a.yaml":         "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
		"db/t.yaml":      "tenants:\n  tenant-db:\n    mysql_connections: 60\n",
	})
	return filepath.Dir(dir), dir
}

func scopeRun(t *testing.T, args ...string) (int, int, string) {
	t.Helper()
	code, stdout, stderr := runOnce(t, append(args, "--format", "json")...)
	if code != exitOK {
		return code, -1, stderr
	}
	var doc struct {
		Report struct {
			Summary struct {
				TotalTenants int `json:"total_tenants"`
			} `json:"summary"`
		} `json:"report"`
	}
	if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
		t.Fatalf("stdout is not JSON: %v\n%s", err, stdout)
	}
	return code, doc.Report.Summary.TotalTenants, stderr
}

// The same relative --scope gives the same answer from any working
// directory: the root's parent, the root itself, and an unrelated directory.
func TestScope_RelativeIsRelativeToConfigDir(t *testing.T) {
	parent, root := scopeTree(t)
	elsewhere := t.TempDir()
	for _, cwd := range []string{parent, root, elsewhere} {
		t.Run(filepath.Base(cwd), func(t *testing.T) {
			t.Chdir(cwd)
			for _, sc := range []struct {
				scope string
				want  int
			}{
				{"db", 1},
				{"db/", 1},
				{"./db", 1},
				{".", 2},
			} {
				code, n, stderr := scopeRun(t, "--config-dir", root, "--scope", sc.scope)
				if code != exitOK || n != sc.want {
					t.Errorf("cwd=%s --scope %q: exit %d total_tenants %d, want 0 and %d; stderr=%q",
						cwd, sc.scope, code, n, sc.want, stderr)
				}
			}
		})
	}
}

// A relative --config-dir and a relative --scope: the scope joins the
// config dir as given.
func TestScope_RelativeConfigDirAndScope(t *testing.T) {
	parent, _ := scopeTree(t)
	t.Chdir(parent)
	if code, n, stderr := scopeRun(t, "--config-dir", "conf.d", "--scope", "db"); code != exitOK || n != 1 {
		t.Errorf("--config-dir conf.d --scope db: exit %d total_tenants %d; stderr=%q", code, n, stderr)
	}
}

// The old spelling (scope relative to the working directory, repeating the
// config-dir prefix) now names <config-dir>/conf.d/db: when that does not
// exist the run is a caller error naming the doubled path, never a run over a
// different tree.
func TestScope_OldCwdRelativeSpellingIsACallerError(t *testing.T) {
	parent, _ := scopeTree(t)
	t.Chdir(parent)
	code, _, stderr := scopeRun(t, "--config-dir", "conf.d", "--scope", "conf.d/db")
	if code != exitCallerErr {
		t.Fatalf("exit = %d, want %d; stderr=%q", code, exitCallerErr, stderr)
	}
	if !strings.Contains(stderr, filepath.Join("conf.d", "conf.d", "db")) {
		t.Errorf("stderr does not name the doubled path: %q", stderr)
	}
}

func TestScope_AbsoluteIsUsedAsIs(t *testing.T) {
	_, root := scopeTree(t)
	t.Chdir(t.TempDir())
	if code, n, stderr := scopeRun(t, "--config-dir", root, "--scope", filepath.Join(root, "db")); code != exitOK || n != 1 {
		t.Errorf("absolute --scope: exit %d total_tenants %d; stderr=%q", code, n, stderr)
	}
}

// Still contained: a relative scope that climbs out of --config-dir is a
// caller error, as before.
func TestScope_RelativeOutsideConfigDirIsACallerError(t *testing.T) {
	parent, root := scopeTree(t)
	if err := os.Mkdir(filepath.Join(parent, "elsewhere"), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Chdir(t.TempDir())
	code, _, stderr := scopeRun(t, "--config-dir", root, "--scope", "../elsewhere")
	if code != exitCallerErr || !strings.Contains(stderr, "outside configDir") {
		t.Errorf("exit = %d, want %d with \"outside configDir\"; stderr=%q", code, exitCallerErr, stderr)
	}
}
