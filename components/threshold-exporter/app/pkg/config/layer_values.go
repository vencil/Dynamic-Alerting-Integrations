package config

// Reserved values judged where they are written (#3591, ADR-038 decision
// 10): the shared key verdict's reading of the values the exporter does not
// read.
//
// ⛔ PER FILE AND LAYER, NEVER ON A MERGED VALUE. The flat config's tenant
// maps already hold what a root platform file's `tenants.<id>` entry and the
// elected profile fill in, so a check on cfg.Tenants (what
// ValidateTenantKeysByTenant judges) charges one profile's typo to every
// tenant that binds it, and one platform entry's typo to the tenant as if
// its own file wrote it — and would block every write to those tenants for
// a value none of them can fix in its own file. Measured before this file:
// a profile `_silent_mode: purple` was a value finding on each tenant
// binding the profile. So each value is judged in the partial the flat
// build decoded from the file that writes it (FlatBuild.FileConfigs, as
// ProfileRoutingKeys reads it), in the layer it is written in.

import (
	"fmt"
	"sort"
	"strings"
)

// The registry layers LayerValueProblems judges values in.
const (
	ValueLayerTenant   = "tenant"
	ValueLayerPlatform = "platform_tenants"
	ValueLayerProfile  = "profile"
)

// LayerValueProblem is one reserved value the exporter does not read, in
// the file and layer that writes it.
type LayerValueProblem struct {
	// File: the scan key (root-relative slash path) of the file.
	File string
	// Layer: ValueLayerTenant (a tenant file's `tenants.<id>`),
	// ValueLayerPlatform (a root platform file's `tenants.<id>`) or
	// ValueLayerProfile (a root platform file's `profiles.<name>`).
	Layer string
	// Owner: the tenant id, or for ValueLayerProfile the profile name.
	Owner string
	// Key: as the file spells it.
	Key string
	// Problem: the reader's tenant-free reason (CheckReservedValue, or
	// unknownProfileProblem for `_profile`).
	//
	// ⛔ CONSTANTS ONLY (#3703). It is the key verdict's `reason`, which the
	// CI readers print as it is (the route generator's `--validate` and
	// `--findings-json`, validate-config): a sentence built from the
	// platform's own words, an index or a count — never the value, a field
	// name the author wrote, or a parser's error (which quotes them). The
	// finding names the file, the layer, the owner and the key; Reading
	// carries the value for the machine.
	// TestLayerValueProblems_CarryNoAuthorText guards it.
	Problem string
	// Value: the value as the file wrote it (its `default` for a schedule,
	// a list or mapping as the decode re-serialised it), for a reader that
	// shows the value apart from Problem's own quoting of it (#3621).
	Value string
	// Reading: the value as the reader reads it (ValueReading) — two values
	// whose readings are equal are the same unread value, so a write that
	// swaps one unread value for another makes a new one (the Ratchet's
	// rule, readAlike).
	Reading string
	// RefersToRoot: the problem is a name the root platform files do not
	// define (`_profile`) — judged only when a root platform file was read.
	RefersToRoot bool
	// Schedule (#3834): the problem is not the value but the schedule it is
	// written with (scheduledReserved, #3269) — LayerScheduleProblems'.
	// Reading is then the schedule as written (ScheduleReading), not a
	// reader's reading of the `default`.
	Schedule bool
}

// LayerValueProblems is every reserved value of built's files the exporter
// does not read, in a layer the exporter reads the key in (a key the
// registry declares no exporter layer for there is the verdict's unread
// finding, not a value one; a `_state_<filter>` whose filter the root does
// not declare is both — its value is judged as the write gate's
// KeyValidation judges it, whatever the declaration), sorted by
// file, layer, owner and key. The files are the ones the flat merge reads:
// the root platform files (an unselected root carrier is not among them, a
// nested `_` file contributes nothing) and the tenant files. A tenant entry
// is judged whether or not the tenant exists; the verdict keeps the tenants
// it judges.
func LayerValueProblems(scan *TreeScan, built *FlatBuild) []LayerValueProblem {
	if built == nil {
		return nil
	}
	profiles := built.Config.Profiles
	var out []LayerValueProblem
	eachLayerValues(scan, built, func(file, layer, owner string, values map[string]ScheduledValue, _ ThresholdConfig) {
		for key, sv := range values {
			if p := layerValueProblem(layer, key, sv, values, profiles); p != "" {
				canon, _ := canonicalKeyFor(key)
				out = append(out, LayerValueProblem{File: file, Layer: layer, Owner: owner, Key: key, Problem: p,
					Value:   sv.Default,
					Reading: ValueReading(canon, sv), RefersToRoot: canon == "_profile"})
			}
		}
	})
	sortLayerValueProblems(out)
	return out
}

// LayerScheduleProblems (#3834) is every reserved value of built's files
// written with a schedule its readers ignore (scheduledReserved, #3269:
// they read the `default` alone), in the file and layer that writes it —
// LayerValueProblems' files and layers, sorted alike. A key judged is one
// some reader reads in that layer (someReaderReadsIn) — not the exporter
// alone: `_routing_profile` (the generator's) and `_namespaces` (the
// annotation's) read the `default` alone too. A key no reader reads there
// is the verdict's unread finding, not this one. Problem is
// scheduleProblem, the write gate's (newScheduleFinding); Reading the
// schedule as written (ScheduleReading).
//
// The decode keeps only each such value's `default` (stripScheduledReserved),
// so the schedule is the one recorded beside the file's partial
// (ScheduledReservedEntry), not the partial's value.
func LayerScheduleProblems(scan *TreeScan, built *FlatBuild) []LayerValueProblem {
	if built == nil {
		return nil
	}
	var out []LayerValueProblem
	eachLayerValues(scan, built, func(file, layer, owner string, values map[string]ScheduledValue, fc ThresholdConfig) {
		if fc.notes == nil {
			return
		}
		section := "tenants"
		if layer == ValueLayerProfile {
			section = "profiles"
		}
		for _, e := range fc.notes.scheduledReserved {
			if e.Section != section || e.Owner != owner {
				continue
			}
			if !someReaderReadsIn(layer, e.Key, values) {
				continue
			}
			out = append(out, LayerValueProblem{File: file, Layer: layer, Owner: owner, Key: e.Key,
				Problem: scheduleProblem, Value: e.written.Default, Reading: ScheduleReading(e.written),
				Schedule: true})
		}
	})
	sortLayerValueProblems(out)
	return out
}

