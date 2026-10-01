package config

// loaddir_unreadable_test.go — #2115: a config-named entry the walk drops
// because its stat or read fails is named in TreeScan.Unreadable /
// LoadReport.Unreadable with a closed-set reason; the drop itself (and the
// rest of the load) is unchanged. A symlink to a directory is not listed.
//
// Seams: none — t.TempDir() trees; the scan logger is a local buffer.

import (
	"bytes"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

const unreadableDefaults = "defaults:\n  mysql_connections: 80\n"

func writeUnreadableTree(t *testing.T) string {
	t.Helper()
	dir := t.TempDir()
	for name, body := range map[string]string{
		"_defaults.yaml": unreadableDefaults,
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
	} {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

func symlinkOrSkip(t *testing.T, target, link string) {
	t.Helper()
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("os.Symlink unavailable here (%v); CI measures this on ubuntu-latest", err)
	}
}

func loadReport(t *testing.T, dir string) (*ThresholdConfig, LoadReport, string) {
	t.Helper()
	var buf bytes.Buffer
	cfg, rep, err := LoadDirReport(dir, log.New(&buf, "", 0))
	if err != nil {
		t.Fatalf("LoadDirReport: %v (log=%q)", err, buf.String())
	}
	return cfg, rep, buf.String()
}

// A dangling symlink with a config name: stat of its target fails. Listed as
// stat_error; the tenant it would hold is absent and the rest still loads.
// Runs as any user, so CI always measures one Unreadable row.
func TestLoadDirReport_DanglingSymlinkIsUnreadable(t *testing.T) {
	t.Parallel()
	dir := writeUnreadableTree(t)
	symlinkOrSkip(t, "missing.yaml", filepath.Join(dir, "tenant-b.yaml"))
	cfg, rep, logged := loadReport(t, dir)
	want := []UnreadableFile{{RelKey: "tenant-b.yaml", Reason: UnreadableStatError}}
	if !reflect.DeepEqual(rep.Unreadable, want) {
		t.Errorf("Unreadable = %v, want %v", rep.Unreadable, want)
	}
	if _, ok := cfg.Tenants["tenant-a"]; !ok {
		t.Errorf("tenant-a missing: the rest of the tree must still load")
	}
	// The WARN the exporter has always logged is still there (unchanged).
	if !strings.Contains(logged, "cannot stat symlink target of") {
		t.Errorf("WARN missing from log: %q", logged)
	}
}

// A file the process may not read: read_error. chmod cannot stop root, so
// this row is skipped there; the dangling-symlink row above always runs.
func TestLoadDirReport_PermissionDeniedIsUnreadable(t *testing.T) {
	t.Parallel()
	if os.Geteuid() == 0 {
		t.Skip("running as root: chmod 000 does not stop a read; " +
			"TestLoadDirReport_DanglingSymlinkIsUnreadable covers Unreadable for every user")
	}
	for name, wantTenant := range map[string]bool{"tenant-a.yaml": false, "_defaults.yaml": true} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			dir := writeUnreadableTree(t)
			p := filepath.Join(dir, name)
			if err := os.Chmod(p, 0); err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { _ = os.Chmod(p, 0o644) })
			cfg, rep, _ := loadReport(t, dir)
			want := []UnreadableFile{{RelKey: name, Reason: UnreadableReadError}}
			if !reflect.DeepEqual(rep.Unreadable, want) {
				t.Errorf("Unreadable = %v, want %v", rep.Unreadable, want)
			}
			if _, ok := cfg.Tenants["tenant-a"]; ok != wantTenant {
				t.Errorf("tenant-a present = %v, want %v", ok, wantTenant)
			}
		})
	}
}

// A config-named symlink to a directory is never followed (a ConfigMap
// volume can carry one); it is not "unreadable", and a real directory named
// like a config file is walked, not listed either.
func TestLoadDirReport_DirectoryLinksAndDirsAreNotUnreadable(t *testing.T) {
	t.Parallel()
	dir := writeUnreadableTree(t)
	if err := os.Mkdir(filepath.Join(dir, "realdir"), 0o755); err != nil {
		t.Fatal(err)
	}
	symlinkOrSkip(t, "realdir", filepath.Join(dir, "x.yaml"))
	if err := os.MkdirAll(filepath.Join(dir, "d.yaml"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "d.yaml", "tenant-c.yaml"),
		[]byte("tenants:\n  tenant-c:\n    mysql_connections: 60\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	cfg, rep, logged := loadReport(t, dir)
	if rep.Unreadable != nil {
		t.Errorf("Unreadable = %v, want nil", rep.Unreadable)
	}
	if _, ok := cfg.Tenants["tenant-c"]; !ok {
		t.Errorf("tenant-c (under the directory d.yaml/) missing")
	}
	// Behaviour unchanged: the WARN for x.yaml is still logged.
	if !strings.Contains(logged, "x.yaml") {
		t.Errorf("x.yaml WARN missing from log: %q", logged)
	}
}

// A real sub-directory the process may not list: every tenant under it is
// lost, so it is walk_error (RelKey = the directory). chmod cannot stop root;
// TestLoadDirReport_UnlistableDeepDirIsWalkError (Linux) runs as any user.
func TestLoadDirReport_UnlistableSubdirIsWalkError(t *testing.T) {
	t.Parallel()
	if os.Geteuid() == 0 {
		t.Skip("running as root: chmod 000 does not stop listing a directory")
	}
	dir := writeUnreadableTree(t)
	sub := filepath.Join(dir, "team")
	if err := os.MkdirAll(sub, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(sub, "tenant-c.yaml"),
		[]byte("tenants:\n  tenant-c:\n    mysql_connections: 60\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(sub, 0); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(sub, 0o755) })
	cfg, rep, _ := loadReport(t, dir)
	want := []UnreadableFile{{RelKey: "team", Reason: UnreadableWalkError}}
	if !reflect.DeepEqual(rep.Unreadable, want) {
		t.Errorf("Unreadable = %v, want %v", rep.Unreadable, want)
	}
	if _, ok := cfg.Tenants["tenant-a"]; !ok {
		t.Errorf("tenant-a missing: the rest of the tree must still load")
	}
}

func TestLoadDirReport_CleanTreeHasNoUnreadable(t *testing.T) {
	t.Parallel()
	_, rep, _ := loadReport(t, writeUnreadableTree(t))
	if rep.Unreadable != nil {
		t.Errorf("Unreadable = %v, want nil", rep.Unreadable)
	}
}
