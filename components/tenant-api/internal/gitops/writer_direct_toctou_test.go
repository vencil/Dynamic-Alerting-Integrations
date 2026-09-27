package gitops

// #1681 (direct-write timing half): Write / WriteIfUnchanged must decide on the
// tree their bytes land on, not on the tree they saw BEFORE queueing for the
// single-writer token. Deciding early fails in both directions, and each has
// a test here: three requests that each pass their own checks commit, in
// order, one tenant id declared by two files (bypass); and a write that is
// legal where it lands is refused on the tree an earlier queued write is
// about to change (over-reject).
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
	"gopkg.in/yaml.v3"
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

// filesDeclaring lists the top-level conf.d files whose `tenants:` map carries
// id. Deliberately its own YAML read rather than anything the writer uses to
// decide (addedTenantKeys, the #2078 walk): it is the oracle for them.
func filesDeclaring(t *testing.T, dir, id string) []string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		t.Fatal(err)
	}
	var out []string
	for _, e := range entries {
		if e.IsDir() || !strings.HasSuffix(e.Name(), ".yaml") {
			continue
		}
		raw, err := os.ReadFile(filepath.Join(dir, e.Name()))
		if err != nil {
			t.Fatal(err)
		}
		var doc struct {
			Tenants map[string]any `yaml:"tenants"`
		}
		if err := yaml.Unmarshal(raw, &doc); err != nil {
			continue
		}
		if _, ok := doc.Tenants[id]; ok {
			out = append(out, e.Name())
		}
	}
	return out
}

// TestDirectWrite_StaleOutOfLockCheckCannotReAddASection is the reviewer's
// three-request interleaving on #1681. mv-x.yaml starts out declaring both
// mv-x and mv-y:
//
//	T1 Write(mv-x)        body declares mv-x only        → removes mv-y
//	T2 WriteMerged(mv-y)  (walk under the lock: nobody declares mv-y) → creates mv-y.yaml
//	T3 Write(mv-x)        body still declares mv-y, checked BEFORE T1 ran
//
// If T3's verdict is the one it reached before queueing, mv-y is still in its
// baseline, the section is "grandfathered", and T3 writes it back into
// mv-x.yaml: two files declare mv-y and the exporter refuses the tree. The
// verdict that counts is the one against the tree T3's bytes land on, where
// mv-y is an addition — so T3 must fail with ErrValidation and change nothing.
func TestDirectWrite_StaleOutOfLockCheckCannotReAddASection(t *testing.T) {
	both := "tenants:\n" +
		"  mv-x:\n    _silent_mode: \"warning\"\n" +
		"  mv-y:\n    _silent_mode: \"warning\"\n"
	dir := seedTreeRepo(t, map[string]string{"mv-x.yaml": both})
	w := NewWriter(dir, dir)
	ctx := context.Background()

	// Hold the single execution token: every request below runs up to
	// acquireWrite and parks there, in the order it is started.
	release := holdExecToken(t, w)

	type result struct {
		name string
		err  error
	}
	results := make(chan result, 3)
	start := func(name string, fn func() error) {
		go func() { results <- result{name, fn()} }()
	}

	start("T1 Write(mv-x) drops mv-y", func() error {
		_, err := w.Write(ctx, "mv-x", "t1@example.com", tenantBody("mv-x"))
		return err
	})
	waitParkedInAcquire(t, 1)
	start("T2 WriteMerged(mv-y)", func() error {
		_, err := w.WriteMerged(ctx, "mv-y", "t2@example.com",
			func([]byte) (string, error) { return tenantBody("mv-y"), nil })
		return err
	})
	waitParkedInAcquire(t, 2)
	t3Body := "tenants:\n" +
		"  mv-x:\n    _silent_mode: \"critical\"\n" +
		"  mv-y:\n    _silent_mode: \"warning\"\n"
	start("T3 Write(mv-x) keeps mv-y", func() error {
		_, err := w.Write(ctx, "mv-x", "t3@example.com", t3Body)
		return err
	})
	waitParkedInAcquire(t, 3)

	// Release the token: T1, T2, T3 now run one at a time, in that order.
	release()
	errs := map[string]error{}
	for i := 0; i < 3; i++ {
		r := <-results
		errs[r.name] = r.err
	}
	for name, err := range errs {
		if strings.HasPrefix(name, "T3") {
			continue
		}
		if err != nil {
			t.Fatalf("%s: %v", name, err)
		}
	}

	t3Err := errs["T3 Write(mv-x) keeps mv-y"]
	decl := filesDeclaring(t, dir, "mv-y")
	_, _, loadErr := cfg.LoadDir(dir, nil)
	t.Logf("T3 err = %v; files declaring mv-y = %v; exporter LoadDir err = %v", t3Err, decl, loadErr)
	if len(decl) != 1 {
		t.Errorf("mv-y is declared by %d files %v, want exactly 1", len(decl), decl)
	}
	if loadErr != nil {
		t.Errorf("exporter refuses the tree the three writes left: %v", loadErr)
	}
	if got, err := os.ReadFile(filepath.Join(dir, "mv-x.yaml")); err != nil || string(got) != tenantBody("mv-x") {
		t.Errorf("mv-x.yaml = %q (%v), want T1's body — a refused T3 must change nothing", got, err)
	}
	if t3Err == nil {
		t.Errorf("T3 re-added mv-y to mv-x.yaml and reported success")
	} else if !errors.Is(t3Err, ErrValidation) || !strings.Contains(t3Err.Error(), "adds tenant section") {
		t.Errorf("T3 err = %v, want the added-section refusal", t3Err)
	}
}

