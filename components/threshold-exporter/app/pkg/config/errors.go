package config

import "fmt"

// DecodeError is a resolve failure caused by one file's bytes failing decode
// during resolve (chainParseError / tenantParseError; #2123). It exists
// because `_`-prefixed files are never decoded by the walker, so
// TreeFile.ParseFailed cannot name a broken defaults file.
//
// Error() is Err's text unchanged — `parse defaults[i]: …` / `parse tenant:
// …`, which package main maps back to a file by that text — so wrapping
// changes no message; Path is the file relative to the scan root, slash-
// separated. Detect with errors.As; Unwrap keeps the yaml error reachable.
type DecodeError struct {
	Path string
	Err  error
}

func (e *DecodeError) Error() string { return e.Err.Error() }
func (e *DecodeError) Unwrap() error { return e.Err }

// chainParseError / tenantParseError carry which input of a byte-level merge
// failed to decode, so the resolver (which knows the paths) can build a
// DecodeError. Their text is the historical fmt.Errorf wording, verbatim.
type chainParseError struct {
	index int
	err   error
}

func (e *chainParseError) Error() string {
	return fmt.Sprintf("parse defaults[%d]: %s", e.index, e.err.Error())
}
func (e *chainParseError) Unwrap() error { return e.err }

type tenantParseError struct{ err error }

func (e *tenantParseError) Error() string { return "parse tenant: " + e.err.Error() }
func (e *tenantParseError) Unwrap() error { return e.err }

// DuplicateTenantError signals that the same tenant ID was discovered in two
// different files during a directory scan. This is a misconfig (e.g. forgot
// to delete the old flat copy after `git mv` to the nested layout) that the
// platform should reject hard rather than silently last-wins-merge.
//
// Produced by the one disk walker, ScanDirTree (tree_scan.go): recorded as
// TreeScan.Conflict (the exporter's whole-tree verdict; package main sees
// the same type through the `type DuplicateTenantError =
// config.DuplicateTenantError` alias in config_types.go) and, per tenant,
// returned by TreeScan.Locate — which is how ResolveEffective (hierarchy.go)
// and ScopeEffective (scope.go) surface it. ScanFromConfigSource
// (source.go), the in-memory scanner, returns it too. All consumers detect
// it via `errors.As(err, &DuplicateTenantError{})` (issue #127, v2.8.x
// hardening).
//
// Before v2.8.x: the exporter's hierarchical scanner (since replaced by
// ScanDirTree) returned a generic fmt.Errorf, and Load() swallowed it with
// a WARN log. Customers could deploy
// with a duplicate tenant silently merged via map last-wins iteration — easy
// to miss in production.
//
// After v2.8.x: the typed error lets Load() / fullDirLoad() reject the
// misconfig at the boundary; other scan errors (permissions, malformed file)
// keep the log-and-continue policy because hierarchical mode is opt-in and
// shouldn't tear down a flat-only deploy.
//
// Lowered into pkg/config (candidate C6-A, #127 library-side gap): the walkers
// used by library consumers (tenant-api, cmd/da-guard, simulate) previously
// returned a stringly fmt.Errorf, so those consumers could only string-match
// the message. Owning the type here lets them unpack the same typed error the
// exporter already does.
type DuplicateTenantError struct {
	TenantID string
	PathA    string // First-discovered file
	PathB    string // Second-discovered file (the one rejected)
}

func (e *DuplicateTenantError) Error() string {
	return fmt.Sprintf("duplicate tenant ID %q: defined in both %s and %s", e.TenantID, e.PathA, e.PathB)
}
