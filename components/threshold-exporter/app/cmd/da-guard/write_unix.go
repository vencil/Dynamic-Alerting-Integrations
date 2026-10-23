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

// ownerOf is orig's owner and group (ok false: the platform carries none).
func ownerOf(orig os.FileInfo) (uid, gid int, ok bool) {
	st, ok := orig.Sys().(*syscall.Stat_t)
	if !ok {
		return 0, 0, false
	}
	return int(st.Uid), int(st.Gid), true
}
