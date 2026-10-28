package guard

// Reserved keys in subtree defaults (#2388, #3341).
//
// A subtree `_defaults.yaml` (not the conf.d root's) whose defaults carry a
// reserved key — `_state_*`, `_silent_mode`, `_severity_dedup`, `_metadata`,
// … (not the routing keys `_routing` / `_routing_<…>`, which the routing
// checks report) — or a `_silent_*` / `_state_*` key nothing reads is an
// operator configuration error: subtree defaults do not carry these keys, and
// the exporter's subtree overlay applies none of them, whatever the value
// (#3341; until then it applied a `disable` or a number of some of them and
// dropped the rest). An error: the value the file asks for is not in effect.
//
// ⛔ NO VERDICT OF ITS OWN, as checkSubtreeUndeliverable: which keys count is
// pkg/config's subtreeReservedUnsupported — the predicate the overlay skips
// them by — over the defaults chain the exporter's build reads
// (subtreeReservedKeys), handed in as CheckInput.SubtreeReservedKeys.

import (
	"fmt"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// checkSubtreeReservedKeys reports one findingkind.FindingSubtreeDefaultReservedKey
// error per (tenant, key) of input.SubtreeReservedKeys, for the tenants in
// input.EffectiveConfigs only (the caller's scope).
func checkSubtreeReservedKeys(input CheckInput) []Finding {
	if len(input.SubtreeReservedKeys) == 0 {
		return nil
	}
	tenants := make([]string, 0, len(input.SubtreeReservedKeys))
	for id := range input.SubtreeReservedKeys {
		if _, inScope := input.EffectiveConfigs[id]; inScope {
			tenants = append(tenants, id)
		}
	}
	sort.Strings(tenants)
	failed := make(map[string]bool, len(input.ParseFailed))
	for _, pf := range input.ParseFailed {
		failed[pf] = true
	}
	unwrapped := unwrappedDefaultsFiles(input, failed)
	var out []Finding
	for _, id := range tenants {
		byKey := input.SubtreeReservedKeys[id]
		keys := make([]string, 0, len(byKey))
		for k := range byKey {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			// A file the exporter drops (ParseFailed — since ADR-036 step 6,
			// #3206, every file parse-tree refuses) applies nothing, so it
			// writes no reserved key: skipped, as checkDefaultsWrapper does.
			writes := withoutFailed(byKey[k], failed)
			if len(writes) == 0 {
				continue
			}
			if readElsewhere[k] {
				writes = writtenInsideDefaults(writes, unwrapped)
				if len(writes) == 0 {
					continue
				}
			}
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     findingkind.FindingSubtreeDefaultReservedKey,
				TenantID: id,
				Field:    k,
				Message:  subtreeReservedMessage(input.DeclaredStateFilters, k, writes),
			})
		}
	}
	return out
}

// unwrappedDefaultsFiles is the set of input.DefaultsFiles (root-relative
// names) with no `defaults:` mapping: the defaults merge reads the whole
// document, so a key in pkg/config's chain view of such a file sits at its
// TOP level. A file in failed (ParseFailed) is skipped, as checkDefaultsWrapper
// skips it: the exporter reads none of it.
func unwrappedDefaultsFiles(input CheckInput, failed map[string]bool) map[string]bool {
	out := map[string]bool{}
	for _, f := range input.DefaultsFiles {
		if failed[f.Name] {
			continue
		}
		if doc := decodeDefaultsDoc(f.Data); doc != nil && !wrappedInMapping(doc) {
			out[f.Name] = true
		}
	}
	return out
}

// withoutFailed is writes minus the ones whose file is in failed; writes
// itself when none is.
func withoutFailed(writes []config.SubtreeReservedWrite, failed map[string]bool) []config.SubtreeReservedWrite {
	if len(failed) == 0 {
		return writes
	}
	var out []config.SubtreeReservedWrite
	for _, w := range writes {
		if !failed[w.File] {
			out = append(out, w)
		}
	}
	return out
}

