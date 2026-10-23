package main

// write_commit.go — the write phase of `da-guard edit` / `emit`, once the
// whole batch passed its judgement (write.go). Three steps, so that as much
// as possible fails before anything is in place:
//
//  1. every file's text goes into a hidden temporary file in its own
//     directory (no walk reads a hidden name), with its mode (and, for edit,
//     the original's owner and group when they can be set), synced; emit
//     first makes the missing directories. A failure: every temporary file
//     and directory made is removed — nothing written.
//  2. every target is checked once more: edit — the file still holds the
//     bytes it was read from; emit — the path does not exist. One that
//     fails: refused changed_during_write, nothing written.
//  3. each file goes in place: edit renames (checking its bytes once more
//     right before); emit hard-links, which never replaces a path created
//     meanwhile (a file system without hard links: check, then rename).
//
// ⛔ A FAILURE IN STEP 3 is the one that leaves files behind. emit's files
// are new, so the ones already linked are removed again (nothing is lost;
// one that cannot be removed is maybe_written). edit's renames cannot be
// undone: the files renamed by then are written, and the call's reason is
// partial_write. The checks are comparisons, not locks: what another writer
// writes between a check and the rename is overwritten.

import (
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

// commit writes every pending changed file (write_commit.go's steps).
func (w *writer) commit(files []*writeFile) int {
	var todo []*writeFile
	for _, f := range files {
		switch {
		case f.unchanged:
			f.res.Status = writeStatusUnchanged
			fmt.Fprintf(w.errOut, "%s %s: %s: unchanged; nothing written\n", programName, w.cmd, f.rel)
		case f.pending:
			todo = append(todo, f)
		}
	}
	var created []string // the directories this call made, in the order made
	done := false
	defer func() {
		for _, f := range todo {
			if f.tmp != "" {
				_ = os.Remove(f.tmp) // after a link or a rename only the temporary name goes
			}
		}
		if !done {
			for i := len(created) - 1; i >= 0; i-- {
				_ = os.Remove(created[i]) // an empty one only: anything put in it stays, and so does it
			}
		}
	}()

	// 1. The temporary files.
	for _, f := range todo {
		if w.cmd == emitCmd {
			if err := w.makeDirs(filepath.Dir(f.abs), &created); err != nil {
				w.fileCallerError(f, writeReasonWriteFailed, "%v", err)
				return w.stopped(writeReasonWriteFailed)
			}
		}
		if err := w.temporary(f); err != nil {
			w.fileCallerError(f, writeReasonWriteFailed, "%v", err)
			return w.stopped(writeReasonWriteFailed)
		}
	}
	// 2. Every target once more, before any is touched.
	changed := false
	for _, f := range todo {
		if !w.unchangedOnDisk(f) {
			w.refuse(f, writeReasonChangedDuringWrite, w.changedMessage())
			changed = true
		}
	}
	if changed {
		return w.stopped("")
	}
	// 3. In place.
	var placed []*writeFile
	for _, f := range todo {
		if reason, err := w.place(f); err != nil {
			if reason == writeReasonChangedDuringWrite {
				w.refuse(f, reason, err.Error())
			} else {
				w.fileCallerError(f, reason, "%v", err)
			}
			if w.cmd == emitCmd {
				w.rollBack(placed)
			}
			return w.stopped(reason)
		}
		placed = append(placed, f)
		if w.cmd == editCmd {
			f.res.Status = writeStatusWritten // a rename is not undone
		}
	}
	done = true
	synced := map[string]bool{}
	for _, f := range todo {
		f.res.Status = writeStatusWritten
		fmt.Fprintf(w.errOut, "%s %s: wrote %s\n", programName, w.cmd, f.rel)
		if dir := filepath.Dir(f.abs); !synced[dir] {
			synced[dir] = true
			if d, err := os.Open(dir); err == nil { // #nosec G304 -- a directory just written
				_ = d.Sync()
				_ = d.Close()
			}
		}
	}
	w.res.Status = writeStatusUnchanged
	if len(todo) > 0 {
		w.res.Status = writeStatusWritten
	}
	return w.finish(exitOK)
}

// stopped ends a call whose write phase stopped on a file (its result is
// already recorded; reason is that file's, "" for a refusal in step 2):
// partial_write when any file is (or may be) in place, else the file's own
// outcome — a refusal (exit 1) or write_failed (exit 2).
func (w *writer) stopped(reason string) int {
	var written, maybe []string
	for _, r := range w.res.Files {
		switch r.Status {
		case writeStatusWritten:
			written = append(written, r.Path)
		case writeStatusMaybeWritten:
			maybe = append(maybe, r.Path)
		}
	}
	switch {
	case len(written)+len(maybe) > 0:
		msg := fmt.Sprintf("the write stopped after %d file(s) were put in place (%d may be); they stay: "+
			"written %q, maybe written %q", len(written), len(maybe), written, maybe)
		fmt.Fprintf(w.errOut, "%s %s: %s\n", programName, w.cmd, msg)
		w.res.Status, w.res.Reason, w.res.Message = writeStatusCallerError, strp(writeReasonPartialWrite), strp(msg)
		return w.finish(exitCallerErr)
	case reason == "" || reason == writeReasonChangedDuringWrite:
		msg := "a file changed after it was judged; nothing was written"
		fmt.Fprintf(w.errOut, "%s %s: %s\n", programName, w.cmd, msg)
		w.res.Status, w.res.Message = writeStatusRefused, strp(msg)
		return w.finish(exitFindings)
	}
	msg := "a write failed; nothing was written"
	fmt.Fprintf(w.errOut, "%s %s: %s\n", programName, w.cmd, msg)
	w.res.Status, w.res.Reason, w.res.Message = writeStatusCallerError, strp(writeReasonWriteFailed), strp(msg)
	return w.finish(exitCallerErr)
}

func (w *writer) changedMessage() string {
	if w.cmd == editCmd {
		return "the file changed after it was read; re-run on its new content"
	}
	return "the path was created after it was judged; nothing replaced it"
}

// makeDirs makes dir and every missing directory above it, one level at a
// time, appending each it made to created: a level counts as this call's
// only when its Mkdir succeeds — one another process made meanwhile
// (EEXIST) is not ours, and the clean-up never removes it.
func (w *writer) makeDirs(dir string, created *[]string) error {
	var missing []string // innermost first
	for d := dir; ; d = filepath.Dir(d) {
		if _, err := os.Lstat(d); err == nil || filepath.Dir(d) == d {
			break
		}
		missing = append(missing, d)
	}
	mkdir := w.mkdir
	if mkdir == nil {
		mkdir = os.Mkdir
	}
	for i := len(missing) - 1; i >= 0; i-- {
		switch err := mkdir(missing[i], 0o755); { // #nosec G301 -- conf.d directories are world-readable by convention
		case err == nil:
			*created = append(*created, missing[i])
		case errors.Is(err, fs.ErrExist):
			if fi, serr := os.Stat(missing[i]); serr != nil || !fi.IsDir() {
				return fmt.Errorf("%s exists and is no directory", missing[i])
			}
		default:
			return err
		}
	}
	return nil
}

// temporary writes f.out to a hidden temporary file beside f.abs (f.tmp):
// its mode, for edit the original's owner and group when they can be set
// (else a warning), synced and closed.
func (w *writer) temporary(f *writeFile) error {
	tmp, err := os.CreateTemp(filepath.Dir(f.abs), "."+filepath.Base(f.abs)+".da-guard-*")
	if err != nil {
		return err
	}
	f.tmp = tmp.Name()
	_, werr := tmp.Write(f.out)
	if werr == nil {
		werr = tmp.Chmod(f.perm)
	}
	if werr == nil && f.ofi != nil {
		if uid, gid, ok := ownerOf(f.ofi); ok {
			chown := w.chown
			if chown == nil {
				chown = tmp.Chown
			}
			// The temporary file's owner and group; unknown (-2, no id)
			// when it cannot be statted, so both are set.
			cur, curGID := -2, -2
			if fi, err := tmp.Stat(); err == nil {
				if u, g, ok := ownerOf(fi); ok {
					cur, curGID = u, g
				}
			}
			if msg := ownerWarning(chown, uid, gid, cur, curGID); msg != "" {
				w.warn(f, "%s", msg)
			}
		}
	}
	if werr == nil {
		werr = tmp.Sync()
	}
	if cerr := tmp.Close(); werr == nil {
		werr = cerr
	}
	return werr
}

// unchangedOnDisk: edit — f.abs still holds f.original (a regular file);
// emit — nothing is at f.abs.
func (w *writer) unchangedOnDisk(f *writeFile) bool {
	if w.cmd == editCmd {
		now, _, regular, err := readRegular(f.abs)
		return err == nil && regular && bytes.Equal(now, f.original)
	}
	_, err := os.Lstat(f.abs)
	return errors.Is(err, fs.ErrNotExist)
}

// place puts f's temporary file at f.abs; on failure the reason
// (changed_during_write or write_failed) and the error.
func (w *writer) place(f *writeFile) (string, error) {
	rename := w.rename
	if rename == nil {
		rename = os.Rename
	}
	if w.cmd == editCmd {
		if !w.unchangedOnDisk(f) {
			return writeReasonChangedDuringWrite, errors.New(w.changedMessage())
		}
		if err := rename(f.tmp, f.abs); err != nil {
			return writeReasonWriteFailed, err
		}
		f.tmp = ""
		return "", nil
	}
	link := w.link
	if link == nil {
		link = os.Link
	}
	// The temporary file's inode is the file put in place, by the link or
	// the rename alike: what a roll-back may remove (rollBack).
	staged, serr := os.Lstat(f.tmp)
	if serr != nil {
		return writeReasonWriteFailed, serr
	}
	// A hard link never replaces what is there: a path created meanwhile is
	// not overwritten.
	switch err := link(f.tmp, f.abs); {
	case errors.Is(err, fs.ErrExist):
		return writeReasonChangedDuringWrite, errors.New(w.changedMessage())
	case err != nil:
		// A file system without hard links: check, then rename.
		if _, lerr := os.Lstat(f.abs); !errors.Is(lerr, fs.ErrNotExist) {
			return writeReasonChangedDuringWrite, errors.New(w.changedMessage())
		}
		if err := rename(f.tmp, f.abs); err != nil {
			return writeReasonWriteFailed, err
		}
		f.tmp = ""
	}
	f.placed = staged
	return "", nil
}

// rollBack removes the emitted files already in place again: they are new,
// so nothing is lost. A path is removed only while it is still the file
// this call put there (os.SameFile against its inode): one another writer
// replaced meanwhile is left as it is. One that cannot be removed, or no
// longer can be told to be ours, is maybe_written.
func (w *writer) rollBack(placed []*writeFile) {
	remove := w.remove
	if remove == nil {
		remove = os.Remove
	}
	for _, f := range placed {
		now, lerr := os.Lstat(f.abs)
		switch {
		case errors.Is(lerr, fs.ErrNotExist):
			f.res.Status, f.res.Message = writeStatusNotWritten, strp("written, and gone again when the call stopped")
			continue
		case lerr != nil || f.placed == nil || !os.SameFile(now, f.placed):
			why := "the path now holds a file this call did not put there; it is left as it is"
			if lerr != nil {
				why = "the path cannot be checked: " + lerr.Error()
			}
			msg := "written, and not removed when the call stopped: " + why
			fmt.Fprintf(w.errOut, "%s %s: %s: %s\n", programName, w.cmd, f.rel, msg)
			f.res.Status, f.res.Message = writeStatusMaybeWritten, strp(msg)
			continue
		}
		if err := remove(f.abs); err != nil && !errors.Is(err, fs.ErrNotExist) {
			msg := fmt.Sprintf("written, and could not be removed again when the call stopped: %v", err)
			fmt.Fprintf(w.errOut, "%s %s: %s: %s\n", programName, w.cmd, f.rel, msg)
			f.res.Status, f.res.Message = writeStatusMaybeWritten, strp(msg)
			continue
		}
		f.res.Status, f.res.Message = writeStatusNotWritten, strp("written, and removed again when the call stopped")
	}
}
