//go:build unix

package federation

import (
	"errors"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"

	"github.com/vencil/tenant-api/internal/handler"
)

// TestReadFederationSubset_RejectsBeforeTouchingDisk pins the ORDER of
// the predicate and the read — something the rejection assertions in
// policy_test.go structurally cannot see. A guard moved BELOW
// os.ReadFile returns the same sentinel for the same ids and passes
// every one of those rows, having already opened and read the escaped
// file before throwing it away.
//
// Probing by error TYPE does not catch that, and this is measured, not
// assumed: the first version of this test planted a directory at the
// target expecting EISDIR, and the mutant passed it. A guard sitting
// after the read returns its own sentinel and swallows whatever
// os.ReadFile reported, so EISDIR and EACCES are both invisible.
//
// A FIFO makes the read itself observable rather than its error: with no
// writer at the other end, os.ReadFile BLOCKS in open(2). Reaching the
// sentinel promptly is then evidence that the predicate ran first, and
// blocking is evidence that it did not.
//
// unix-only for syscall.Mkfifo. The service ships as a Linux container,
// and this is a second line of evidence for a branch policy_test.go
// already covers, so the build tag costs no coverage where it runs.
func TestReadFederationSubset_RejectsBeforeTouchingDisk(t *testing.T) {
	t.Parallel()

	root := t.TempDir()
	configDir := filepath.Join(root, "conf.d")
	const escaping = "foo/../../../outside/secret"
	target := federationSubsetPath(configDir, escaping)
	if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
		t.Fatalf("mkdir %s: %v", filepath.Dir(target), err)
	}
	if err := syscall.Mkfifo(target, 0o600); err != nil {
		t.Fatalf("mkfifo %s: %v", target, err)
	}

	d := &handler.Deps{ConfigDir: configDir}
	done := make(chan error, 1)
	go func() {
		_, err := readFederationSubset(d, escaping)
		done <- err
	}()

	select {
	case err := <-done:
		if !errors.Is(err, ErrUnaddressableTenantID) {
			t.Fatalf("err = %v, want ErrUnaddressableTenantID", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatalf("readFederationSubset blocked opening the FIFO at %s — os.ReadFile reached the escaped path before the predicate ran", target)
	}
}
