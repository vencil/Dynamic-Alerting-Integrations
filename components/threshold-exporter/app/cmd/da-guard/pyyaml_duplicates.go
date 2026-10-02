package main

import (
	"fmt"
	"io"
	"os"
	"path"
	"path/filepath"
	"sort"

	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/pyyamlcompat"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
)

// withGeneratorDuplicates adds to scoped.ParseFailed every file in scope that
// the route generator refuses for a repeated mapping key the exporter's
// yaml.v3 lets through (#2295): an alias key beside its anchor, two `<<`.
// The generator's StrictLoader refuses the WHOLE file
// (pyyamlcompat.FindDuplicateKey), so such a file takes the path a plain
// repeated key takes — the exporter's decode rejects that one and the file
// lands in ParseFailed, exit 3 — and its tenants are dropped as the walker
// drops a file it cannot decode.
//
// Files: the tenant files that declared in-scope tenants, the defaults
// carriers bearing on the scope, the root platform files and the other `_`
// files below the root bearing on the scope (#2439: the exporter reads
// none of those, so its decode no longer rejects them for us). Not the root
// `_domain_policy` / `_routing_profiles` files, nor a nested
// `_domain_policy`: the routing loader names one it cannot parse as a
// Problem, a repeated key included (routingpolicy.ReportsUnusable). A
// nested `_routing_profiles` stays here: its Problem is only a warning,
// and before #2439 the exporter's decode made its repeated key exit 3.
//
// Each file found is named on errOut with the key, since the report's
// parse-failure block is worded for the exporter's decode.
func withGeneratorDuplicates(configDir string, scoped *config.ScopedTenants, errOut io.Writer) {
	failed := make(map[string]bool, len(scoped.ParseFailed))
	for _, pf := range scoped.ParseFailed {
		failed[pf] = true
	}
	dup := map[string]bool{}
	check := func(name string, data []byte) {
		if failed[name] || dup[name] {
			return
		}
		if d := pyyamlcompat.FindDuplicateKeyIn(data); d != nil {
			dup[name] = true
			fmt.Fprintf(errOut, "%s: %s: %v\n", programName, name, d)
		}
	}

	root := config.AbsScanRoot(configDir)
	for _, name := range scoped.SourceFiles {
		// Unreadable now: the walker read it a moment ago; whatever reads it
		// next reports the error (pyRouting leaves it nil: fail-closed).
		if data, err := os.ReadFile(filepath.Join(root, filepath.FromSlash(name))); err == nil {
			check(name, data)
		}
	}
	for _, f := range scoped.DefaultsFiles {
		check(f.Name, f.Data)
	}
	if files, err := config.RootPlatformFiles(configDir); err == nil {
		// An error here is reported by routingpolicy.LoadRoot, which reads
		// the same list.
		for _, f := range files {
			if !routingpolicy.ReportsUnusable(f.Name) {
				check(f.Name, f.Data)
			}
		}
	}
	for _, f := range scoped.NestedPlatformFiles {
		if !routingpolicy.IsDomainPolicyFile(path.Base(f.Name)) {
			check(f.Name, f.Data)
		}
	}
	if len(dup) == 0 {
		return
	}

	for name := range dup {
		scoped.ParseFailed = append(scoped.ParseFailed, name)
	}
	sort.Strings(scoped.ParseFailed)
	tenants := scoped.Tenants[:0]
	for _, ec := range scoped.Tenants {
		if !dup[ec.SourceFile] {
			tenants = append(tenants, ec)
		}
	}
	scoped.Tenants = tenants
	sources := scoped.SourceFiles[:0]
	for _, s := range scoped.SourceFiles {
		if !dup[s] {
			sources = append(sources, s)
		}
	}
	scoped.SourceFiles = sources
}
