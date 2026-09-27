package gitops

// #1681 (direct-write timing half): Write / WriteIfUnchanged decide on a tree
// they read BEFORE queueing for the single-writer token. Three requests that
// each pass their own checks can then commit, in order, a tree the exporter
// refuses: one tenant id declared by two files.
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
	"testing"
	"time"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"gopkg.in/yaml.v3"
)

// waitParkedInAcquire blocks until n goroutines are parked in acquireWrite's
// wait for the execution token.
func waitParkedInAcquire(t *testing.T, n int) {
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
			return
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
	<-w.writeExec

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
	w.writeExec <- struct{}{}
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