// writtenInsideDefaults drops from writes the ones of an unwrapped file.
//
// ⛔ ONLY FOR TopLevelReadElsewhere KEYS (#2388 r1b): another reader takes
// such a key from the TOP level of a defaults file, so only a copy under a
// `defaults:` mapping is reported — nothing reads it there. pkg/config's
// chain view (subtreeReservedKeys) cannot tell the top level from
// `defaults:` in an unwrapped file, so this check, which holds the files'
// bytes, does. (`_custom_alerts` was such a key until #3042, when the then
// Python custom-alert compiler read it from a `_defaults.yaml` top level;
// the compiler, now pkg/config's (#3229), compiles what the exporter serves,
// so both places are reported.)
// A file not in DefaultsFiles is kept (reported), not guessed.
func writtenInsideDefaults(writes []config.SubtreeReservedWrite, unwrapped map[string]bool) []config.SubtreeReservedWrite {
	var out []config.SubtreeReservedWrite
	for _, w := range writes {
		if !unwrapped[w.File] {
			out = append(out, w)
		}
	}
	return out
}

// subtreeReservedMessage is the finding's message for key k written by
// writes (each file with its value, root-first).
//
// ⛔ THREE SHAPES, because "set it in each tenant's own entry" is only a fix
// where a tenant's own entry is read (#2388 r2, measured): a `_state_<f>` whose
// f the root `state_filters:` does not declare, and a key that is not a
// recognised reserved key (config.IsRecognisedReservedKey: `_silent_x`, which
// only baseRowsSkipKey accepts, and — #2388 r3 — `_routingProfile` /
// `_routings`, which IsReservedKey accepts by prefix alone), are read by
// nothing in a tenant's entry either — moving them there made this finding go
// away while the value was still not served, and nothing said so. Those get
// "declare the filter or delete it" / "delete it" instead.
//
// ⚠️ THE VALUE IS NAMED, per file (#2388 A r2): the exporter applies none of
// them (#3341), so an operator who wants a value for the tenant moves it into
// the tenant's own entry — and with several levels, "move it" has to say
// which value. Deleting the key changes nothing that is served.
func subtreeReservedMessage(declared map[string]bool, k string, writes []config.SubtreeReservedWrite) string {
	files := make([]string, len(writes))
	for i, w := range writes {
		files[i] = w.File
	}
	if k == "_custom_alerts" {
		// #3042: no reader takes a custom-alert list from a `_defaults.yaml`,
		// at its top level or under `defaults:` — the exporter has never
		// served one, and the compiler compiles what the exporter serves.
		return fmt.Sprintf("`_custom_alerts` is written in a subtree `_defaults.yaml` (%s) that this tenant "+
			"inherits from; no reader takes custom alerts from a `_defaults.yaml`: the exporter serves none of "+
			"them and the custom-alert compiler compiles none (#3042). Write the list in each tenant's own entry "+
			"under `tenants:`, in a root platform file's `tenants:`, or in a profile its tenants select.", quoteJoin(files))
	}
	where := fmt.Sprintf("is set in the defaults of a subtree `_defaults.yaml` (%s) that this tenant inherits from",
		writtenValues(k, writes))
	const unsupported = "subtree defaults do not carry reserved keys, and the exporter does not apply it " +
		"from there, whatever its value (#3341)."
	switch reservedKeyShape(k, declared) {
	case shapeUndeclaredState:
		return fmt.Sprintf("Reserved key `%s` %s; %s %s", k, where, unsupported, subtreeRefusedKeyFix(k, declared))
	case shapeUnrecognised:
		return fmt.Sprintf("Key `%s` %s. %s", k, where, subtreeRefusedKeyFix(k, declared))
	}
	del := "Delete it from this file"
	if len(writes) > 1 {
		del = fmt.Sprintf("Delete `%s` from every subtree file listed above", k)
	}
	return fmt.Sprintf("Reserved key `%s` %s; %s %s: what the exporter serves stays the same. %s",
		k, where, unsupported, del, reservedKeyFix(k))
}

// writtenValues renders writes as "`a/_defaults.yaml`: `k: v`, …" — each
// file and the value it writes; the value is left out where it does not fit
// one pasteable line (config.RenderYAMLFlow).
func writtenValues(k string, writes []config.SubtreeReservedWrite) string {
	q := make([]string, len(writes))
	for i, w := range writes {
		q[i] = "`" + w.File + "`"
		if r, ok := config.RenderYAMLFlow(w.Value); ok {
			q[i] += fmt.Sprintf(": `%s: %s`", k, r)
		}
	}
	return strings.Join(q, ", ")
}

// The three fix shapes of a key subtree defaults refuse.
const (
	shapeRecognised      = iota // (c) a recognised reserved key: set it in each tenant's entry
	shapeUndeclaredState        // (a) `_state_<f>`, f not declared: declare f or delete
	shapeUnrecognised           // (b) read by nothing anywhere: delete
)

