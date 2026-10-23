//go:build !unix

package main

import "os"

// openNoBlock opens abs for reading (no FIFOs to block on here); the caller
// fstats the handle and reads only a regular file.
func openNoBlock(abs string) (*os.File, error) {
	return os.Open(abs) // #nosec G304 -- inside --config-dir
}

// ownerOf: file ownership is not carried on this platform.
func ownerOf(os.FileInfo) (uid, gid int, ok bool) { return 0, 0, false }
