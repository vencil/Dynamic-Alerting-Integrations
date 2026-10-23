//go:build unix

package main

import (
	"os"
	"syscall"
)

// openNoBlock opens abs for reading without following a final symlink and
// without blocking: a FIFO with no writer opens at once instead of hanging
// (the caller fstats the handle and reads only a regular file).
func openNoBlock(abs string) (*os.File, error) {
	return os.OpenFile(abs, os.O_RDONLY|syscall.O_NONBLOCK|syscall.O_NOFOLLOW, 0) // #nosec G304 -- inside --config-dir
}

// chownLike gives f the owner and group of orig (best effort; the caller
// reports a failure).
func chownLike(f *os.File, orig os.FileInfo) error {
	st, ok := orig.Sys().(*syscall.Stat_t)
	if !ok {
		return nil
	}
	return f.Chown(int(st.Uid), int(st.Gid))
}
