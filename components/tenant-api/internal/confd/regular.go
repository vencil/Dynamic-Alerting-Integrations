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

// notRegularAfterOpenFailure classifies a path whose open just failed: when
// it still stats as something other than a regular file (a unix socket
// answers ENXIO at open, before any fstat could run), the answer is an error
// wrapping ErrNotRegularFile; otherwise nil, and the caller keeps its own
// verdict for the open error. The one classifier behind both openRegular and
// ReadTenantFile, so the list row and the per-tenant endpoints name the same
// reason for the same file.
func notRegularAfterOpenFailure(path string) error {
	if fi, err := os.Stat(path); err == nil && !fi.Mode().IsRegular() {
		return fmt.Errorf("%w: %s", ErrNotRegularFile, filepath.Base(path))
	}
	return nil
}

// openRegular opens path the way ReadTenantFile does — without blocking, then
// fstat on the opened fd — and returns the fd only when it is a regular file.
//
// Some special files fail already at open (a unix socket answers ENXIO), so
// an open error is classified too: when path still stats as something other
// than a regular file, the answer is ErrNotRegularFile. Any other open error
// is returned unchanged, so os.ErrNotExist still reads as "the file is gone"
// to callers that map it to 404, and a permission error on a regular file
// stays a permission error.
func openRegular(path string) (*os.File, error) {
	f, err := openNoBlock(path)
	if err != nil {
		if nr := notRegularAfterOpenFailure(path); nr != nil {
			return nil, nr
		}
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
