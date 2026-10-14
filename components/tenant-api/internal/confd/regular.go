package confd

import (
	"errors"
	"fmt"
	"path/filepath"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// ErrNotRegularFile reports that a tenant file's path (after following
// symlinks) exists but is not a regular file — a FIFO, a device, a socket or
// a directory. It is the error-returning twin of ProblemNotRegularFile, for
// the per-tenant handlers that answer one request about one file (#2477).
var ErrNotRegularFile = errors.New("confd: not a regular file")

// ⛔ ONE RULE (#2939). Whether a conf.d entry may be opened, and how to open
// it without blocking on a FIFO, is pkg/config's OpenConfFile — the same
// IsSpecialFile the exporter's walk judges every entry with. This package
// only maps its verdict onto tenant-api's vocabulary (ErrNotRegularFile /
// ProblemNotRegularFile); it keeps no mode test or open flag of its own
// (TestConfd_NoSecondRegularFileRule).

// notRegular reports whether err is OpenConfFile's refusal of a path that is
// not a regular file: a special file, or a directory.
func notRegular(err error) bool {
	return errors.Is(err, cfg.ErrSpecialFile) || errors.Is(err, cfg.ErrDirectory)
}

// openError is OpenConfFile's error in this package's terms: a refusal
// becomes ErrNotRegularFile naming only the base name (no server path, as
// before); any other open error — os.ErrNotExist for "the file is gone", a
// permission error on a regular file — is returned unchanged.
func openError(path string, err error) error {
	if notRegular(err) {
		return fmt.Errorf("%w: %s", ErrNotRegularFile, filepath.Base(path))
	}
	return err
}

// ReadRegularFile reads path, refusing anything but a regular file with an
// error wrapping ErrNotRegularFile. Unlike ReadTenantFile it does not judge
// the bytes: a caller that serves or merges the content applies its own
// checks to them.
func ReadRegularFile(path string) ([]byte, error) {
	data, err := cfg.ReadConfFile(path)
	if err != nil {
		return nil, openError(path, err)
	}
	return data, nil
}

// CheckRegularFile returns nil when path is a regular file, an error wrapping
// ErrNotRegularFile when it exists but is not one, and the open/stat error
// otherwise. It reads nothing.
func CheckRegularFile(path string) error {
	f, _, err := cfg.OpenConfFile(path)
	if err != nil {
		return openError(path, err)
	}
	return f.Close()
}