// TestDirectWrite_PreflightDoesNotRefuseOnAStaleTree pins the other direction
// (#1718's shape on the direct path): nothing decided before queueing may
// REFUSE a write that is legal on the tree it lands on.
func TestDirectWrite_PreflightDoesNotRefuseOnAStaleTree(t *testing.T) {
	both := "tenants:\n" +
		"  mv-x:\n    _silent_mode: \"warning\"\n" +
		"  mv-y:\n    _silent_mode: \"warning\"\n"

	// T1 Write(mv-x) is queued to drop mv-y from the shared file; T2
	// Write(mv-y) is started after it. On the tree T2 lands on, mv-x.yaml no
	// longer declares mv-y, so creating mv-y.yaml is legal — sent one after the
	// other, both succeed. Queued, T2 must succeed too, not be refused with
	// ErrTenantDeclaredElsewhere on the tree as it was before T1 ran.
	t.Run("queued behind the write that frees the id", func(t *testing.T) {
		dir := seedTreeRepo(t, map[string]string{"mv-x.yaml": both})
		w := NewWriter(dir, dir)
		ctx := context.Background()
		release := holdExecToken(t, w)

		t1 := make(chan error, 1)
		go func() {
			_, err := w.Write(ctx, "mv-x", "t1@example.com", tenantBody("mv-x"))
			t1 <- err
		}()
		waitParkedInAcquire(t, 1)
		t2 := make(chan error, 1)
		go func() {
			_, err := w.Write(ctx, "mv-y", "t2@example.com", tenantBody("mv-y"))
			t2 <- err
		}()
		if !waitParkedInAcquireUnless(t, 2, func() bool { return len(t2) > 0 }) {
			release()
			err := <-t2
			<-t1
			t.Fatalf("T2 Write(mv-y) returned before queueing: %v — refused on the tree "+
				"T1 had not changed yet (ErrTenantDeclaredElsewhere=%v)",
				err, errors.Is(err, ErrTenantDeclaredElsewhere))
		}
		release()
		if err := <-t1; err != nil {
			t.Fatalf("T1 Write(mv-x): %v", err)
		}
		if err := <-t2; err != nil {
			t.Fatalf("T2 Write(mv-y): %v", err)
		}
		if decl := filesDeclaring(t, dir, "mv-y"); len(decl) != 1 || decl[0] != "mv-y.yaml" {
			t.Errorf("files declaring mv-y = %v, want [mv-y.yaml]", decl)
		}
		if _, _, err := cfg.LoadDir(dir, nil); err != nil {
			t.Errorf("exporter refuses the resulting tree: %v", err)
		}
	})

	// A WriteIfUnchanged whose base is stale must hear ErrPrecondition —
	// "refresh and retry" — not a validation verdict about a file it has not
	// seen. Its body still carries mv-y, which the current file no longer
	// declares; judged on the current file that is an added section.
	t.Run("stale base hears ErrPrecondition", func(t *testing.T) {
		dir := seedTreeRepo(t, map[string]string{"mv-x.yaml": both})
		w := NewWriter(dir, dir)
		ctx := context.Background()
		staleHash := cfg.ComputeSourceHash([]byte(both))
		if _, err := w.Write(ctx, "mv-x", "t1@example.com", tenantBody("mv-x")); err != nil {
			t.Fatalf("Write(mv-x): %v", err)
		}
		body := "tenants:\n" +
			"  mv-x:\n    _silent_mode: \"critical\"\n" +
			"  mv-y:\n    _silent_mode: \"warning\"\n"
		_, err := w.WriteIfUnchanged(ctx, "mv-x", "t2@example.com", body, staleHash)
		if !errors.Is(err, ErrPrecondition) {
			t.Fatalf("WriteIfUnchanged on a stale base: err = %v (ErrValidation=%v), want ErrPrecondition",
				err, errors.Is(err, ErrValidation))
		}
		if got, rerr := os.ReadFile(filepath.Join(dir, "mv-x.yaml")); rerr != nil || string(got) != tenantBody("mv-x") {
			t.Errorf("mv-x.yaml = %q (%v), want unchanged", got, rerr)
		}
	})
}
