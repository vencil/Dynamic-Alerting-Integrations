package config

import "os"

// ConfDFileMode is the mode of every file written into a conf.d tree — by
// da-guard (create, copy, replace, edit; a move renames the same file and
// writes nothing) and by tenant-api (#3535,
// owner ruling 2026-11-03). A conf.d file is a repo artifact: it is committed
// (git records 100644 / 100755 only, so a 0600 never survives a checkout)
// and read by another uid (the exporter beside tenant-api in try-local, a
// shared volume), so it is neither kept from the file it replaces nor
// tightened to an operator output's 0600. Credentials in a tenant file are
// not guarded by a mode (the file is committed and rendered into a
// ConfigMap); that is #1560's.
//
// Set with chmod on the written file, never as an open(2) mode: the umask
// does not narrow it. A file whose text is unchanged is not written, so a
// file an earlier version wrote 0600 keeps that mode until its text changes.
//
// ⛔ Python's repo-artifact writer has the same rule:
// scripts/tools/_lib_atomic_write.py `_DEFAULT_MODE`. The two are pinned
// together by tests/shared/test_confd_file_mode_parity.py.
const ConfDFileMode os.FileMode = 0o644

// ConfDDirMode is the mode of a directory da-guard makes in a conf.d tree for
// a file it writes (#3535): readable and enterable by another uid, set with
// chmod after mkdir so the umask does not narrow it (a 0700 directory would
// hide the 0644 file inside it).
const ConfDDirMode os.FileMode = 0o755
