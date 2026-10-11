package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// #2884: `tenant-api state restore` against a dir store, end to end through
// the flags. Settings come from flags here; the pod passes them as TA_* env.
func TestStateRestoreCmd_DirStore(t *testing.T) {
	root := t.TempDir()
	stateDir := filepath.Join(root, "state")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	cur := "schema_version: v1\nnext_account_id: 1002\nallocations:\n  t-a: 1000\n  t-b: 1001\n"
	if err := os.WriteFile(filepath.Join(stateDir, "_account_registry.yaml"), []byte(cur), 0o644); err != nil {
		t.Fatal(err)
	}
	backup := "kind: ConfigMap\ndata:\n  _account_registry.yaml: |\n    schema_version: v1\n    next_account_id: 1001\n    allocations:\n      t-a: 1000\n  _groups.yaml: |\n    groups: {}\n"
	base := []string{"restore", "--from", "-", "--state-store", "dir", "--state-dir", stateDir,
		"--config-dir", filepath.Join(root, "conf.d"), "--federation-key", ""}

	run := func(extra ...string) (int, string, string) {
		var out, errb bytes.Buffer
		code := runStateCmd(append(append([]string{}, base...), extra...), strings.NewReader(backup), &out, &errb)
		return code, out.String(), errb.String()
	}

	code, out, errOut := run("--dry-run", "--keys", "_groups.yaml")
	if code != 0 || !strings.Contains(out, "_groups.yaml: replace") || !strings.Contains(out, "_account_registry.yaml: unchanged") {
		t.Fatalf("dry run: code %d\nstdout:\n%s\nstderr:\n%s", code, out, errOut)
	}
	if _, err := os.Stat(filepath.Join(stateDir, "_groups.yaml")); err == nil {
		t.Error("dry run wrote _groups.yaml")
	}

	if code, out, errOut := run("--keys", "_groups.yaml"); code != 0 {
		t.Fatalf("restore: code %d\nstdout:\n%s\nstderr:\n%s", code, out, errOut)
	}
	if b, _ := os.ReadFile(filepath.Join(stateDir, "_groups.yaml")); string(b) != "groups: {}\n" {
		t.Errorf("_groups.yaml = %q", b)
	}
	if b, _ := os.ReadFile(filepath.Join(stateDir, "_account_registry.yaml")); string(b) != cur {
		t.Errorf("registry changed by restoring an older backup:\n%s", b)
	}

	if code, _, _ := run("--keys", "_account_registry.yaml"); code != 2 {
		t.Errorf("--keys naming the registry: code %d, want 2", code)
	}
	var errb bytes.Buffer
	if code := runStateCmd([]string{"export"}, nil, &bytes.Buffer{}, &errb); code != 2 || !strings.Contains(errb.String(), "usage:") {
		t.Errorf("unknown verb: code %d, stderr %q", code, errb.String())
	}
}
