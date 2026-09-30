package confd

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// ErrNotRegularFile reports that a tenant file's path (after following
// symlinks) exists but is not a regular file — a FIFO, a device, a socket or
// a directory. It is the error-returning twin of ProblemNotRegularFile, for
// the per-tenant handlers that answer one request about one file (#2477).
var ErrNotRegularFile = errors.New("confd: not a regular file")

// openRegular opens path the way ReadTenantFile does — without blocking, then
// fstat on the opened fd — and returns the fd only when it is a regular file.
// An open error is returned unchanged, so os.ErrNotExist still reads as "the
// file is gone" to callers that map it to 404.
func openRegular(path string) (*os.File, error) {
	f, err := openNoBlock(path)
	if err != nil {
		return nil, err
	}
	fi, err := f.Stat()
	if err != nil {
		_ = f.Close()
		return nil, err
	}
	if !fi.Mode().IsRegular() {
		_ = f.Close()
		return nil, fmt.Errorf("%w: %s", ErrNotRegularFile, filepath.Base(path))
	}
	return f, nil
}

// ReadRegularFile reads path, refusing anything but a regular file with an
// error wrapping ErrNotRegularFile. Unlike ReadTenantFile it does not judge
// the bytes: a caller that serves or merges the content applies its own
// checks to them.
func ReadRegularFile(path string) ([]byte, error) {
	f, err := openRegular(path)
	if err != nil {
		return nil, err
	}
	defer func() { _ = f.Close() }() // read-only fd: a close error cannot lose data
	return io.ReadAll(f)
}

// CheckRegularFile returns nil when path is a regular file, an error wrapping
// ErrNotRegularFile when it exists but is not one, and the open/stat error
// otherwise. It reads nothing.
func CheckRegularFile(path string) error {
	f, err := openRegular(path)
	if err != nil {
		return err
	}
	return f.Close()
}
