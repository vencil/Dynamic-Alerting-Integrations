package config

// Role is what one path the walk recorded IS, for every reader of the tree:
// a config file (which a reader reads, and on which its read may fail), a
// path where the exporter's walk cannot reach a regular file, or a directory
// whose contents the walk could not see. It is the one verdict on that question: the walker
// records the facts (TreeScan.Unreadable, TreeScan.NotFiles,
// TreeScan.RootWalkErr), these methods judge them, and `da-guard
// parse-tree` writes the judgement into its document ("role") so that no
// reader composes one of its own from the facts (ADR-036 §7, #3206).
//
// ⛔ A WHITELIST. Only what shows the exporter's walk cannot reach a
// regular file at a path makes it RoleNotAFile; every other failure — a
// stat refused by an unsearchable directory on the way (EACCES), an open
// refused, EIO, ENAMETOOLONG, a reason added later that nobody named here —
// is RoleConfig: a config file whose
// readers fail on it, loudly. Twice a default that leaned the other way let
// a tenant file vanish behind one WARN line.
type Role string

// Closed set of Role values.
const (
	// RoleConfig: a config file. Every file the walk keeps (TreeScan.Files)
	// is one, read or not (an unselected defaults carrier is a config file
	// no reader reads); so is every unreadable entry not shown to be
	// something else.
	RoleConfig Role = "config"
	// RoleNotAFile: no regular file the exporter's walk can reach — a
	// config-named directory, a symlink to a directory, a special file (a
	// FIFO, a socket, a device, or a symlink to one), a stat that found
	// nothing there (a dangling link, a looping link, a chain of more than 40
	// links: UnreadableFile.Missing).
	RoleNotAFile Role = "not_a_file"
	// RoleUnlistableDir: a directory (the root included) whose contents the
	// walk could not list. Nothing under it was seen, so a listing that holds
	// one is incomplete.
	RoleUnlistableDir Role = "unlistable_dir"
)

// Roles is the closed set, in a fixed order.
func Roles() []Role { return []Role{RoleConfig, RoleNotAFile, RoleUnlistableDir} }

// Role is the verdict on one unreadable walk entry (see Role).
func (u UnreadableFile) Role() Role {
	switch u.Reason {
	case UnreadableSpecialFile:
		return RoleNotAFile
	case UnreadableWalkError:
		return RoleUnlistableDir
	case UnreadableStatError:
		if u.Missing {
			return RoleNotAFile
		}
	}
	return RoleConfig
}

// Role is the verdict on a config-named path that is not a file (both
// NotFile reasons are directories, or links to one).
func (NotFile) Role() Role { return RoleNotAFile }

// RootWalkRole is the verdict on the root when it could not be listed
// (TreeScan.RootWalkErr non-nil).
const RootWalkRole = RoleUnlistableDir
