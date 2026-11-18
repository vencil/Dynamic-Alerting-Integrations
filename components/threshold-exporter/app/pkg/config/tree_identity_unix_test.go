//go:build unix

package config

// #4042: an Unreadable record names the path, not why. The same path
// failing for another reason (a dangling link, then a FIFO) leaves the
// identity alone, so a reason that flaps on a flaky filesystem does not
// reload the exporter on every flip; the entry is dropped from what is
// served either way.

import (
	"os"
	"path/filepath"
	"syscall"
	"testing"
)

func TestTreeIdentity_AnUnreadableReasonIsNotInIt(t *testing.T) {
	t.Parallel()
	dir := identityTreeFiles(t)
	link := filepath.Join(dir, "team-c.yaml")
	symlinkOrSkip(t, filepath.Join(dir, "nowhere.yaml"), link)
	dangling := identityOf(t, dir)
	if err := os.Remove(link); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(link, 0o600); err != nil {
		t.Skipf("mkfifo unavailable here (%v)", err)
	}
	if fifo := identityOf(t, dir); fifo != dangling {
		t.Errorf("the same unreadable path changed the identity with its reason: %s -> %s", dangling, fifo)
	}
}