// eachLayerValues calls fn for every `tenants:` / `profiles:` entry of the
// files the flat merge reads, with the layer it is written in: a root
// platform file's `tenants.<id>` (ValueLayerPlatform) and `profiles.<name>`
// (ValueLayerProfile), a tenant file's `tenants.<id>` (ValueLayerTenant).
// An unselected root carrier is not among them; a nested `_` file
// contributes nothing. fc is the file's partial.
func eachLayerValues(scan *TreeScan, built *FlatBuild,
	fn func(file, layer, owner string, values map[string]ScheduledValue, fc ThresholdConfig)) {
	if scan == nil || built == nil {
		return
	}
	carrier := selectedRootCarrierKey(scan)
	for file, fc := range built.FileConfigs {
		switch {
		case isRootPlatformKey(file, carrier):
			for tid, values := range fc.Tenants {
				fn(file, ValueLayerPlatform, tid, values, fc)
			}
			for name, values := range fc.Profiles {
				fn(file, ValueLayerProfile, name, values, fc)
			}
		case !isPlatformKey(file):
			for tid, values := range fc.Tenants {
				fn(file, ValueLayerTenant, tid, values, fc)
			}
		}
	}
}

// sortLayerValueProblems sorts out by file, layer, owner and key.
func sortLayerValueProblems(out []LayerValueProblem) {
	sort.Slice(out, func(i, j int) bool {
		a, b := out[i], out[j]
		if a.File != b.File {
			return a.File < b.File
		}
		if a.Layer != b.Layer {
			return a.Layer < b.Layer
		}
		if a.Owner != b.Owner {
			return a.Owner < b.Owner
		}
		return a.Key < b.Key
	})
}

// ScheduleReading is sv as written, schedule included, as text: two
// schedules whose readings are equal are the same value (sameSchedule), so
// a write that moves or adds a window makes a new one.
func ScheduleReading(sv ScheduledValue) string {
	var b strings.Builder
	fmt.Fprintf(&b, "default=%q structured=%t", sv.Default, sv.structured)
	for _, o := range sv.Overrides {
		fmt.Fprintf(&b, " window=%q value=%q", o.Window, o.Value)
	}
	if sv.Expiry != nil {
		fmt.Fprintf(&b, " expires=%q reason=%q", sv.Expiry.Expires, sv.Expiry.Reason)
	}
	return b.String()
}

// ValueReading is sv, the value of reserved key k, as its reader reads it,
// as text: the reading k's `value.check` is bound to, `_profile`'s name, or
// the value as written when k has neither.
func ValueReading(k string, sv ScheduledValue) string {
	if k == "_profile" {
		return strings.TrimSpace(sv.Default)
	}
	if c, ok := reservedCheck(k); ok && c.reading != nil {
		return fmt.Sprintf("%v", c.reading(k, sv))
	}
	return sv.Default
}

// exporterReadsIn reports whether the exporter reads reserved key key,
// written in layer beside the other keys of its map (values), and its
// canonical spelling: not for a threshold key, a key the registry declares
// no exporter reader for in layer, or a deprecated spelling its canonical
// sibling overrides (resolve ignores it).
func exporterReadsIn(layer, key string, values map[string]ScheduledValue) (string, bool) {
	if !strings.HasPrefix(key, "_") {
		return "", false
	}
	canon, wasAlias := canonicalKeyFor(key)
	if wasAlias {
		if _, both := values[canon]; both {
			return "", false
		}
	}
	readers, _ := RegisteredReaders(canon)
	return canon, containsString(readers["exporter"], layer)
}

// someReaderReadsIn reports whether some reader the registry declares reads
// reserved key key in layer, written beside the other keys of its map
// (values) — exporterReadsIn for any reader.
func someReaderReadsIn(layer, key string, values map[string]ScheduledValue) bool {
	if !strings.HasPrefix(key, "_") {
		return false
	}
	canon, wasAlias := canonicalKeyFor(key)
	if wasAlias {
		if _, both := values[canon]; both {
			return false
		}
	}
	readers, _ := RegisteredReaders(canon)
	for _, layers := range readers {
		if containsString(layers, layer) {
			return true
		}
	}
	return false
}

// layerValueProblem is the problem of key's value sv, written in layer
// beside the other keys of its map (values): "" for a threshold key, a key
// the exporter does not read in layer, a deprecated spelling its canonical
// sibling overrides (resolve ignores it), or a value the exporter reads.
func layerValueProblem(layer, key string, sv ScheduledValue, values map[string]ScheduledValue,
	profiles map[string]map[string]ScheduledValue) string {
	canon, ok := exporterReadsIn(layer, key, values)
	if !ok {
		return ""
	}
	if canon == "_profile" {
		return unknownProfileProblem(profiles, sv)
	}
	return CheckReservedValue(canon, sv)
}
