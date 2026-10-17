package gitops

// #1681 (direct-write timing half): Write / WriteIfUnchanged must decide on the
// tree their bytes land on, not on the tree they saw BEFORE queueing for the
// single-writer token. Deciding early fails in both directions, and each has
// a test here (#3064: on whether the tenant is git-managed): a write queued
// while its file becomes shared must be refused (bypass), and a write queued
// while its shared file is split must go through (over-reject).
//
// The interleaving is forced through the admission queue itself, not a sleep:
// the test holds the one execution token, so every request runs whatever it
// does before acquireWrite and then parks there. The queue is FIFO (a Go
// channel's waiting receivers are served in arrival order), and each request
// is started only after the previous one is seen parked, so the order they
// commit in is the order they were started in.

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// holdExecToken takes the writer's single execution token, so every write
// started afterwards parks in acquireWrite. The returned release gives it
// back; t.Cleanup also gives it back if the test ends first (a t.Fatalf while
// writes are parked), so no parked goroutine is left waiting forever.
func holdExecToken(t *testing.T, w *Writer) (release func()) {
	t.Helper()
	<-w.writeExec
	var once sync.Once
	release = func() { once.Do(func() { w.writeExec <- struct{}{} }) }
	t.Cleanup(release)
	return release
}

// waitParkedInAcquire blocks until n goroutines are parked in acquireWrite's
// wait for the execution token.
func waitParkedInAcquire(t *testing.T, n int) {
	t.Helper()
	waitParkedInAcquireUnless(t, n, func() bool { return false })
}

// waitParkedInAcquireUnless is waitParkedInAcquire that also returns, false,
// as soon as done reports true — for a write that may return before it ever
// reaches the queue.
func waitParkedInAcquireUnless(t *testing.T, n int, done func() bool) bool {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	buf := make([]byte, 1<<20)
	for {
		for {
			m := runtime.Stack(buf, true)
			if m < len(buf) {
				buf = buf[:m]
				break
			}
			buf = make([]byte, 2*len(buf))
		}
		parked := 0
		for _, g := range strings.Split(string(buf), "\n\n") {
			if strings.Contains(g, "[select") && strings.Contains(g, "gitops.(*Writer).acquireWrite") {
				parked++
			}
		}
		if parked >= n {
			return true
		}
		if done() {
			return false
		}
		if time.Now().After(deadline) {
			t.Fatalf("only %d goroutine(s) parked in acquireWrite, want %d", parked, n)
		}
		buf = buf[:cap(buf)]
		time.Sleep(time.Millisecond)
	}
}

// commitFile replaces name in dir and commits it — an operator's change in git
// reaching the serving tree while writes are queued.
func commitFile(t *testing.T, dir, name, body string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
	gitRun(t, dir, "add", "-A")
	gitRun(t, dir, "commit", "-q", "-m", "operator edits "+name)
}

// sharedMvX is mv-x.yaml shared with mv-y: git-managed (#3064).
const sharedMvX = "tenants:\n" +
	"  mv-x:\n    _silent_mode: \"warning\"\n" +
	"  mv-y:\n    _silent_mode: \"warning\"\n"

// TestDirectWrite_GitManagedIsJudgedUnderTheLock is #3064's timing half: the
// git-managed verdict is taken under the writer lock, on the file the write
// replaces — not on the file as it was when the request queued. A write
// started while mv-x.yaml is the tenant's own, and queued while an operator
// turns it into a file shared with mv-y, must be refused and change nothing:
// the body would otherwise overwrite mv-y's section.
func TestDirectWrite_GitManagedIsJudgedUnderTheLock(t *testing.T) {
	dir := seedTreeRepo(t, map[string]string{"mv-x.yaml": tenantBody("mv-x")})
	w := NewWriter(dir, dir)
	release := holdExecToken(t, w)

	done := make(chan error, 1)
	body := "tenants:\n  mv-x:\n    _silent_mode: \"critical\"\n"
	go func() {
		_, err := w.Write(context.Background(), "mv-x", "t1@example.com", body)
		done <- err
	}()
	waitParkedInAcquire(t, 1)
	commitFile(t, dir, "mv-x.yaml", sharedMvX)
	release()

	err := <-done
	var gm *GitManagedError
	if !errors.As(err, &gm) || gm.Reason != "tenant_count" || !errors.Is(err, ErrTenantGitManaged) {
		t.Fatalf("err = %v, want the git-managed refusal (tenant_count)", err)
	}
	if strings.Contains(err.Error(), "mv-y") {
		t.Errorf("the refusal names the other tenant: %v", err)
	}
	if got, rerr := os.ReadFile(filepath.Join(dir, "mv-x.yaml")); rerr != nil || string(got) != sharedMvX {
		t.Errorf("mv-x.yaml = %q (%v), want the operator's shared file unchanged", got, rerr)
	}
}

