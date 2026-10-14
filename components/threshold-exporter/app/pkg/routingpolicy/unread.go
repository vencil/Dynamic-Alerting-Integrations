package routingpolicy

import (
	"fmt"
	"path"
	"sort"

	"github.com/vencil/threshold-exporter/internal/confdlayout"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// ProblemRoutingInUnreadLocation: a `_routing` / `_routing_*` key where the
// route generator never reads it (#2291). The exporter still merges such a
// key into every tenant's effective config (a defaults block, a threshold
// profile), so a reader of the effective config sees routing no route is
// rendered from — the route generator reads only the tenant's own
// `_routing` / `_routing_profile`, the root platform files'
// `tenants.<id>` entries for those two keys, and the conf.d root's
// `_routing_defaults` / `routing_profiles` / `_routing_enforced`.
const ProblemRoutingInUnreadLocation = "routing_in_unread_location"

// rootTopLevelRoutingKeys are the `_routing*` keys the route generator reads
// at the top level of a conf.d ROOT platform file (_grar_parse.
// _parse_platform_config). An unwrapped root `_defaults.yaml` carrying them
// is therefore read correctly, whatever else the exporter does with them.
// Below the root (#2326) the carrier's `_routing_defaults` is read as well,
// and its `_routing_enforced` is refused (LoadTree).
var rootTopLevelRoutingKeys = map[string]bool{
	"_routing_defaults": true,
	"_routing_enforced": true,
}

// UnreadRouting reports the routing keys the route generator never reads
// (#2291), in two places (a key at the TOP level of any `_` file is
// LoadRoot / LoadTree's ProblemRoutingKeyIgnored since #2983):
//
//  1. inside the `defaults:` block of a defaults carrier (root or nested —
//     though a ROOT carrier whose block holds `_routing*` fails the
//     exporter's decode, so da-guard skips it as parse_failed, exit 3);
//  2. inside a profile of a root platform file's `profiles:` block (the
//     threshold `_profile` mechanism; routing profiles are
//     `_routing_profiles.yaml`'s `routing_profiles:`).
//
// defaults are the carriers the chains read (config.ScopedTenants.
// DefaultsFiles); the root platform files are read from configDir. skip
// (may be nil) receives each file's root-relative path; true leaves it out —
// da-guard passes the files the exporter drops (exit 3), so a broken file is
// named once. A file that does not decode is left out as well.
func UnreadRouting(configDir string, defaults []config.DefaultsFile, skip func(rel string) bool) []Problem {
	var out []Problem
	for _, f := range defaults {
		if skip != nil && skip(f.Name) {
			continue
		}
		out = append(out, unreadInDefaults(f.Name, f.Data)...)
	}
	files, err := config.RootPlatformFiles(configDir)
	if err != nil {
		return out // LoadRoot names an unreadable root
	}
	for _, f := range files {
		if skip != nil && skip(f.Name) {
			continue
		}
		out = append(out, unreadInProfiles(f.Name, f.Data)...)
	}
	return out
}

// topMap decodes one document's top level; nil when it is not a mapping or
// does not decode. A `_domain_policy.yaml` / `.yml` is decoded as a policy
// document, as loadRoot / LoadTree decode it (#2325): its `!!null x` is read
// like the generator reads it instead of failing the file, and a value
// PyYAML refuses (`!!null {}`) fails it — the generator drops that file, so
// none of it is reported as unread.
func topMap(name string, data []byte) map[string]any {
	top, err := parseDoc(data, confdlayout.IsDomainPolicyFile(path.Base(name)))
	if err != nil || top == nil {
		return nil
	}
	var v any
	if top.Decode(&v) != nil {
		return nil
	}
	// #2904: keys as the exporter reads them (config.ExtractDefaultsBlock
	// runs over NormalizeYAMLToJSON, which re-spells a non-string key instead
	// of refusing the map; a threshold profile is decoded string-keyed the
	// same way) — and as the route generator's loader does (raw key text).
	// Without it asStringMap called `defaults: {1: 5}` no mapping, so a
	// top-level `_routing` beside it was reported although the exporter takes
	// the `defaults:` block and merges nothing else; and one non-string key
	// in a top level, a profiles block or a profile hid the `_routing` the
	// exporter does merge. After this every mapping below is string-keyed.
	m, _ := asStringMap(config.NormalizeYAMLToJSON(v))
	return m
}

func unreadInDefaults(name string, data []byte) []Problem {
	doc := topMap(name, data)
	if doc == nil {
		return nil
	}
	// The exporter's own rule (config.ExtractDefaultsBlock over
	// NormalizeYAMLToJSON): a `defaults:` mapping — any keys — is the block,
	// anything else makes the document the block.
	if block, wrapped := asStringMap(doc["defaults"]); wrapped {
		var out []Problem
		for _, k := range sortedRoutingKeys(block) {
			out = append(out, unreadProblem(name, "defaults."+k, k,
				"a defaults block is merged into every tenant's effective config, but the route generator never reads routing from it"))
		}
		return out
	}
	// #2983: a carrier with no `defaults:` mapping — the exporter takes the
	// whole document as the block — has its top-level `_routing*` keys named
	// by LoadRoot / LoadTree instead (unknownPlatformKeyProblems): every key
	// there but `_routing_defaults` / `_routing_enforced`, in any `_` file,
	// once.
	return nil
}

func unreadInProfiles(name string, data []byte) []Problem {
	doc := topMap(name, data)
	profiles, ok := asStringMap(doc["profiles"])
	if !ok {
		return nil
	}
	names := make([]string, 0, len(profiles))
	for n := range profiles {
		names = append(names, n)
	}
	sort.Strings(names)
	var out []Problem
	for _, pn := range names {
		body, ok := asStringMap(profiles[pn])
		if !ok {
			continue
		}
		for _, k := range sortedRoutingKeys(body) {
			out = append(out, unreadProblem(name, "profiles."+pn+"."+k, k,
				fmt.Sprintf("a threshold profile fills its keys into the tenants on `_profile: %s`, but the route generator never reads routing from it", pn)))
		}
	}
	return out
}

func sortedRoutingKeys(m map[string]any) []string {
	var out []string
	for k := range m {
		if config.IsRoutingKey(k) {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

// unreadProblem builds the finding for one key, with where the key belongs.
func unreadProblem(file, field, key, why string) Problem {
	var fix string
	switch {
	case key == "_routing":
		fix = "move it to `_routing_defaults` at the top level of a conf.d root platform file (every tenant), " +
			"a routing profile in `_routing_profiles.yaml` referenced by `_routing_profile` (a group), " +
			"or the tenant's own file (one tenant)"
	case key == "_routing_profile":
		fix = "set `_routing_profile` in the tenant's own file (or its root platform `tenants:` entry)"
	case key == "_routing_enforced":
		fix = "move `_routing_enforced` to the top level of a conf.d ROOT platform file (not under `defaults:`, not below the root)"
	case rootTopLevelRoutingKeys[key]:
		fix = fmt.Sprintf("move `%s` to the top level of this defaults file (not under `defaults:`)", key)
	default:
		fix = "the route generator reads no such key here; move the routing to `_routing_defaults`, " +
			"`_routing_profiles.yaml`, or the tenant's own `_routing`"
	}
	return Problem{
		Kind:    ProblemRoutingInUnreadLocation,
		File:    file,
		Field:   field,
		Message: fmt.Sprintf("%s: %s is not rendered — %s; %s", file, field, why, fix),
	}
}
