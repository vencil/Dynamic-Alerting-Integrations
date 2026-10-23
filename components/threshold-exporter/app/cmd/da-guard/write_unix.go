//go:build unix

package main

import (
	"fmt"
	"os"
	"syscall"
)

// openNoBlock opens abs for reading without following a final symlink and
// without blocking: a FIFO with no writer opens at once instead of hanging
// (the caller fstats the handle and reads only a regular file).
func openNoBlock(abs string) (*os.File, error) {
	return os.OpenFile(abs, os.O_RDONLY|syscall.O_NONBLOCK|syscall.O_NOFOLLOW, 0) // #nosec G304 -- inside --config-dir
}

// chownLike gives f the owner and group of orig, best effort, and returns
// what it could not carry over ("" when nothing): a process that does not
// own the original (not uid 0) cannot give the file away, but may still set
// the group when it is a member — so the group is tried alone after the pair
// fails, and only what still fails is reported.
func chownLike(f *os.File, orig os.FileInfo) string {
	st, ok := orig.Sys().(*syscall.Stat_t)
	if !ok {
		return ""
	}
	err := f.Chown(int(st.Uid), int(st.Gid))
	if err == nil {
		return ""
	}
	if gerr := f.Chown(-1, int(st.Gid)); gerr == nil {
		return fmt.Sprintf("the written file could not be given the original's owner (uid %d): %v; its group (gid %d) is kept", st.Uid, err, st.Gid)
	}
	return fmt.Sprintf("the written file could not be given the original's owner and group (uid %d, gid %d): %v", st.Uid, st.Gid, err)
}
