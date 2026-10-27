package main

// write_commit.go — the write phase of `da-guard write`, once the whole batch
// passed its judgement (write.go). Three steps, so that as much as possible
// fails before anything is in place:
//
//  1. every edited and created file's text goes into a hidden temporary file
//     in its own directory (no walk reads a hidden name), with its mode (and,
//     for an edit, the original's owner and group when they can be set),
//     synced; a create or a move first makes its target's missing
//     directories. A failure: every temporary file and directory made is
//     removed — nothing written.
//  2. every file is checked once more: an edited file and a move's source
//     still hold the bytes they were read from; no create's or move's target
//     exists. One that fails: refused changed_during_write, nothing written.
//  3. each file goes in place — creates, then moves, then edits (the order
//     in which what is done can still be undone comes first): a create
//     hard-links its temporary file at its path, which never replaces a path
//     created meanwhile (a file system without hard links: check, then
//     rename); a move hard-links its source at its target the same way and
//     then removes the source name (while it is still that file); an edit
//     renames, checking its bytes once more right before.
//
// ⛔ A FAILURE IN STEP 3 is the one that leaves files behind. Creates and
// moves already in place are undone (a created file removed, a moved one
// linked back at its source), each only while its path still holds the file
// this call put there (os.SameFile); one that cannot be undone is
// maybe_written. An edit's rename cannot be undone: the files renamed by then
// are written, and the call's reason is partial_write. The checks are
// comparisons, not locks: what another writer writes between a check and the
// rename is overwritten.
//
// A replace and a delete go with the groups above (writeFile.group): a
// replace of an existing file is an edit's rename over it (checked and not
// undone as an edit's), a replace of a missing path a create; a delete goes
// with the moves — it first hard-links the file at a hidden name beside it
// (a rename there on a file system without hard links), then removes the
// path's name while it is still that file. Undone, the hidden link is linked
// back at the path (os.SameFile: still the file removed), else the file is
// maybe_written and the hidden link left as its one name; once the call is
// done the hidden link is removed.

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
)

