package guard

// Reserved keys in subtree defaults (#2388).
//
// A subtree `_defaults.yaml` (not the conf.d root's) whose defaults carry a
// reserved key — `_state_*`, `_silent_mode`, `_severity_dedup`, `_metadata`,
// … (not `_routing*`, which the routing checks report) — is an operator
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
				Message: fmt.Sprintf("Reserved key `%s` is set in the defaults of a subtree `_defaults.yaml` (%s) "+
					"that this tenant inherits from; subtree defaults do not support reserved keys. %s %s "+
					"From the next minor release the exporter stops applying these keys from a subtree "+
					"`_defaults.yaml`, and this warning becomes an error (#2388).",
					k, quoteJoin(files), reservedKeyToday(k), reservedKeyFix(k)),
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

// reservedKeyToday is the finding's sentence on what the exporter does with
// key k from a subtree defaults file today. pkg/config's applySubtreeDefaults
// applies a value only when it is threshold-shaped (`disable` or a number),
// the tenant does not set the key itself and a resolver reads the key; any
// other value is dropped.
func reservedKeyToday(k string) string {
	if strings.HasPrefix(k, "_state_") {
		return "Today the exporter applies a plain `disable` from there (for a filter the root " +
			"`state_filters:` declares) and ignores `enable`, " +
			"so the subtree can switch the filter off but not on."
	}
	return "Today the exporter applies at most a `disable` or numeric value of it from there " +
		"and ignores any other value (for example a severity name or a mapping)."
}

// reservedKeyFix is the finding's fix sentence for reserved key k. (No
// `_routing*` key reaches here: the routing checks own those.)
func reservedKeyFix(k string) string {
	if strings.HasPrefix(k, "_state_") {
		return fmt.Sprintf("Set `%s` in each tenant's own entry under `tenants:`, or set "+
			"`state_filters.%s.default_state` in the conf.d root `_defaults.yaml`.",
			k, strings.TrimPrefix(k, "_state_"))
	}
	if readElsewhere[k] { // `_custom_alerts`: its reader takes it from the top level
		return fmt.Sprintf("Move `%s` out of `defaults:` to the top level of the file, where it is read, "+
			"or set it in each tenant's own entry under `tenants:`.", k)
	}
	return fmt.Sprintf("Set `%s` in each tenant's own entry under `tenants:`.", k)
}

// quoteJoin renders file paths as "`a`, `b`".
func quoteJoin(paths []string) string {
	q := make([]string, len(paths))
	for i, p := range paths {
		q[i] = "`" + p + "`"
	}
	return strings.Join(q, ", ")
}
