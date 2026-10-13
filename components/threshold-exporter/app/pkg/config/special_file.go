package config

import (
	"errors"
	"io"
	"io/fs"
	"os"
	"runtime"
	"syscall"
)

// ============================================================
// Special files: the ONE judgement (#2939)
// ============================================================
//
// A conf.d entry named like a config file can be something that is neither a
// regular file nor a directory: a FIFO, a unix socket, a device — or a
// symlink to one. Opening a FIFO that has no writer blocks in open(2), and
// reading it blocks until a writer closes; a character device such as
// /dev/zero never reaches EOF. Before #2939 only parse-tree's walk refused
// such an entry (#2897); the exporter's walk read it, so one `mkfifo` in
// conf.d hung every reload tick and every da-guard subcommand.
//
// IsSpecialFile is the predicate; the walk applies it to every entry before
// any read, in every mode, and records the entry as UnreadableSpecialFile.
// ReadConfFile applies the same predicate to the opened fd, for every later
// re-read of a path the walk kept (a warm scan carries files unread, and the
// readers downstream read them again) — an entry swapped for a FIFO between
// the walk and that read is refused too, without blocking.

// ErrSpecialFile is wrapped by ReadConfFile's error for a path that is
// neither a regular file nor a directory (IsSpecialFile).
var ErrSpecialFile = errors.New("not a regular file (a FIFO, a socket or a device)")

// IsSpecialFile reports whether mode — a stat result, a symlink already
// followed — is neither a regular file nor a directory. A directory is not
// special here: the walk descends it, and a config-named symlink to one is
// a NotFileDirLink.
func IsSpecialFile(mode fs.FileMode) bool {
	return !mode.IsRegular() && !mode.IsDir()
}

// openNonBlock is the extra open flag that keeps open(2) on a FIFO with no
// writer from blocking. O_NONBLOCK has no effect on a regular file (POSIX),
// the only kind ReadConfFile goes on to read. Chosen by runtime.GOOS rather
// than a build tag, as wrongPathErrnos is (stat_classify.go): Windows has no
// FIFO in this sense, and its os.OpenFile is not handed a flag it does not
// know.
var openNonBlock = func() int {
	if runtime.GOOS == "windows" {
		return 0
	}
	return syscall.O_NONBLOCK
}()

// ReadConfFile is os.ReadFile for a conf.d path, refusing a special file
// without blocking: the open does not wait for a FIFO writer, and fstat on
// that same fd decides before any read, so the object judged is the object
// read. Some special files fail already at open (a unix socket answers
// ENXIO); the open error is then classified by a stat of the path. Every
// refusal wraps ErrSpecialFile. A directory, a missing file and a
// permission error return what os.ReadFile returns.
func ReadConfFile(name string) ([]byte, error) {
	f, err := os.OpenFile(name, os.O_RDONLY|openNonBlock, 0) // #nosec G304 -- a conf.d path the walk listed
	if err != nil {
		if fi, serr := os.Stat(name); serr == nil && IsSpecialFile(fi.Mode()) {
			return nil, &fs.PathError{Op: "open", Path: name, Err: ErrSpecialFile}
		}
		return nil, err
	}
	defer func() { _ = f.Close() }() // read-only fd: a close error cannot lose data
	fi, err := f.Stat()
	if err != nil {
		return nil, err
	}
	if IsSpecialFile(fi.Mode()) {
		return nil, &fs.PathError{Op: "read", Path: name, Err: ErrSpecialFile}
	}
	// Sized like os.ReadFile, so the walk's per-file allocation count is
	// what it was (the warm-path benches pin it).
	size := 0
	if s := fi.Size(); int64(int(s)) == s {
		size = int(s)
	}
	size++ // one byte for the final read at EOF
	if size < 512 {
		size = 512
	}
	data := make([]byte, 0, size)
	for {
		n, rerr := f.Read(data[len(data):cap(data)])
		data = data[:len(data)+n]
		if rerr != nil {
			if errors.Is(rerr, io.EOF) {
				return data, nil
			}
			return nil, rerr // already a *fs.PathError naming the file, as os.ReadFile's
		}
		if len(data) >= cap(data) {
			data = append(data, 0)[:len(data)]
		}
	}
}
