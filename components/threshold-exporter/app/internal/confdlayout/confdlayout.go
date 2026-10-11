// Package confdlayout answers two questions about where a file sits in a
// conf.d tree as the Python route generator lays it out: which directory
// level a file belongs to (LevelOf), and whether a basename is a domain
// policy file (IsDomainPolicyFile).
//
// Neither reads a file. They were in pkg/routingpolicy (#2899), which
// ADR-036 §7 / §11 splits: its PyYAML imitation is deleted at the switch,
// so the helpers that do not parse move out first and the package can be
// split without taking them along.
//
// Why not internal/confdname: that package is restricted to the
// exporter's walker's name rules, and `_domain_policy.yaml` is a name only
// the route generator gives meaning to. Why not pkg/config: it reads the
// exporter's effective config, which has no notion of a routing level.
package confdlayout

import (
	"path"
	"strings"
)

// policyFileNames are the domain policy file names the Python reader takes
// policies from (exact spelling, case-sensitive: _grar_parse._POLICY_FILENAMES).
var policyFileNames = [...]string{"_domain_policy.yaml", "_domain_policy.yml"}

// LevelOf is the routing Tree level of a root-relative slash path to a file:
// its directory, "." for a file at the conf.d root. A backslash is read as
// a separator.
func LevelOf(rel string) string {
	return path.Dir(strings.ReplaceAll(rel, "\\", "/"))
}

// IsDomainPolicyFile reports whether name is a `_domain_policy.yaml` /
// `.yml` basename.
func IsDomainPolicyFile(name string) bool {
	for _, v := range policyFileNames {
		if v == name {
			return true
		}
	}
	return false
}
