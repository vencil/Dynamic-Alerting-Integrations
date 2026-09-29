package routingpolicy

import (
	"fmt"
	"sort"
	"strings"

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
var rootTopLevelRoutingKeys = map[string]bool{
	"_routing_defaults": true,
	"_routing_enforced": true,
}

func isRoutingKey(k string) bool {
	return k == "_routing" || strings.HasPrefix(k, "_routing_")
}

// UnreadRouting reports the routing keys the route generator never reads
// (#2291), in three places:
//
//  1. inside the `defaults:` block of a defaults carrier (root or nested —
//     though a ROOT carrier whose block holds `_routing*` fails the
//     exporter's decode, so da-guard skips it as parse_failed, exit 3);
//  2. at the top level of a defaults carrier with no `defaults:` mapping —
//     the exporter then takes the whole document as the defaults block —
//     except `_routing_defaults` / `_routing_enforced` in the ROOT carrier,
//     which the generator reads there;
//  3. inside a profile of a root platform file's `profiles:` block (the
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
		out = append(out, unreadInDefaults(f.Name, f.Data, !strings.Contains(f.Name, "/"))...)
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
// does not decode.
func topMap(data []byte) map[string]any {
	top, err := parseDoc(data, false)
	if err != nil || top == nil {
		return nil
	}
	var v any
	if top.Decode(&v) != nil {
		return nil
	}
	m, _ := asStringMap(v)
	return m
}

func unreadInDefaults(name string, data []byte, atRoot bool) []Problem {
	doc := topMap(data)
	if doc == nil {
		return nil
	}
	// The exporter's own rule (config.ExtractDefaultsBlock): a `defaults:`
	// mapping is the block, anything else makes the document the block.
	if block, wrapped := asStringMap(doc["defaults"]); wrapped {
		var out []Problem
		for _, k := range sortedRoutingKeys(block) {
			out = append(out, unreadProblem(name, "defaults."+k, k,
				"a defaults block is merged into every tenant's effective config, but the route generator never reads routing from it"))
		}
		return out
	}
	var out []Problem
	for _, k := range sortedRoutingKeys(doc) {
		if atRoot && rootTopLevelRoutingKeys[k] {
			continue
		}
		out = append(out, unreadProblem(name, k, k,
			"this file has no `defaults:` block, so its whole top level is merged into every tenant's effective config as defaults, but the route generator never reads routing from it"))
	}
	return out
}

func unreadInProfiles(name string, data []byte) []Problem {
	doc := topMap(data)
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
		if isRoutingKey(k) {
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
	case rootTopLevelRoutingKeys[key]:
		fix = fmt.Sprintf("move `%s` to the top level of a conf.d ROOT platform file (not under `defaults:`, not below the root)", key)
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