// placeOrder is step 3's order (by writeFile.group): what can be undone
// first.
var placeOrder = map[string]int{writeKindCreate: 0, writeKindMove: 1, writeKindEdit: 2}

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
	sort.SliceStable(todo, func(i, j int) bool { return placeOrder[todo[i].group()] < placeOrder[todo[j].group()] })
	var created []string // the directories this call made, in the order made
	done := false
	defer func() {
		for _, f := range todo {
			if f.tmp != "" {
				_ = os.Remove(f.tmp) // after a link or a rename only the temporary name goes
			}
			if !done && f.backup != "" && !f.keepBackup {
				_ = os.Remove(f.backup) // a delete not done, or put back: its file is at its path
			}
		}
		if !done {
			for i := len(created) - 1; i >= 0; i-- {
				_ = os.Remove(created[i]) // an empty one only: anything put in it stays, and so does it
			}
		}
	}()

	// 1. The temporary files (a move has none: its file is the source; nor a
	// delete).
	for _, f := range todo {
		if f.kind == writeKindDelete {
			continue
		}
		if f.group() != writeKindEdit {
			if err := w.makeDirs(filepath.Dir(f.abs), &created); err != nil {
				w.fileCallerError(f, writeReasonWriteFailed, "%v", err)
				return w.stopped(writeReasonWriteFailed)
			}
		}
		if f.kind == writeKindMove {
			continue
		}
		if err := w.temporary(f); err != nil {
			w.fileCallerError(f, writeReasonWriteFailed, "%v", err)
			return w.stopped(writeReasonWriteFailed)
		}
	}
	// 2. Every file once more, before any is touched.
	changed := false
	for _, f := range todo {
		if !w.unchangedOnDisk(f) {
			w.refuse(f, writeReasonChangedDuringWrite, changedMessage(f))
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
			if f.group() == writeKindMove && f.placed != nil {
				placed = append(placed, f) // its target link (a delete's hidden one) is made: undone with the rest
			}
			w.rollBack(placed)
			return w.stopped(reason)
		}
		placed = append(placed, f)
		if f.group() == writeKindEdit {
			f.res.Status = writeStatusWritten // a rename is not undone
		}
	}
	done = true
	synced := map[string]bool{}
	for _, f := range todo {
		f.res.Status = writeStatusWritten
		if f.kind == writeKindDelete {
			fmt.Fprintf(w.errOut, "%s %s: deleted %s\n", programName, w.cmd, f.rel)
			if f.backup != "" {
				if err := os.Remove(f.backup); err != nil && !errors.Is(err, fs.ErrNotExist) {
					w.warn(f, "the hidden link %s to the deleted file could not be removed: %v", filepath.Base(f.backup), err)
				}
			}
		} else {
			fmt.Fprintf(w.errOut, "%s %s: wrote %s\n", programName, w.cmd, f.rel)
		}
		dirs := []string{filepath.Dir(f.abs)}
		if f.kind == writeKindMove {
			dirs = append(dirs, filepath.Dir(f.fromAbs))
		}
		for _, dir := range dirs {
			if synced[dir] {
				continue
			}
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

func changedMessage(f *writeFile) string {
	switch {
	case f.kind == writeKindDelete:
		return "the file changed after it was read; nothing was deleted"
	case f.group() == writeKindEdit:
		return "the file changed after it was read; re-run on its new content"
	case f.kind == writeKindMove:
		return "the source changed, or the target was created, after it was judged; nothing was moved"
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
// its mode, for an edit the original's owner and group when they can be set
// (else a warning), synced and closed. ⛔ The name does not hold f.abs's
// base name: a file name near NAME_MAX (255 bytes; a 250-character
// Kubernetes object name + `.yaml`) would make the temporary one too long
// to create, and a file that can be written directly could not be written
// through here.
func (w *writer) temporary(f *writeFile) error {
	tmp, err := os.CreateTemp(filepath.Dir(f.abs), ".da-guard-*")
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

func holds(abs string, want []byte) bool {
	now, _, regular, err := readRegular(abs)
	return err == nil && regular && bytes.Equal(now, want)
}

func absent(abs string) bool {
	_, err := os.Lstat(abs)
	return errors.Is(err, fs.ErrNotExist)
}

// unchangedOnDisk: edit, replace of a file, delete — f.abs still holds
// f.original (a regular file); create, replace of a missing path — nothing is
// at f.abs; move — the source still holds the bytes judged and nothing is at
// the target.
func (w *writer) unchangedOnDisk(f *writeFile) bool {
	switch {
	case f.kind == writeKindDelete, f.group() == writeKindEdit:
		return holds(f.abs, f.original)
	case f.kind == writeKindMove:
		return holds(f.fromAbs, f.original) && absent(f.abs)
	}
	return absent(f.abs)
}

// place puts f in place; on failure the reason (changed_during_write or
// write_failed) and the error.
func (w *writer) place(f *writeFile) (string, error) {
	rename := w.rename
	if rename == nil {
		rename = os.Rename
	}
	switch {
	case f.kind == writeKindDelete:
		return w.placeDelete(f, rename)
	case f.group() == writeKindEdit:
		if !w.unchangedOnDisk(f) {
			return writeReasonChangedDuringWrite, errors.New(changedMessage(f))
		}
		if err := rename(f.tmp, f.abs); err != nil {
			return writeReasonWriteFailed, err
		}
		f.tmp = ""
		return "", nil
	case f.kind == writeKindMove:
		return w.placeMove(f, rename)
	}
	// The temporary file's inode is the file put in place, by the link or
	// the rename alike: what a roll-back may remove (rollBack).
	staged, serr := os.Lstat(f.tmp)
	if serr != nil {
		return writeReasonWriteFailed, serr
	}
	moved, reason, err := w.linkOrRename(f.tmp, f.abs, staged, rename, f)
	if err != nil {
		return reason, err
	}
	if moved {
		f.tmp = ""
	}
	f.placed = staged
	return "", nil
}

// linkOrRename puts the file want (the inode from held when checked) at from
// also at to without replacing anything there: a hard link, or — on a file
// system without hard links — a rename after checking once more that to is
// absent (moved true: from is gone). A rename moves whatever from holds by
// then, so the file it moved is checked to be want after; another one is
// renamed back and nothing is placed (changed_during_write), or, when that
// rename back fails too, left at to and named in the error.
func (w *writer) linkOrRename(from, to string, want os.FileInfo, rename func(string, string) error, f *writeFile) (moved bool, reason string, err error) {
	link := w.link
	if link == nil {
		link = os.Link
	}
	switch lerr := link(from, to); {
	case errors.Is(lerr, fs.ErrExist):
		return false, writeReasonChangedDuringWrite, errors.New(changedMessage(f))
	case lerr != nil:
		if !absent(to) {
			return false, writeReasonChangedDuringWrite, errors.New(changedMessage(f))
		}
		if rerr := rename(from, to); rerr != nil {
			return false, writeReasonWriteFailed, rerr
		}
		if cur, cerr := os.Lstat(to); cerr != nil || !os.SameFile(cur, want) {
			if berr := rename(to, from); berr != nil {
				return false, writeReasonWriteFailed, fmt.Errorf(
					"%s changed while it was moved, and could not be moved back: it is at %s (%v)", from, to, berr)
			}
			return false, writeReasonChangedDuringWrite, errors.New(changedMessage(f))
		}
		return true, "", nil
	}
	return false, "", nil
}

// placeMove links the source at the target, then removes the source name
// while it is still the file that was linked. f.placed is that file once
// the target holds it; f.sourceGone once the source name is gone.
func (w *writer) placeMove(f *writeFile, rename func(string, string) error) (string, error) {
	src, err := os.Lstat(f.fromAbs)
	if err != nil {
		return writeReasonChangedDuringWrite, errors.New(changedMessage(f))
	}
	moved, reason, err := w.linkOrRename(f.fromAbs, f.abs, src, rename, f)
	if err != nil {
		return reason, err
	}
	f.placed = src
	if moved {
		f.sourceGone = true
		return "", nil
	}
	remove := w.remove
	if remove == nil {
		remove = os.Remove
	}
	if cur, cerr := os.Lstat(f.fromAbs); cerr != nil || !os.SameFile(cur, src) {
		return writeReasonChangedDuringWrite, errors.New(changedMessage(f))
	}
	if err := remove(f.fromAbs); err != nil {
		return writeReasonWriteFailed, err
	}
	f.sourceGone = true
	return "", nil
}

// backupName is a fresh hidden name beside abs for a delete's link to its
// file (no walk reads a hidden name; temporary() names its files alike,
// and for the same reason without abs's base name: one near NAME_MAX
// could not be deleted otherwise — the file's own path is in the result).
func backupName(abs string) (string, error) {
	var b [8]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	return filepath.Join(filepath.Dir(abs), ".da-guard-deleted-"+hex.EncodeToString(b[:])), nil
}

// placeDelete removes f's file from its path so that it can be put back: it
// is first linked at a hidden name beside it (f.backup; renamed there on a
// file system without hard links), then the path's name is removed while it
// is still that file. "Still that file" is its bytes (holds: the bytes the
// judgement read, so a "tenants" precondition judged on them still holds)
// compared before the link, and its inode (os.SameFile) before the unlink:
// a file replaced by a rename is refused changed_during_write. ⚠️ The
// residual window: bytes written into the SAME inode after that byte
// comparison and before the unlink are not seen — no lock is taken. f.placed is the file once the hidden link holds it;
// f.sourceGone once the path's name is gone.
func (w *writer) placeDelete(f *writeFile, rename func(string, string) error) (string, error) {
	src, err := os.Lstat(f.abs)
	if err != nil || !holds(f.abs, f.original) {
		return writeReasonChangedDuringWrite, errors.New(changedMessage(f))
	}
	backup, err := backupName(f.abs)
	if err != nil {
		return writeReasonWriteFailed, err
	}
	moved, reason, err := w.linkOrRename(f.abs, backup, src, rename, f)
	if err != nil {
		return reason, err
	}
	f.backup, f.placed = backup, src
	if moved {
		f.sourceGone = true
		return "", nil
	}
	if cur, cerr := os.Lstat(f.abs); cerr != nil || !os.SameFile(cur, src) {
		return writeReasonChangedDuringWrite, errors.New(changedMessage(f))
	}
	remove := w.remove
	if remove == nil {
		remove = os.Remove
	}
	if err := remove(f.abs); err != nil {
		return writeReasonWriteFailed, err
	}
	f.sourceGone = true
	return "", nil
}

// restoreDeleted undoes a delete placed (rollBack): the path's name, once
// removed, is linked back from the hidden link — only while that link is
// still the file removed and the path is free, and checked to be that file
// after (os.SameFile). One that cannot be put back is maybe_written, its
// hidden link kept (the file's one name left).
func (w *writer) restoreDeleted(f *writeFile, rename func(string, string) error) {
	maybe := func(why string) {
		f.keepBackup = true
		msg := fmt.Sprintf("deleted, and not put back when the call stopped: %s; the file is kept at %s",
			why, filepath.Base(f.backup))
		fmt.Fprintf(w.errOut, "%s %s: %s: %s\n", programName, w.cmd, f.rel, msg)
		f.res.Status, f.res.Message = writeStatusMaybeWritten, strp(msg)
	}
	if !f.sourceGone {
		// The path's name was never removed: only the hidden link goes
		// (commit's deferred clean-up).
		f.res.Status, f.res.Message = writeStatusNotWritten, strp("not deleted: the call stopped first")
		return
	}
	if bfi, err := os.Lstat(f.backup); err != nil || !os.SameFile(bfi, f.placed) {
		f.keepBackup = true
		msg := "deleted, and not put back when the call stopped: its hidden link no longer holds the file removed"
		fmt.Fprintf(w.errOut, "%s %s: %s: %s\n", programName, w.cmd, f.rel, msg)
		f.res.Status, f.res.Message = writeStatusMaybeWritten, strp(msg)
		return
	}
	if _, reason, err := w.linkOrRename(f.backup, f.abs, f.placed, rename, f); err != nil {
		if reason == writeReasonChangedDuringWrite {
			maybe("the path holds another file now; it is left as it is")
		} else {
			maybe("it could not be linked back at its path: " + err.Error())
		}
		return
	}
	if cur, err := os.Lstat(f.abs); err != nil || !os.SameFile(cur, f.placed) {
		maybe("the path does not hold the file put back")
		return
	}
	f.res.Status, f.res.Message = writeStatusNotWritten, strp("deleted, and put back when the call stopped")
}

// rollBack undoes the creates and moves already in place (placed, in the
// order placed; edits in it are left: a rename is not undone): a created
// file removed, a moved one put back at its source. A path is touched only
// while it is still the file this call put there (os.SameFile against its
// inode): one another writer replaced meanwhile is left as it is. One that
// cannot be undone, or no longer can be told to be ours, is maybe_written.
func (w *writer) rollBack(placed []*writeFile) {
	remove := w.remove
	if remove == nil {
		remove = os.Remove
	}
	rename := w.rename
	if rename == nil {
		rename = os.Rename
	}
	maybe := func(f *writeFile, why string) {
		msg := "written, and not undone when the call stopped: " + why
		fmt.Fprintf(w.errOut, "%s %s: %s: %s\n", programName, w.cmd, f.rel, msg)
		f.res.Status, f.res.Message = writeStatusMaybeWritten, strp(msg)
	}
	for i := len(placed) - 1; i >= 0; i-- {
		f := placed[i]
		switch {
		case f.group() == writeKindEdit:
			continue
		case f.kind == writeKindDelete:
			w.restoreDeleted(f, rename)
			continue
		}
		now, lerr := os.Lstat(f.abs)
		switch {
		case errors.Is(lerr, fs.ErrNotExist) && f.group() == writeKindCreate:
			f.res.Status, f.res.Message = writeStatusNotWritten, strp("written, and gone again when the call stopped")
			continue
		case lerr != nil:
			maybe(f, "the path cannot be checked: "+lerr.Error())
			continue
		case f.placed == nil || !os.SameFile(now, f.placed):
			maybe(f, "the path now holds a file this call did not put there; it is left as it is")
			continue
		}
		if f.kind == writeKindMove && f.sourceGone {
			// Back at the source, without replacing what may be there now.
			if _, _, err := w.linkOrRename(f.abs, f.fromAbs, f.placed, rename, f); err != nil {
				maybe(f, "it could not be put back at its source "+f.from+": "+err.Error())
				continue
			}
			if absent(f.abs) { // put back by a rename: nothing left at the target
				f.res.Status, f.res.Message = writeStatusNotWritten, strp("moved, and moved back when the call stopped")
				continue
			}
		}
		if err := remove(f.abs); err != nil && !errors.Is(err, fs.ErrNotExist) {
			maybe(f, fmt.Sprintf("it could not be removed again: %v", err))
			continue
		}
		msg := "written, and removed again when the call stopped"
		if f.kind == writeKindMove {
			msg = "moved, and moved back when the call stopped"
		}
		f.res.Status, f.res.Message = writeStatusNotWritten, strp(msg)
	}
}
