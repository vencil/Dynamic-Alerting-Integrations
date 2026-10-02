package config

import (
	"errors"
	"io/fs"
	"slices"
	"syscall"
)

// StatErrIsWrongPath is the one split, for a path the caller names
// (--config-dir, --scope), between "the path is wrong" — the caller's error
// (exit 2) — and "the path cannot be read" (exit 3, #2627). Wrong: the path
// does not exist (fs.ErrNotExist, which on Windows also covers
// ERROR_FILE_NOT_FOUND and ERROR_PATH_NOT_FOUND), or the platform's errno
// for a malformed path: wrongPathErrnos (stat_classify_other.go: ENOTDIR, a
// component on the way is not a directory, and ELOOP, symlinks loop;
// stat_classify_windows.go: windowsWrongPathErrnos). Every other stat
// failure (permission denied, EIO, a name too long, …) is a path that
// cannot be read, as the walker records any stat failure of an entry as
// UnreadableStatError.
func StatErrIsWrongPath(err error) bool {
	return statErrIsWrongPath(err, wrongPathErrnos)
}

// statErrIsWrongPath is StatErrIsWrongPath over an explicit errno set, so
// each platform's set is testable on any platform.
func statErrIsWrongPath(err error, wrong []syscall.Errno) bool {
	if errors.Is(err, fs.ErrNotExist) {
		return true
	}
	var errno syscall.Errno
	return errors.As(err, &errno) && slices.Contains(wrong, errno)
}

// windowsWrongPathErrnos is the wrong-path set on Windows, where stat never
// returns ENOTDIR or ELOOP. Numeric because package syscall does not name
// them all and no Windows-only dependency is wanted for four constants;
// values from the Win32 system error codes (winerror.h,
// https://learn.microsoft.com/windows/win32/debug/system-error-codes--0-499-
// and ...--1700-3999-):
//
//	ERROR_INVALID_NAME          123  the file name, directory name or volume label syntax is incorrect
//	ERROR_BAD_PATHNAME          161  the specified path is invalid
//	ERROR_DIRECTORY             267  the directory name is invalid (a component is not a directory)
//	ERROR_CANT_RESOLVE_FILENAME 1921 the name of the file cannot be resolved by the system (link loop)
var windowsWrongPathErrnos = []syscall.Errno{123, 161, 267, 1921}
