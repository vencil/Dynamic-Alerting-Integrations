package testutil

import (
	"os"
	"path/filepath"
	"testing"
)

// ConfdFileState is one way a conf.d tenant entry can exist on disk (#1680).
// The matrix is shared so every conf.d caller's test — confd's own table, the
// cross-caller parity test, the list handler — builds the SAME filesystem
// shapes rather than each hand-rolling a slightly different "broken file".
type ConfdFileState string

const (
	ConfdGood            ConfdFileState = "control-good"
	ConfdMalformedYAML   ConfdFileState = "malformed-yaml"
	ConfdInvalidConfig   ConfdFileState = "invalid-config" // well-formed YAML, wrong schema shape
	ConfdEmptyFile       ConfdFileState = "empty-file"
	ConfdDanglingSymlink ConfdFileState = "dangling-symlink"
	ConfdSymlinkToDir    ConfdFileState = "symlink-to-dir"
	ConfdRealDir         ConfdFileState = "real-dir-named-yaml"
	ConfdUnreadable      ConfdFileState = "unreadable-permissions"
)

// ConfdFileStates lists every state, in a stable order.
var ConfdFileStates = []ConfdFileState{
	ConfdGood, ConfdMalformedYAML, ConfdInvalidConfig, ConfdEmptyFile,
	ConfdDanglingSymlink, ConfdSymlinkToDir, ConfdRealDir, ConfdUnreadable,
}

// BuildConfdEntry creates `<id>.yaml` in dir in the given state. It returns
// false (having created nothing) when the state cannot be built in this
// process — only ConfdUnreadable, under euid 0, where permission bits do not
// stop a read; callers must skip rather than silently test nothing.
//
// Everything a symlink points at is created OUTSIDE dir (in a fresh
// t.TempDir()), so the fixture adds no conf.d entries beyond `<id>.yaml`.
func BuildConfdEntry(t testing.TB, dir, id string, state ConfdFileState) bool {
	t.Helper()
	path := filepath.Join(dir, id+".yaml")
	switch state {
	case ConfdGood:
		WriteYAML(t, dir, id+".yaml", "tenants:\n  "+id+":\n    cpu: \"70\"\n")
	case ConfdMalformedYAML:
		WriteYAML(t, dir, id+".yaml", "tenants: [this is not: valid yaml\n")
	case ConfdInvalidConfig:
		WriteYAML(t, dir, id+".yaml", "tenants:\n  - "+id+"\n")
	case ConfdEmptyFile:
		WriteYAML(t, dir, id+".yaml", "")
	case ConfdDanglingSymlink:
		mustSymlink(t, filepath.Join(t.TempDir(), "gone.yaml"), path)
	case ConfdSymlinkToDir:
		mustSymlink(t, t.TempDir(), path)
	case ConfdRealDir:
		if err := os.Mkdir(path, 0o755); err != nil {
			t.Fatalf("mkdir %q: %v", path, err)
		}
	case ConfdUnreadable:
		if os.Geteuid() == 0 {
			return false
		}
		WriteYAML(t, dir, id+".yaml", "tenants:\n  "+id+":\n    cpu: \"70\"\n")
		if err := os.Chmod(path, 0o000); err != nil {
			t.Fatalf("chmod %q: %v", path, err)
		}
		// Restore so t.TempDir's RemoveAll is not the thing that fails.
		t.Cleanup(func() { _ = os.Chmod(path, 0o644) })
	default:
		t.Fatalf("unknown ConfdFileState %q", state)
	}
	return true
}

func mustSymlink(t testing.TB, target, link string) {
	t.Helper()
	if err := os.Symlink(target, link); err != nil {
		t.Fatalf("symlink %q -> %q: %v", link, target, err)
	}
}
