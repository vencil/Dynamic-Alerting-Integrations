//go:build linux || darwin

package testutil

import (
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// AgeSymlink sets the mtime of the symlink ITSELF (not its target) to
// time.Now()-age. os.Chtimes follows the link, so it cannot do this.
//
// It exists for #1969: the conf.d walker's mtime fast-path used to take a
// symlink's own lstat. A test that wants that bug to be observable must make
// the LINK older than config.TreeScanMtimeGuard — otherwise the old walker
// re-reads the file because the link looks young, and the test passes on the
// defect (vacuous). Aging the link is what lets such a test fail on it
// without a real sleep past the guard.
func AgeSymlink(t testing.TB, path string, age time.Duration) {
	t.Helper()
	SetSymlinkMtime(t, path, time.Now().Add(-age))
}

// SetSymlinkMtime sets the mtime (and atime) of the symlink ITSELF to when,
// at microsecond resolution (lutimes takes a timeval). Pass a time with no
// sub-microsecond part when the test compares it with a file's mtime.
func SetSymlinkMtime(t testing.TB, path string, when time.Time) {
	t.Helper()
	tv := unix.NsecToTimeval(when.UnixNano())
	if err := unix.Lutimes(path, []unix.Timeval{tv, tv}); err != nil {
		t.Fatalf("SetSymlinkMtime(%q): %v", path, err)
	}
}
