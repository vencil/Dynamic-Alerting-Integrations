package guard

// Reserved keys in subtree defaults (#2388).
//
// A subtree `_defaults.yaml` (not the conf.d root's) whose defaults carry a
// reserved key — `_state_*`, `_silent_mode`, `_severity_dedup`, `_metadata`,
// … (not the routing keys `_routing` / `_routing_<…>`, which the routing
// checks report) — or a `_silent_*` / `_state_*` key nothing reads is an operator
// configuration error: subtree defaults do
// not support these keys. Today the exporter's subtree overlay applies some
// values of them (a `disable`, a number) and drops the rest, so for example a
// subtree can switch a state filter off but not on. From the next minor
// release the exporter stops applying them, and this warning becomes an
// error; until then the exporter's behaviour is unchanged.
//
// ⛔ NO VERDICT OF ITS OWN, as checkSubtreeUndeliverable: which keys count is
// pkg/config's subtreeDefaultsRefusedKey — the same predicate #1976's report
// leaves out — over the defaults chain the exporter's build reads
// (subtreeReservedKeys), handed in as CheckInput.SubtreeReservedKeys.

import (
	"fmt"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// FindingSubtreeDefaultReservedKey (warn; #2388): a subtree `_defaults.yaml`
// in the tenant's defaults chain carries Field, a reserved key, in its
// defaults, whatever its non-null value and whether or not the exporter
// applies it.
// Planned to become an error in the next minor release, when the exporter
// stops applying such keys.
const FindingSubtreeDefaultReservedKey FindingKind = "subtree_default_reserved_key"

// checkSubtreeReservedKeys reports one FindingSubtreeDefaultReservedKey per
// (tenant, key) of input.SubtreeReservedKeys, for the tenants in
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
	unwrapped := unwrappedDefaultsFiles(input)
	var out []Finding
	for _, id := range tenants {
		byKey := input.SubtreeReservedKeys[id]
		keys := make([]string, 0, len(byKey))
		for k := range byKey {
			keys = append(keys, k)
		}
		sort.Strings(keys)
		for _, k := range keys {
			files := byKey[k]
			if readElsewhere[k] {
				files = writtenInsideDefaults(files, unwrapped)
				if len(files) == 0 {
					continue
				}
			}
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     FindingSubtreeDefaultReservedKey,
				TenantID: id,
				Field:    k,
				Message:  subtreeReservedMessage(k, files, input.DeclaredStateFilters, input.SubtreeReservedApplied[id][k]),
			})
		}
	}
	return out
}

// unwrappedDefaultsFiles is the set of input.DefaultsFiles (root-relative
// names) with no `defaults:` mapping: the defaults merge reads the whole
// document, so a key in pkg/config's chain view of such a file sits at its
// TOP level.
func unwrappedDefaultsFiles(input CheckInput) map[string]bool {
	out := map[string]bool{}
	for _, f := range input.DefaultsFiles {
		if doc := decodeDefaultsDoc(f.Data); doc != nil && !wrappedInMapping(doc) {
			out[f.Name] = true
		}
	}
	return out
}

// writtenInsideDefaults drops from files the unwrapped ones.
//
// ⛔ ONLY FOR TopLevelReadElsewhere KEYS (#2388 r1b). Another reader takes
// such a key from the TOP level of a defaults file, at every level: measured
// with the repo's own rule-packs/recipes/examples/conf.d, whose
// `finance/_defaults.yaml` carries `_custom_alerts` at the top level with no
// `defaults:` wrapper — compile_custom_alerts.py compiled 12 shapes (pay-a: 2
// recipes); the same list moved under `defaults:` compiled 11 (pay-a: 1),
// and no other finding named either. pkg/config's chain view
// (subtreeReservedKeys) cannot tell the top level from `defaults:` in an
// unwrapped file, so this check, which holds the files' bytes, does. A key
// under a `defaults:` mapping is still reported: nothing reads it there.
// A file not in DefaultsFiles is kept (reported), not guessed.
func writtenInsideDefaults(files []string, unwrapped map[string]bool) []string {
	var out []string
	for _, f := range files {
		if !unwrapped[f] {
			out = append(out, f)
		}
	}
	return out
}

