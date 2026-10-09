package policy

// availability_test.go — Manager.Unavailable / CheckAvailable (hub #2486
// Q7-2 甲軸): which file states make the policy unavailable, and the
// --policy-unavailable-open escape hatch. watcher_unavailable_test.go pins the
// shapes that must not let a forbidden write through; this file pins the
// other direction — the states that must NOT be reported unavailable.

import (
	"errors"
	"os"
	"testing"

	"github.com/vencil/tenant-api/internal/testutil"
)

func TestUnavailable_FileStates(t *testing.T) {
	t.Parallel()
	t.Run("no policy file", func(t *testing.T) {
		t.Parallel()
		if err := NewManager(t.TempDir()).Unavailable(); err != nil {
			t.Errorf("Unavailable = %v, want nil: no file is no policy", err)
		}
	})
	t.Run("sound file", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("fin", "t1", "slack"))
		if err := NewManager(dir).Unavailable(); err != nil {
			t.Errorf("Unavailable = %v, want nil", err)
		}
	})
	t.Run("broken at startup, then repaired, then broken again, then removed", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		testutil.WriteYAML(t, dir, "_domain_policy.yaml", brokenForbidding("fin", "t1", "slack"))
		m := NewManager(dir)
		if err := m.Unavailable(); !errors.Is(err, ErrUnavailable) {
			t.Fatalf("broken at startup: Unavailable = %v, want ErrUnavailable", err)
		}
		testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("fin", "t1", "slack"))
		if err := m.Reload(); err != nil {
			t.Fatalf("Reload: %v", err)
		}
		if err := m.Unavailable(); err != nil {
			t.Fatalf("repaired: Unavailable = %v, want nil", err)
		}
		testutil.WriteYAML(t, dir, "_domain_policy.yaml", brokenForbidding("fin", "t1", "slack"))
		_ = m.Reload()
		if err := m.Unavailable(); err != nil {
			t.Errorf("broken after a good parse: Unavailable = %v, want nil (its last good content applies)", err)
		}
		if v := m.CheckWrite("t1", map[string]string{"_routing_receiver_type": "slack"}); len(v) == 0 {
			t.Error("broken after a good parse: the last good policy no longer forbids slack")
		}
		if err := os.Remove(policyPath(dir, "_domain_policy.yaml")); err != nil {
			t.Fatal(err)
		}
		if err := m.Reload(); err != nil {
			t.Fatalf("Reload after remove: %v", err)
		}
		if err := m.Unavailable(); err != nil {
			t.Errorf("removed: Unavailable = %v, want nil", err)
		}
	})
	t.Run("generator-parity refusal is unavailable", func(t *testing.T) {
		t.Parallel()
		dir := t.TempDir()
		// #2730 §1: a second document — the generator drops the file.
		testutil.WriteYAML(t, dir, "_domain_policy.yaml", forbidding("fin", "t1", "slack")+"---\n")
		if err := NewManager(dir).Unavailable(); !errors.Is(err, ErrUnavailable) {
			t.Errorf("Unavailable = %v, want ErrUnavailable", err)
		}
	})
	t.Run("LoadSnapshot and NewForTest are never unavailable", func(t *testing.T) {
		t.Parallel()
		if err := NewForTest(emptyConfig()).Unavailable(); err != nil {
			t.Errorf("NewForTest: Unavailable = %v", err)
		}
		var nilMgr *Manager
		if err := nilMgr.Unavailable(); err != nil {
			t.Errorf("nil Manager: Unavailable = %v", err)
		}
	})
}

func TestCheckAvailable_EscapeHatch(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	if err := os.Mkdir(policyPath(dir, "_domain_policy.yml"), 0o755); err != nil {
		t.Fatal(err)
	}
	m := NewManager(dir)
	if err := m.CheckAvailable("test"); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("CheckAvailable = %v, want ErrUnavailable", err)
	}
	if err := m.RefusesWrites(); !errors.Is(err, ErrUnavailable) {
		t.Fatalf("RefusesWrites = %v, want ErrUnavailable", err)
	}
	if m.OpenPasses() != 0 {
		t.Fatalf("OpenPasses = %d before the hatch is open", m.OpenPasses())
	}
	m.SetOpenOnUnavailable(true)
	// The batch-level gate lets nothing through on its own account (N2,
	// hub #2486 PR-7c round 2): only CheckAvailable, once per write, counts.
	if err := m.RefusesWrites(); err != nil {
		t.Fatalf("RefusesWrites with --policy-unavailable-open = %v, want nil", err)
	}
	for i := 0; i < 2; i++ {
		if err := m.CheckAvailable("test"); err != nil {
			t.Fatalf("CheckAvailable with --policy-unavailable-open = %v, want nil", err)
		}
	}
	if got := m.OpenPasses(); got != 2 {
		t.Errorf("OpenPasses = %d, want 2", got)
	}
	if m.Unavailable() == nil {
		t.Error("the escape hatch must not hide the state: Unavailable = nil")
	}
}
