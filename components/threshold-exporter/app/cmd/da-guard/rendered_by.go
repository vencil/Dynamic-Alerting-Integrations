package main

import (
	"path"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/internal/confdname"
	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// renderedByKeys are the platform carriers rendered_by answers for (#2962):
// the top-level keys of a `_` file a render can be changed by. Any other key
// is not modelled and is left out of the file's map — absent means "not
// answered here", never "nothing reads it". max_metrics_per_tenant is not
// in it: no render shows the cap itself.
var renderedByKeys = []string{
	"defaults", "state_filters", "optional_overrides", "profiles", "tenants",
	"_routing_defaults", "_routing_enforced", "routing_profiles",
}

// routingProfilesNames: the generator takes routing_profiles from these
// names only, exact spelling (_grar_parse._parse_profiles_and_policies).
var routingProfilesNames = map[string]bool{"_routing_profiles.yaml": true, "_routing_profiles.yml": true}

// renderedBy is one `_` file's rendered_by entry: for each modelled key the
// file carries, the readers whose render a change to it can alter (#2962).
// carrier: the file is its directory's selected defaults carrier.
//
// The rules (exporter = the served values, generator = the routes):
//   - root carrier: defaults / state_filters / optional_overrides → exporter
//     (flat_build applyBoundaryRules: only a carrier's are honoured);
//   - a directory's carrier: defaults → exporter (the chain reads only its
//     `defaults:` block, hierarchy.ExtractDefaultsBlock);
//   - root `_` file: profiles → exporter (nested `_` files are dropped from
//     the flat merge whole); tenants → exporter and generator (the overlay,
//     and the generator's `tenants.<id>._routing` / `_routing_profile`);
//     _routing_enforced → generator; _routing_defaults → generator;
//   - a directory's carrier: _routing_defaults → generator (#2326);
//   - routing_profiles → generator in `_routing_profiles.yaml` / `.yml` at
//     any level;
//   - anything else: no reader ([]).
//
// tests/shared/test_parse_tree_rendered_by.py holds the rows to the renders
// themselves — each carrier changed in the root carrier, another root `_`
// file, a directory's carrier and another `_` file below the root — and the
// views that changed must be exactly these readers'. Two tables agreeing with
// each other is not the oracle.
func renderedBy(rel string, carrier bool, content parsetree.Content) map[string][]string {
	base := path.Base(rel)
	if !confdname.IsReserved(base) {
		return nil
	}
	root := !strings.Contains(rel, "/")
	out := map[string][]string{}
	for _, key := range renderedByKeys {
		if _, has := content[key]; !has {
			continue
		}
		var readers []string
		switch key {
		case "defaults":
			if carrier {
				readers = []string{readerExporter}
			}
		case "state_filters", "optional_overrides":
			if carrier && root {
				readers = []string{readerExporter}
			}
		case "profiles":
			if root {
				readers = []string{readerExporter}
			}
		case "tenants":
			if root {
				readers = []string{readerExporter, readerGenerator}
			}
		case "_routing_enforced":
			if root {
				readers = []string{readerGenerator}
			}
		case "_routing_defaults":
			if root || carrier {
				readers = []string{readerGenerator}
			}
		case "routing_profiles":
			if routingProfilesNames[base] {
				readers = []string{readerGenerator}
			}
		}
		if readers == nil {
			readers = []string{}
		}
		sort.Strings(readers)
		out[key] = readers
	}
	return out
}
