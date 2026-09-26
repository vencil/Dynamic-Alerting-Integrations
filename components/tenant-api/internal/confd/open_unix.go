//go:build unix

package confd

import (
	"os"
	"syscall"
)

// openNoBlock opens path read-only WITHOUT blocking on a FIFO: O_RDONLY on a
// FIFO with no writer blocks in open(2) itself, and O_NONBLOCK is the only
// way to make that open return immediately. O_NONBLOCK has no effect on reads
// of a regular file (POSIX), which is the only kind ReadTenantFile goes on to
// read — anything else is rejected by fstat on this same fd first.
func openNoBlock(path string) (*os.File, error) {
	return os.OpenFile(path, os.O_RDONLY|syscall.O_NONBLOCK, 0)
}
