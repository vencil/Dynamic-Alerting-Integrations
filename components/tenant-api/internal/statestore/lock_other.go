//go:build !unix

package statestore

// lockDir is a no-op where flock does not exist: the process mutex is then
// the only serialisation, and running `tenant-api state restore` beside a
// live server on such a host is not safe.
func lockDir(string) (func(), error) { return func() {}, nil }