// reservedKeyShape picks k's fix shape. A bare `_state_` is the filter named
// "", judged like any other (#2388 r5).
func reservedKeyShape(k string, declared map[string]bool) int {
	if f, isState := strings.CutPrefix(k, "_state_"); isState && !declared[f] {
		return shapeUndeclaredState
	}
	if !config.IsRecognisedReservedKey(k) {
		return shapeUnrecognised
	}
	return shapeRecognised
}

// subtreeRefusedKeyFix is the fix for a key subtree defaults refuse, by its
// shape. One text for both findings that name such a key in a subtree
// `_defaults.yaml`: subtree_default_reserved_key, and defaults_toplevel_ignored
// (#2388 r4), whose generic "move it under `defaults:`" would trade one
// finding for the other.
//
// For a recognised key it is defaults_toplevel_ignored's fix: the key sits
// beside a `defaults:` mapping, so the merge leaves it out and it has no
// effect; subtree_default_reserved_key words a recognised key itself
// (subtreeReservedMessage).
func subtreeRefusedKeyFix(k string, declared map[string]bool) string {
	switch reservedKeyShape(k, declared) {
	case shapeUndeclaredState:
		f := filterName(strings.TrimPrefix(k, "_state_"))
		return fmt.Sprintf("The conf.d root `state_filters:` does not declare a filter `%s`, so nothing reads `%s` "+
			"anywhere, a tenant's own entry included. Declare `%s` under `state_filters:` in the conf.d "+
			"root `_defaults.yaml` (this applies to every tenant in the tree), or delete it from this file.", f, k, f)
	case shapeUnrecognised:
		return "It is not a recognised key: the exporter does not read it, " +
			"in a subtree `_defaults.yaml` or in a tenant's own entry. Delete it from this file."
	}
	return "Today it has no effect here (the defaults merge leaves it out, so it is in neither /effective " +
		"nor what the exporter serves); delete it from this file to keep things as they are. " + reservedKeyFix(k)
}

// filterName renders a state filter's name for a message; the empty name,
// which `_state_` names and a root `state_filters:` can declare, as `""` (#2388 r6).
func filterName(f string) string {
	if f == "" {
		return `""`
	}
	return f
}

// filterPath is f as a path segment under `state_filters.`: the empty name
// quoted, so the path reads `state_filters."".default_state`.
func filterPath(f string) string { return filterName(f) }

// reservedKeyFix is the finding's fix sentence for a recognised reserved key
// k, `_state_<f>` with f declared (subtreeReservedMessage routes the rest).
// (No routing key reaches here: subtreeReservedKeys leaves those to the
// route generator's checks.)
func reservedKeyFix(k string) string { return reservedKeyFixAt(k, false) }

// reservedKeyFixAt is reservedKeyFix, worded for the conf.d root's own
// `defaults:` when atRoot (root_defaults_reserved_key, #3250): there is no
// subtree to scope a change to there.
func reservedKeyFixAt(k string, atRoot bool) string {
	if strings.HasPrefix(k, "_state_") {
		scope := "; to change only this subtree's tenants, set the key in each tenant's own entry."
		if atRoot {
			scope = "."
		}
		return fmt.Sprintf("To have it take effect, set `%s` in each tenant's own entry under `tenants:`, or set "+
			"`state_filters.%s.default_state` in the conf.d root `_defaults.yaml` — that affects every "+
			"tenant in the tree%s", k, filterPath(strings.TrimPrefix(k, "_state_")), scope)
	}
	if readElsewhere[k] { // a TopLevelReadElsewhere key: its reader takes it from the top level
		// A tenant's own entry reads it only when the registry says a tenant
		// layer does (config.IsReservedKey: `_custom_alerts` yes, `_policies`
		// — read at the top level alone — no; #3250 review).
		if !config.IsReservedKey(k) {
			return fmt.Sprintf("To have it take effect, move `%s` out of `defaults:` to the top level of the "+
				"file, where it is read.", k)
		}
		return fmt.Sprintf("To have it take effect, move `%s` out of `defaults:` to the top level of the file, "+
			"where it is read, or set it in each tenant's own entry under `tenants:`.", k)
	}
	return fmt.Sprintf("To have it take effect, set `%s` in each tenant's own entry under `tenants:`.", k)
}

// quoteJoin renders file paths as "`a`, `b`".
func quoteJoin(paths []string) string {
	q := make([]string, len(paths))
	for i, p := range paths {
		q[i] = "`" + p + "`"
	}
	return strings.Join(q, ", ")
}
