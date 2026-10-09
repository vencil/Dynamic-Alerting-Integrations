package policy

// watcher_unavailable_test.go — the file-system shapes of hub #2486 Q7-2
// (甲軸) that the single-file corpus (merge_key_corpus_test.go) cannot hold:
// a policy file the watcher can see but not use, with no last good content
// of its own. Measured on main (2026-10-07): in each of these tenant-api
// lets through a write the file means to forbid, because an unusable file
// reads as "no policy". The decision is that direct mode then refuses the
// policy-reading writes (503) — so each case here asserts that CheckWrite
// does not let the forbidden write through, OR that the Manager reports the
// policy unavailable (unavailableReporter, the seam the fix is to add).
// Expected red on main: these pin the shapes before PR-7c fixes them.
//
//	S2  `.yaml` sound, `.yml` broken at startup (the `.yml` forbids)
//	S4  hot reload: a broken file added where there was none
//	S5  hot reload: the file removed, then added back broken
//	S6  `_domain_policy.yaml` is a directory
//	S7  `_domain_policy.yaml` is a dangling symlink at startup
//	S7b hot reload: a sound file replaced by a dangling symlink
//
// PR mode (LoadSnapshot) must refuse S6 / S7: a directory or a dangling
// symlink is not "no file".

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

// unavailableReporter is how the Manager is to say "a policy file is there
// but unusable, and it has no last good content" (hub #2486 Q7-2). Nothing
// implements it on main; the assertion then rests on CheckWrite alone.
type unavailableReporter interface {
	Unavailable() error
}

// brokenForbidding is forbidding(domain, tenant, receiverType) followed by
// a line no YAML reader parses: the file means to forbid, and neither the
// generator nor tenant-api can read it.
func brokenForbidding(domain, tenant, receiverType string) string {
	return forbidding(domain, tenant, receiverType) + "  broken: [unclosed\n"
}

// assertNotLetThrough fails unless the Manager reports the policy
// unavailable or CheckWrite refuses tenant's write of receiverType.
func assertNotLetThrough(t *testing.T, m *Manager, step, tenant, receiverType string) {
	t.Helper()
	if u, ok := any(m).(unavailableReporter); ok {
		if err := u.Unavailable(); err != nil {
			return
		}
	}
	patch := map[string]string{"_routing_receiver_type": receiverType}
	if v := m.CheckWrite(tenant, patch); len(v) == 0 {
		t.Errorf("%s: CheckWrite(%s, %s) lets the write through and the policy is not reported unavailable "+
			"(the file meant to forbid it)", step, tenant, receiverType)
	}
}

func policyPath(dir string, name string) string { return filepath.Join(dir, name) }

func TestWatcherUnavailable_S2_YmlBrokenBesideSoundYamlAtStartup(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("fin", "t1", "slack"))
	testutil.WriteYAML(t, dir, "_domain_policy.yml", brokenForbidding("ops", "t2", "slack"))
	m := NewManager(dir)
	assertNotLetThrough(t, m, "S2 startup", "t2", "slack")
}

func TestWatcherUnavailable_S4_BrokenFileAddedOnHotReload(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	m := NewManager(dir)
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", brokenForbidding("fin", "t1", "slack"))
	_ = m.Reload() // a failure is expected; what is served after it is the question
	assertNotLetThrough(t, m, "S4 broken file added", "t1", "slack")
}

func TestWatcherUnavailable_S5_RemovedThenAddedBackBroken(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("fin", "t1", "slack"))
	m := NewManager(dir)
	assertNotLetThrough(t, m, "S5 sound file", "t1", "slack") // control: enforced
	if err := os.Remove(policyPath(dir, "_domain_policy.yaml")); err != nil {
		t.Fatal(err)
	}
	if err := m.Reload(); err != nil {
		t.Fatalf("Reload after remove: %v", err)
	}
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", brokenForbidding("fin", "t1", "slack"))
	_ = m.Reload()
	assertNotLetThrough(t, m, "S5 added back broken", "t1", "slack")
}

func TestWatcherUnavailable_S6_PolicyPathIsADirectory(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	if err := os.Mkdir(policyPath(dir, "_domain_policy.yaml"), 0o755); err != nil {
		t.Fatal(err)
	}
	m := NewManager(dir)
	assertNotLetThrough(t, m, "S6 directory at startup", "t1", "slack")
	if _, err := LoadSnapshot(dir); err == nil {
		t.Error("S6: LoadSnapshot (PR mode) reads a directory as no policy file")
	}
}

func TestWatcherUnavailable_S7_DanglingSymlinkAtStartup(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	if err := os.Symlink(policyPath(dir, "gone.yaml"), policyPath(dir, "_domain_policy.yaml")); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	m := NewManager(dir)
	assertNotLetThrough(t, m, "S7 dangling symlink at startup", "t1", "slack")
	if _, err := LoadSnapshot(dir); err == nil {
		t.Error("S7: LoadSnapshot (PR mode) reads a dangling symlink as no policy file")
	}
}

func TestWatcherUnavailable_S7b_SoundFileReplacedByDanglingSymlink(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("fin", "t1", "slack"))
	m := NewManager(dir)
	assertNotLetThrough(t, m, "S7b sound file", "t1", "slack") // control: enforced
	path := policyPath(dir, "_domain_policy.yaml")
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(policyPath(dir, "gone.yaml"), path); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	_ = m.Reload()
	assertNotLetThrough(t, m, "S7b replaced by a dangling symlink", "t1", "slack")
}
