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
				Message:  subtreeReservedMessage(input, id, k, files),
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
// A recognised key's fix follows the exporter overlay's own verdict
// (input.SubtreeRefusedVerdicts, #2388 A): applied for this tenant → move the
// value it gets; ignored → delete, unless another tenant gets its value from
// one of these files (sharedApplied).
func subtreeReservedMessage(input CheckInput, tenant, k string, files []string) string {
	declared := input.DeclaredStateFilters
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
	v := input.SubtreeRefusedVerdicts[tenant][k]
	others := sharedApplied(input.SubtreeRefusedVerdicts, tenant, k, files)
	var fix string
	switch {
	case v.Applied:
		fix = reservedKeyMove(k, v, files, others)
	case len(others) > 0:
		fix = reservedKeyIgnoredShared(k, v, others)
	default:
		fix = reservedKeyIgnoredFix(k, v)
	}
	return fmt.Sprintf("Reserved key `%s` %s; subtree defaults do not support reserved keys. %s %s",
		k, where, fix, tail)
}

// sharedApplied is, sorted, every OTHER tenant of the whole tree that gets
// its value of k from one of files (config.SubtreeRefusedVerdict.Source):
// deleting k from those files changes what is served for them.
//
// ⛔ WHOLE TREE, NOT --scope (#2388 A r2). A subtree file is shared: measured,
// a/_defaults.yaml `_state_maintenance: disable` is applied for t1 and
// ignored for t2 (which sets the key itself). t2's finding said "delete it
// … to keep things as they are"; deleting it turned t1's filter on, and with
// `--scope a/x` only t2's finding was shown at all. The verdicts are the
// exporter's whole-tree load (ScopedTenants.SubtreeRefusedVerdicts).
func sharedApplied(verdicts map[string]map[string]config.SubtreeRefusedVerdict, tenant, k string, files []string) []string {
	in := make(map[string]bool, len(files))
	for _, f := range files {
		in[f] = true
	}
	var out []string
	for id, byKey := range verdicts {
		if id == tenant {
			continue
		}
		if v := byKey[k]; v.Applied && in[v.Source] {
			out = append(out, id)
		}
	}
	sort.Strings(out)
	return out
}

// tenantList renders ids as "`a`, `b`, `c` and 4 more" (at most 5 named).
func tenantList(ids []string) string {
	const shown = 5
	q := make([]string, 0, shown)
	for i, id := range ids {
		if i == shown {
			break
		}
		q = append(q, "`"+id+"`")
	}
	s := strings.Join(q, ", ")
	if len(ids) > shown {
		s += fmt.Sprintf(" and %d more", len(ids)-shown)
	}
	return s
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
	return "Today it has no effect here (the defaults merge leaves it out, so it is in neither /effective " +
		"nor what the exporter serves); delete it from this file to keep things as they are. " + reservedKeyFix(k)
}

// reservedKeyIgnoredFix is the fix for a recognised key whose subtree value
// has no effect today, for this tenant AND every other tenant that inherits
// these files: deleting it keeps what the exporter serves; setting it in the
// tenant's entry would START applying it.
//
// ⚠️ "What the exporter serves", not "things": /effective does show the
// ignored subtree value, and deleting it removes it from there (#2388 A r2).
// When the tenant sets the key itself, its own entry is what is served and
// there is nothing to "have take effect".
//
// ⛔ #2388 A. r5 told every such key "delete it" (wrong for an applied
// `disable`: deleting it turned the filter back on); r6 told every one "move
// it" (wrong for an ignored `enable` / severity name: moving it turned them
// ON — measured, served `{_silent_mode: [], _state_offd: false}` became
// `{[warning], true}` with no finding left). Which one is right is the
// overlay's own verdict, handed in as `applied`; neither text guesses.
func reservedKeyIgnoredFix(k string, v config.SubtreeRefusedVerdict) string {
	if v.TenantSets {
		return fmt.Sprintf("This tenant's own entry sets `%s`, and that is what the exporter serves; the subtree "+
			"value is ignored for it. Delete it from this file: what the exporter serves stays the same.", k)
	}
	return "Today the exporter ignores this value; delete it from this file: what the exporter serves stays the " +
		"same, and /effective stops showing the ignored value. " + reservedKeyFix(k)
}

// reservedKeyIgnoredShared is the fix for a recognised key ignored for this
// tenant but applied, from one of the same files, to other tenants: the file
// must not just be deleted (sharedApplied says why).
func reservedKeyIgnoredShared(k string, v config.SubtreeRefusedVerdict, others []string) string {
	own := "Today the exporter ignores this value for this tenant"
	if v.TenantSets {
		own = fmt.Sprintf("This tenant's own entry sets `%s`, and that is what the exporter serves for it; "+
			"the subtree value is ignored for this tenant", k)
	}
	return fmt.Sprintf("%s — but it is applied to other tenants that inherit the same file: %s. Do not just "+
		"delete it from the file: that changes what is served for them. Follow their findings (move the value "+
		"into their own entries) first; then delete it.", own, tenantList(others))
}

// reservedKeyMove is the fix for a recognised key the overlay APPLIES for this
// tenant today: ONE action, moving it — deleting it alone can change what is
// served (measured: subtree `_state_maintenance: disable` under a root
// default_state enable; deleted, the filter came back on).
//
// ⛔ IT NAMES THE VALUE (#2388 A r2). With several subtree levels writing the
// key, "move it" did not say which: measured, a/ `_silent_mode: disable`
// (applied) and a/us/ `warning` (dropped) served `[]`, and moving `warning`
// served `["warning"]`. The value named is the one the tenant gets today
// (SubtreeRefusedVerdict.Value, from .Source), and the key is to be deleted
// from every listed file.
//
// ⚠️ "Can change", not "changes" (#2388 A r2, F3): Applied means the exporter
// uses the subtree's value for this tenant; it is not judged whether that
// value differs from what the tenant would get without it (a subtree
// `disable` under a root default_state disable is applied and a no-op).
// Judging that would need every reserved key's resolver re-run without the
// value — a second model of those resolvers, which this check does not keep.
func reservedKeyMove(k string, v config.SubtreeRefusedVerdict, files, others []string) string {
	val := fmt.Sprintf("`%s: %s`", k, config.RenderYAMLFlow(v.Value))
	from := fmt.Sprintf("the value this tenant gets today, from `%s`", v.Source)
	del := "delete it from this file"
	if len(files) > 1 {
		del = fmt.Sprintf("delete `%s` from every subtree file listed above", k)
	}
	var b strings.Builder
	if f, isState := strings.CutPrefix(k, "_state_"); isState {
		fmt.Fprintf(&b, "Move it: set %s (%s) in this tenant's own entry under `tenants:` and %s — or instead "+
			"set `state_filters.%s.default_state` in the conf.d root `_defaults.yaml`, which affects every tenant "+
			"in the tree.", val, from, del, filterPath(f))
	} else {
		fmt.Fprintf(&b, "Move it: set %s (%s) in this tenant's own entry under `tenants:` and %s.", val, from, del)
	}
	b.WriteString(" (Deleting it alone can change what is served: the exporter uses this value for this tenant now.)")
	if len(others) > 0 {
		fmt.Fprintf(&b, " Other tenants get their value from the same file too: %s — move it into their entries "+
			"as well before deleting it.", tenantList(others))
	}
	return b.String()
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