// subtreeReservedMessage is the finding's message for key k written by files.
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
// applied is the exporter overlay's own verdict (config FlatBuild.
// SubtreeRefusedApplied): whether the subtree's value of k is in effect for
// this tenant today. It picks the recognised key's fix (#2388 A).
func subtreeReservedMessage(k string, files []string, declared map[string]bool, applied bool) string {
	where := fmt.Sprintf("is set in the defaults of a subtree `_defaults.yaml` (%s) that this tenant inherits from", quoteJoin(files))
	const tail = "From the next minor release the exporter stops applying these keys from a subtree " +
		"`_defaults.yaml`, and this warning becomes an error (#2388)."
	switch reservedKeyShape(k, declared) {
	case shapeUndeclaredState:
		return fmt.Sprintf("Reserved key `%s` %s; subtree defaults do not support reserved keys. %s %s",
			k, where, subtreeRefusedKeyFix(k, declared), tail)
	case shapeUnrecognised:
		return fmt.Sprintf("Key `%s` %s. %s %s", k, where, subtreeRefusedKeyFix(k, declared), tail)
	}
	fix := reservedKeyIgnoredFix(k)
	if applied {
		fix = reservedKeyMove(k)
	}
	return fmt.Sprintf("Reserved key `%s` %s; subtree defaults do not support reserved keys. %s %s",
		k, where, fix, tail)
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
// It is defaults_toplevel_ignored's fix: the key sits beside a `defaults:`
// mapping, so the merge leaves it out and it has no effect today — a
// recognised key gets reservedKeyIgnoredFix. subtree_default_reserved_key
// routes a recognised key itself (subtreeReservedMessage), by whether the
// overlay applies it (#2388 A).
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
	return reservedKeyIgnoredFix(k)
}

// reservedKeyIgnoredFix is the fix for a recognised key whose subtree value
// has no effect today: deleting it keeps what is served; setting it in the
// tenant's entry would START applying it.
//
// ⛔ #2388 A. r5 told every such key "delete it" (wrong for an applied
// `disable`: deleting it turned the filter back on); r6 told every one "move
// it" (wrong for an ignored `enable` / severity name: moving it turned them
// ON — measured, served `{_silent_mode: [], _state_offd: false}` became
// `{[warning], true}` with no finding left). Which one is right is the
// overlay's own verdict, handed in as `applied`; neither text guesses.
func reservedKeyIgnoredFix(k string) string {
	return "Today the exporter ignores this value; delete it from this file to keep things as they are. " +
		reservedKeyFix(k)
}

// reservedKeyMove is the fix for a recognised key the overlay APPLIES today:
// ONE action, moving it — deleting it alone changes what is served (measured:
// subtree `_state_maintenance: disable` under a root default_state enable;
// deleted, the filter came back on).
func reservedKeyMove(k string) string {
	const alone = " (Deleting it alone changes what is served today: the exporter applies this value now.)"
	if f, isState := strings.CutPrefix(k, "_state_"); isState {
		return fmt.Sprintf("Move it: set `%s` in each tenant's own entry under `tenants:` and delete it from this "+
			"file — or instead set `state_filters.%s.default_state` in the conf.d root `_defaults.yaml`, which "+
			"affects every tenant in the tree.%s", k, filterPath(f), alone)
	}
	return fmt.Sprintf("Move it: set `%s` in each tenant's own entry under `tenants:` and delete it from this "+
		"file.%s", k, alone)
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
// (No routing key reaches here: the routing checks own those.)
func reservedKeyFix(k string) string {
	if strings.HasPrefix(k, "_state_") {
		return fmt.Sprintf("To have it take effect, set `%s` in each tenant's own entry under `tenants:`, or set "+
			"`state_filters.%s.default_state` in the conf.d root `_defaults.yaml` — that affects every "+
			"tenant in the tree; to change only this subtree's tenants, set the key in each tenant's own entry.",
			k, filterPath(strings.TrimPrefix(k, "_state_")))
	}
	if readElsewhere[k] { // `_custom_alerts`: its reader takes it from the top level
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
