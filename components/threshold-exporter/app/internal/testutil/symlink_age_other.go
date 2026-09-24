//go:build !(linux || darwin)

package testutil

import (
	"testing"
	"time"
)

// AgeSymlink skips the calling test: setting a symlink's own mtime needs
// lutimes, which this helper only wires on linux and darwin. See the
// linux/darwin build of this function for why the tests need it.
func AgeSymlink(t testing.TB, path string, age time.Duration) {
	t.Helper()
	_ = age
	t.Skipf("AgeSymlink(%q): setting a symlink's own mtime is not wired on this platform", path)
}

// SetSymlinkMtime skips the calling test; see AgeSymlink.
func SetSymlinkMtime(t testing.TB, path string, when time.Time) {
	t.Helper()
	_ = when
	t.Skipf("SetSymlinkMtime(%q): setting a symlink's own mtime is not wired on this platform", path)
}