// TestDirectWrite_PreflightDoesNotRefuseOnAStaleTree pins the other direction
// (#1718's shape on the direct path): nothing decided before queueing may
// REFUSE a write that is legal on the tree it lands on.
func TestDirectWrite_PreflightDoesNotRefuseOnAStaleTree(t *testing.T) {
	// The file is shared (git-managed) when the write queues, and the
	// tenant's own once the operator splits it: the write lands on the
	// tenant's own file and goes through.
	t.Run("queued while the operator splits the shared file", func(t *testing.T) {
		dir := seedTreeRepo(t, map[string]string{"mv-x.yaml": sharedMvX})
		w := NewWriter(dir, dir)
		release := holdExecToken(t, w)
		done := make(chan error, 1)
		body := "tenants:\n  mv-x:\n    _silent_mode: \"critical\"\n"
		go func() {
			_, err := w.Write(context.Background(), "mv-x", "t1@example.com", body)
			done <- err
		}()
		if !waitParkedInAcquireUnless(t, 1, func() bool { return len(done) > 0 }) {
			release()
			t.Fatalf("Write(mv-x) returned before queueing: %v — refused on the tree as it was", <-done)
		}
		commitFile(t, dir, "mv-x.yaml", tenantBody("mv-x"))
		commitFile(t, dir, "mv-y.yaml", tenantBody("mv-y"))
		release()
		if err := <-done; err != nil {
			t.Fatalf("Write(mv-x): %v", err)
		}
		if got, err := os.ReadFile(filepath.Join(dir, "mv-x.yaml")); err != nil || string(got) != body {
			t.Errorf("mv-x.yaml = %q (%v), want the body", got, err)
		}
		if _, _, err := cfg.LoadDir(dir, nil); err != nil {
			t.Errorf("exporter refuses the resulting tree: %v", err)
		}
	})

	// A WriteIfUnchanged whose base is stale must hear ErrPrecondition —
	// "refresh and retry" — not a validation verdict about a file it has not
	// seen.
	t.Run("stale base hears ErrPrecondition", func(t *testing.T) {
		dir := seedTreeRepo(t, map[string]string{"mv-x.yaml": tenantBody("mv-x")})
		w := NewWriter(dir, dir)
		ctx := context.Background()
		staleHash := cfg.ComputeSourceHash([]byte(tenantBody("mv-x")))
		changed := "tenants:\n  mv-x:\n    _silent_mode: \"disable\"\n"
		if _, err := w.Write(ctx, "mv-x", "t1@example.com", changed); err != nil {
			t.Fatalf("Write(mv-x): %v", err)
		}
		body := "tenants:\n  mv-x:\n    _silent_mode: \"critical\"\n"
		_, err := w.WriteIfUnchanged(ctx, "mv-x", "t2@example.com", body, staleHash)
		if !errors.Is(err, ErrPrecondition) {
			t.Fatalf("WriteIfUnchanged on a stale base: err = %v, want ErrPrecondition", err)
		}
		if got, rerr := os.ReadFile(filepath.Join(dir, "mv-x.yaml")); rerr != nil || string(got) != changed {
			t.Errorf("mv-x.yaml = %q (%v), want unchanged", got, rerr)
		}
	})

	// #3064: on a git-managed file the precondition is never judged — even a
	// base hash that matches the shared file is answered with the
	// git-managed refusal, so CONFLICT versus not cannot confirm a guess of
	// a file tenant-api does not show.
	t.Run("git-managed is judged before the base hash", func(t *testing.T) {
		dir := seedTreeRepo(t, map[string]string{"mv-x.yaml": sharedMvX})
		w := NewWriter(dir, dir)
		body := "tenants:\n  mv-x:\n    _silent_mode: \"critical\"\n"
		for _, hash := range []string{cfg.ComputeSourceHash([]byte(sharedMvX)), "0000000000000000"} {
			_, err := w.WriteIfUnchanged(context.Background(), "mv-x", "t2@example.com", body, hash)
			if !errors.Is(err, ErrTenantGitManaged) || errors.Is(err, ErrPrecondition) {
				t.Errorf("base hash %s: err = %v, want the git-managed refusal", hash, err)
			}
		}
	})
}
