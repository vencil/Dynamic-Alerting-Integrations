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
		"--config-dir", filepath.Join(root, "conf.d"), "--federation-key", "", "--account-id-margin", "0"}

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

// #2928: without --account-id-margin the counter goes twice the fleet (at
// least 10000) above the backup's, once — the same restore again changes
// nothing. Flag misuse is a usage error, including --no-backup without
// federation, where it would have nothing to merge.
func TestStateRestoreCmd_MarginAndNoBackup(t *testing.T) {
	root := t.TempDir()
	stateDir := filepath.Join(root, "state")
	confDir := filepath.Join(root, "conf.d")
	for _, d := range []string{stateDir, confDir} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	for _, tn := range []string{"t-a", "t-b"} {
		if err := os.WriteFile(filepath.Join(confDir, tn+".yaml"), []byte("tenants: {}\n"), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	reg := filepath.Join(stateDir, "_account_registry.yaml")
	if err := os.WriteFile(reg, []byte("schema_version: v1\nnext_account_id: 1001\nallocations:\n  t-a: 1000\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	backup := "kind: ConfigMap\ndata:\n  _account_registry.yaml: |\n    schema_version: v1\n    next_account_id: 1001\n    allocations:\n      t-a: 1000\n"
	common := []string{"--state-store", "dir", "--state-dir", stateDir, "--config-dir", confDir, "--federation-key", ""}
	run := func(args ...string) (int, string) {
		var out, errb bytes.Buffer
		code := runStateCmd(append(append([]string{"restore"}, args...), common...), strings.NewReader(backup), &out, &errb)
		return code, out.String() + errb.String()
	}

	for i := 0; i < 2; i++ {
		if code, out := run("--from", "-"); code != 0 {
			t.Fatalf("restore #%d: code %d\n%s", i+1, code, out)
		}
		if b, _ := os.ReadFile(reg); !strings.Contains(string(b), "next_account_id: 11001\n") {
			t.Fatalf("after restore #%d the registry is\n%s\nwant next_account_id 11001 (1001 + the 10000 floor)", i+1, b)
		}
	}

	for name, args := range map[string][]string{
		"neither --from nor --no-backup": {},
		"both":                           {"--from", "-", "--no-backup"},
		"--no-backup with --keys":        {"--no-backup", "--keys", "_groups.yaml"},
		"--no-backup with a margin":      {"--no-backup", "--account-id-margin", "5"},
		"--no-backup without federation": {"--no-backup"},
		"negative margin":                {"--from", "-", "--account-id-margin", "-5"},
	} {
		if code, out := run(args...); code != 2 {
			t.Errorf("%s: code %d, want 2\n%s", name, code, out)
		}
	}
}

// A backup without a registry record gets no margin, so the default margin's
// conf.d count is not needed: a missing conf.d does not fail the restore.
func TestStateRestoreCmd_NoRegistryInBackup_NeedsNoConfDir(t *testing.T) {
	stateDir := filepath.Join(t.TempDir(), "state")
	if err := os.MkdirAll(stateDir, 0o755); err != nil {
		t.Fatal(err)
	}
	backup := "kind: ConfigMap\ndata:\n  _groups.yaml: |\n    groups: {}\n"
	var out, errb bytes.Buffer
	code := runStateCmd([]string{"restore", "--from", "-", "--keys", "_groups.yaml",
		"--state-store", "dir", "--state-dir", stateDir,
		"--config-dir", filepath.Join(t.TempDir(), "absent"), "--federation-key", ""},
		strings.NewReader(backup), &out, &errb)
	if code != 0 {
		t.Fatalf("code %d\nstdout:\n%s\nstderr:\n%s", code, out.String(), errb.String())
	}
}
