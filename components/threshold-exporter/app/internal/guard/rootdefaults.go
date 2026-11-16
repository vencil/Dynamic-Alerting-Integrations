package guard

// The `defaults:` wrapper of a `_defaults.yaml` (#2386): two shapes where the
// wrapper decides which keys of the file the readers see, and neither shows
// in the merged maps the other checks read.
//
//  1. root_defaults_unwrapped — the conf.d ROOT carrier has no `defaults:`
//     mapping (no such key, or the key with no value). The defaults-chain
//     merge behind /effective (config.ExtractDefaultsBlock) then takes the
//     whole document as the block, so /effective shows its top-level keys;
//     the exporter's root decode (config.ThresholdConfig) reads platform
//     thresholds only from a `defaults:` mapping and has no field for the
//     others, so /metrics does not carry them from this file. Measured: a
//     top-level threshold was absent from /metrics, and a top-level
//     `_severity_dedup: disable` showed `disable` on /effective while
//     /metrics served `enable`. ROOT only: below the root the merge is what
//     serves the values.
//
//  2. defaults_toplevel_ignored — a carrier at ANY level whose `defaults:`
//     is a mapping (`{}` included, and a YAML `!!set`, which yaml.v3 decodes
//     as one) while the top level holds other keys. ExtractDefaultsBlock then
//     returns the mapping alone, so those keys stay out of the merge, and
//     out of /effective. Measured on a subtree: wrapped this way, a top-level
//     threshold and `_severity_dedup` were no longer served on /metrics
//     either; `_silent_mode` and `_state_*` changed /effective only (they
//     were not on /metrics in any shape).
//
// Both report the same keys (actsWhenMerged), judge the file's own bytes
// through the exporter's functions, and skip a file in ParseFailed (the
// exporter drops it; exit 3 names it once) and a document that does not
// decode or is not a mapping.

import (
	"fmt"
	"sort"
	"strconv"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// rootDecodedKeys are the top-level keys the exporter's root decode reads
// (config.RootDecodedKeys: the yaml tags of config.ThresholdConfig, the set
// the build's own FlatBuild.RootDefaultsUnread is judged by, #2296).
// TestRootDecodedKeysMatchSchema pins them to the non-`_` properties of
// platform-defaults.schema.json.
var rootDecodedKeys = config.RootDecodedKeys()

// TopLevelReadElsewhere are the `_`-prefixed keys another reader takes from
// the top level of a defaults file (config.TopLevelReadElsewhere, the one
// list; #2296 moved it so the exporter's build judges the same keys).
// They are never reported (tests/shared/defaults_wrapper_matrix.json pins
// the list).
var TopLevelReadElsewhere = config.TopLevelReadElsewhere()

var readElsewhere = toSet(TopLevelReadElsewhere)

func toSet(keys []string) map[string]bool {
	out := make(map[string]bool, len(keys))
	for _, k := range keys {
		out[k] = true
	}
	return out
}

// actsWhenMerged is config.ActsWhenMerged: the one predicate both this
// check and the exporter's FlatBuild.RootDefaultsUnread (#2296) use.
func actsWhenMerged(k string) bool { return config.ActsWhenMerged(k) }

// decodeDefaultsDoc decodes one defaults file as the defaults-chain merge
// does (config.ParseChainDefaults: yaml.v3, then NormalizeYAMLToJSON); nil
// when it does not decode or is not a mapping.
func decodeDefaultsDoc(data []byte) map[string]any {
	var raw any
	if err := yaml.Unmarshal(data, &raw); err != nil {
		return nil
	}
	m, _ := config.NormalizeYAMLToJSON(raw).(map[string]any)
	return m
}

// wrappedInMapping reports whether the merge reads only `defaults:` of doc:
// config.ExtractDefaultsBlock returns a map other than the document itself.
// (Asked of config rather than by comparing map pointers through reflect.)
func wrappedInMapping(doc map[string]any) bool {
	return config.DefaultsBlockWrapped(doc)
}

// keysActingWhenMerged lists, sorted, doc's top-level keys for which
// actsWhenMerged holds.
func keysActingWhenMerged(doc map[string]any) []string {
	var out []string
	for k := range doc {
		if actsWhenMerged(k) {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

func quoteKeys(keys []string) string { return "`" + strings.Join(keys, "`, `") + "`" }

// rootNumbersOnly is the remedy at the root, where `defaults:` is decoded as
// numbers only.
const rootNumbersOnly = "A threshold among them goes under `defaults:`; the root `defaults:` holds numbers only " +
	"(a mapping or a list there drops the whole block; a scalar that is no number is refused for that key " +
	"alone), so another key has no place in this file."

// customAlertsRootFix: no reader takes custom alerts from a `_defaults.yaml`
// (the exporter never served them; the compiler compiles what it serves,
// #3042) — where they go.
const customAlertsRootFix = "`_custom_alerts`: no reader takes custom alerts from a `_defaults.yaml`; " +
	"write the list in each tenant's own entry under `tenants:`, in a root platform file's `tenants:`, " +
	"or in a profile its tenants select (#3042)."

// rootFix is the root file's fix for the keys a finding names.
func rootFix(keys []string) string {
	for _, k := range keys {
		if k == "_custom_alerts" {
			return rootNumbersOnly + " " + customAlertsRootFix
		}
	}
	return rootNumbersOnly
}

// subtreeTopLevelFix is defaults_toplevel_ignored's fix for a SUBTREE file.
//
// ⛔ A KEY SUBTREE DEFAULTS REFUSE IS NOT MOVED UNDER `defaults:` (#2388 r4).
// Doing that — or leaving `defaults:` with no value — puts it in the subtree's
// defaults, which subtree_default_reserved_key then reports: the advice traded
// one finding for the other. Measured: `a/_defaults.yaml` with
// `defaults: {mysql_connections: 70}` and a top-level `_silent_mode: warning`.
// Such keys (config.SubtreeDefaultsRefusedKey, the predicate that finding
// reports on) get that finding's own fix (subtreeRefusedKeyFix), key by key;
// the other keys keep the generic fix.
func subtreeTopLevelFix(keys []string, declared map[string]bool) string {
	var move, refused []string
	for _, k := range keys {
		if config.SubtreeDefaultsRefusedKey(k) {
			refused = append(refused, k)
		} else {
			move = append(move, k)
		}
	}
	var parts []string
	if len(move) > 0 {
		// ⛔ "Or leave `defaults:` with no value" merges the WHOLE document,
		// refused keys included — the same trade as moving them. Offered only
		// when no refused key is in the file (#2388 r5).
		if len(refused) == 0 {
			parts = append(parts, fmt.Sprintf("Move %s under `defaults:`, or leave `defaults:` with no value "+
				"so the whole document is merged.", quoteKeys(move)))
		} else {
			parts = append(parts, fmt.Sprintf("Move %s under `defaults:`.", quoteKeys(move)))
		}
	}
	for _, k := range refused {
		parts = append(parts, fmt.Sprintf("`%s`: subtree defaults do not support it, so do not move it under "+
			"`defaults:` (subtree_default_reserved_key, #2388). %s", k, subtreeRefusedKeyFix(k, declared)))
	}
	return strings.Join(parts, " ")
}

// checkDefaultsWrapper reports both shapes over input.DefaultsFiles. The root
// carrier is the one whose root-relative path has no directory part.
func checkDefaultsWrapper(input CheckInput) []Finding {
	failed := make(map[string]bool, len(input.ParseFailed))
	for _, pf := range input.ParseFailed {
		failed[pf] = true
	}
	var out []Finding
	for _, f := range input.DefaultsFiles {
		if failed[f.Name] {
			continue
		}
		doc := decodeDefaultsDoc(f.Data)
		if doc == nil {
			continue
		}
		root := !strings.Contains(f.Name, "/")
		wrapped := wrappedInMapping(doc)
		if !wrapped && !root {
			continue // below the root the merge reads the whole document and serves it
		}
		keys := keysActingWhenMerged(doc)
		if len(keys) == 0 {
			continue
		}
		if wrapped {
			// Below the root, subtreeTopLevelFix keeps the generic "move them
			// under `defaults:`" for the keys that may go there (#2388 r4).
			fix := rootFix(keys)
			if !root {
				fix = subtreeTopLevelFix(keys, input.DeclaredStateFilters)
			}
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     findingkind.FindingDefaultsTopLevelIgnored,
				Field:    f.Name,
				Message: fmt.Sprintf("%s: `defaults:` is a mapping, so the defaults merge reads only the keys under it; "+
					"the top-level key(s) %s are left out of every tenant's merged config (/effective), "+
					"and a threshold among them is not served on /metrics. %s", f.Name, quoteKeys(keys), fix),
			})
			continue
		}
		out = append(out, Finding{
			Severity: SeverityError,
			Kind:     findingkind.FindingRootDefaultsUnwrapped,
			Field:    f.Name,
			Message: fmt.Sprintf("%s: the conf.d root defaults file has no `defaults:` mapping, so /effective "+
				"(the defaults-chain merge, which then reads the whole document) shows the top-level key(s) %s, "+
				"but threshold-exporter's root decode does not read them: /metrics does not carry them from "+
				"this file. %s", f.Name, quoteKeys(keys), rootFix(keys)),
		})
	}
	return out
}

// checkRootCriticalKeys reports findingkind.FindingRootDefaultsCriticalKey for the
// `_critical` keys under the root carrier's `defaults:` mapping. A root with
// no `defaults:` mapping is root_defaults_unwrapped's (its keys are not
// served at all); a file in ParseFailed is skipped like checkDefaultsWrapper
// does.
func checkRootCriticalKeys(input CheckInput) []Finding {
	failed := make(map[string]bool, len(input.ParseFailed))
	for _, pf := range input.ParseFailed {
		failed[pf] = true
	}
	var out []Finding
	for _, f := range input.DefaultsFiles {
		if failed[f.Name] || strings.Contains(f.Name, "/") {
			continue
		}
		doc := decodeDefaultsDoc(f.Data)
		if doc == nil || !wrappedInMapping(doc) {
			continue
		}
		block := config.ExtractDefaultsBlock(doc)
		keys := make([]string, 0, len(block))
		for k, v := range block {
			// #2518: a null is no write — the exporter serves no series for
			// it, so there is no "threshold of its own" to describe.
			if v == nil {
				continue
			}
			if strings.HasSuffix(k, "_critical") && !strings.HasPrefix(k, "_state_") && !strings.HasPrefix(k, "_silent_") {
				keys = append(keys, k)
			}
		}
		sort.Strings(keys)
		for _, k := range keys {
			base := strings.TrimSuffix(k, "_critical")
			// A key with a label segment (`x{a="1"}_critical`) is no row of
			// its own (#2031): the clauses about its rows do not hold.
			served, rest := "is not", "."
			if !strings.Contains(k, "{") {
				served = "is served as a threshold of its own (severity=warning), not as"
				rest = fmt.Sprintf(": only a tenant's own `%s` (or one a subtree `_defaults.yaml`, a root platform "+
					"`tenants:` entry or a profile supplies) produces that row. A tenant deleting its own `%s` falls "+
					"back to this root value only on that separate warning series, never on the critical row, so the "+
					"tenant's key is not reported redundant against it.", k, k)
			}
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     findingkind.FindingRootDefaultsCriticalKey,
				Field:    f.Name + ":defaults." + k,
				Message: fmt.Sprintf("%s: `%s` under the conf.d root `defaults:` %s the severity=critical row of `%s`%s",
					f.Name, k, served, base, rest),
			})
		}
	}
	return out
}

// checkRootDefaultsRefused reports findingkind.FindingRootDefaultRefused for
// each threshold under the conf.d root carrier's `defaults:` mapping whose
// value the exporter refuses (#3140, #3250): the decode's own verdict
// (config.RootDefaultRefusals, parseThresholdNumber on the text as written),
// so the keys named are the ones the resolver serves no default row for. A
// file in ParseFailed is skipped, as checkRootCriticalKeys does: it is
// reported for that.
func checkRootDefaultsRefused(input CheckInput) []Finding {
	failed := make(map[string]bool, len(input.ParseFailed))
	for _, pf := range input.ParseFailed {
		failed[pf] = true
	}
	var out []Finding
	for _, f := range input.DefaultsFiles {
		if failed[f.Name] || strings.Contains(f.Name, "/") {
			continue
		}
		doc := decodeDefaultsDoc(f.Data)
		if doc == nil || !wrappedInMapping(doc) {
			continue
		}
		for _, r := range config.RootDefaultRefusals(f.Data) {
			if config.BaseRowsSkipKey(r.Key) {
				continue
			}
			why := "is not a number the exporter reads"
			if r.Reason == config.RefusedNonFinite {
				why = "is not a finite number: an alert compared against NaN or an infinity never fires or always fires"
			}
			off := ""
			if config.IsDisabled(strings.ToLower(strings.TrimSpace(r.Text))) {
				// #3832 decision 3: the root reads one opt-out word only,
				// `disable`, and only for a default-armed key.
				if _, armed := config.BuiltinDefault(r.Key); armed {
					off = fmt.Sprintf(" The root reads no alias of `disable`: to switch `%s` off for every "+
						"tenant, write `%s: disable`.", r.Key, r.Key)
				} else {
					off = fmt.Sprintf(" `disable` under the root `defaults:` switches off only a key the platform "+
						"arms by default, and `%s` is not one: the root gives it no value to switch off.", r.Key)
				}
			}
			if v, armed := config.BuiltinDefault(r.Key); armed {
				// #3832: a default-armed key falls to the built-in value.
				out = append(out, Finding{
					Severity: SeverityError,
					Kind:     findingkind.FindingRootDefaultRefused,
					Field:    f.Name + ":defaults." + r.Key,
					Message: fmt.Sprintf("%s: `%s: %s` under the conf.d root `defaults:` %s. The exporter serves "+
						"the platform's built-in value %s for `%s` instead, as if the line were not there. Write a "+
						"finite number to serve your own value (a root default takes no `:severity`), or delete the "+
						"line to keep the platform's.%s", f.Name, r.Key, r.Text, why, formatBuiltin(v), r.Key, off),
				})
				continue
			}
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     findingkind.FindingRootDefaultRefused,
				Field:    f.Name + ":defaults." + r.Key,
				Message: fmt.Sprintf("%s: `%s: %s` under the conf.d root `defaults:` %s. The exporter serves no "+
					"default row for `%s`; only a tenant's own value is served, as for an `optional_overrides:` "+
					"key. Write a finite number (a root default takes no `:severity`), or list `%s` under "+
					"`optional_overrides:` instead.%s", f.Name, r.Key, r.Text, why, r.Key, r.Key, off),
			})
		}
	}
	return out
}

// checkRootOptionalBuiltin reports findingkind.FindingRootOptionalBuiltin
// for each default-armed key (config.BuiltinDefault) the conf.d root
// carrier lists under its top-level `optional_overrides:` while its
// `defaults:` mapping does not give the key a value — the key is absent there
// or written as null (#3832). Such a list entry declares the key without a
// platform value, which for a default-armed key changes nothing: the built-in
// layer still gives every tenant the platform's value. A tree that lists the
// key to keep its alert off (what validate-config advised before the built-in
// layer, #3830) gets the alert after the upgrade, so the list entry is an
// error, not a no-op. A value the root does write — a number, or one the
// decode refuses (checkRootDefaultsRefused's) — is not this check's. A file
// in ParseFailed is skipped, as checkRootCriticalKeys does.
func checkRootOptionalBuiltin(input CheckInput) []Finding {
	failed := make(map[string]bool, len(input.ParseFailed))
	for _, pf := range input.ParseFailed {
		failed[pf] = true
	}
	var out []Finding
	for _, f := range input.DefaultsFiles {
		if failed[f.Name] || strings.Contains(f.Name, "/") {
			continue
		}
		doc := decodeDefaultsDoc(f.Data)
		if doc == nil {
			continue
		}
		list, _ := doc["optional_overrides"].([]any)
		var defaults map[string]any
		if wrappedInMapping(doc) {
			defaults, _ = doc["defaults"].(map[string]any)
		}
		seen := map[string]bool{}
		for _, item := range list {
			k, ok := item.(string)
			if !ok || seen[k] {
				continue
			}
			seen[k] = true
			v, armed := config.BuiltinDefault(k)
			if !armed || defaults[k] != nil {
				continue
			}
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     findingkind.FindingRootOptionalBuiltin,
				Field:    f.Name + ":optional_overrides." + k,
				Message: fmt.Sprintf("%s: `%s` is listed under the conf.d root `optional_overrides:` without a "+
					"number under `defaults:`. The platform arms `%s` by default: every tenant gets its built-in "+
					"value %s, and the list does not switch it off. To keep its alert off for every tenant, write "+
					"`%s: disable` under the root `defaults:` (for part of the tree: in a subtree `_defaults.yaml` "+
					"or the tenant files); to serve your own value, give it a number under `defaults:`. Then "+
					"delete the list entry.", f.Name, k, k, formatBuiltin(v), k),
			})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Field < out[j].Field })
	return out
}

// checkRootDisableFamily reports findingkind.FindingRootDisableFamilyValue
// for each key of the conf.d root carrier's `defaults:` mapping that gives a
// value (not null) to `<base>_critical` or a dimensional `<base>{…}` while
// the same mapping writes base as `disable` (config.RootDisableText, #3832
// decision 3). The `disable` switches base's whole family off for every
// tenant that does not give base a value of its own, so the root's value of
// the family key is served to no such tenant: the root contradicts itself.
// A file in ParseFailed is skipped, as checkRootCriticalKeys does.
func checkRootDisableFamily(input CheckInput) []Finding {
	failed := make(map[string]bool, len(input.ParseFailed))
	for _, pf := range input.ParseFailed {
		failed[pf] = true
	}
	var out []Finding
	for _, f := range input.DefaultsFiles {
		if failed[f.Name] || strings.Contains(f.Name, "/") {
			continue
		}
		doc := decodeDefaultsDoc(f.Data)
		if doc == nil || !wrappedInMapping(doc) {
			continue
		}
		defaults, _ := doc["defaults"].(map[string]any)
		off := map[string]bool{}
		for k, v := range defaults {
			if s, ok := v.(string); ok && config.RootDisableText(k, s) {
				off[k] = true
			}
		}
		if len(off) == 0 {
			continue
		}
		for k, v := range defaults {
			if v == nil || off[k] {
				continue
			}
			base := strings.TrimSuffix(k, "_critical")
			if i := strings.Index(k, "{"); i >= 0 {
				base = k[:i]
			}
			if base == k || !off[base] {
				continue
			}
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     findingkind.FindingRootDisableFamilyValue,
				Field:    f.Name + ":defaults." + k,
				Message: fmt.Sprintf("%s: the conf.d root `defaults:` writes `%s: disable`, which switches the whole "+
					"`%s` family off for every tenant — `%s` included, so its value here is served to no tenant. "+
					"Delete one of the two lines: the `disable` to serve the family, or `%s` to keep it off. A "+
					"subtree or tenant that gives `%s` a value gets the family back, and its own `%s` with it.",
					f.Name, base, base, k, k, base, k),
			})
		}
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Field < out[j].Field })
	return out
}

// formatBuiltin is a built-in value as the generated table writes it.
func formatBuiltin(v float64) string { return strconv.FormatFloat(v, 'f', -1, 64) }

// checkRootDimensionsCriticalBase reports
// findingkind.FindingRootDimensionsCriticalBase for each base under the
// conf.d root carrier's top-level `dimensions:` that is a `_critical` tier
// (config.DimensionBaseRefused, #3556 — the predicate dimensionDeclared
// refuses it by, so what is named here is what serves no row). The root
// decode reads `dimensions:` whether or not `defaults:` wraps the file, so a
// root without the wrapper is judged too. A file in ParseFailed is skipped,
// as checkRootCriticalKeys does.
func checkRootDimensionsCriticalBase(input CheckInput) []Finding {
	failed := make(map[string]bool, len(input.ParseFailed))
	for _, pf := range input.ParseFailed {
		failed[pf] = true
	}
	var out []Finding
	for _, f := range input.DefaultsFiles {
		if failed[f.Name] || strings.Contains(f.Name, "/") {
			continue
		}
		doc := decodeDefaultsDoc(f.Data)
		dims, _ := doc["dimensions"].(map[string]any)
		bases := make([]string, 0, len(dims))
		for base := range dims {
			if config.DimensionBaseRefused(base) {
				bases = append(bases, base)
			}
		}
		sort.Strings(bases)
		for _, base := range bases {
			tier := strings.TrimSuffix(base, "_critical")
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     findingkind.FindingRootDimensionsCriticalBase,
				Field:    f.Name + ":dimensions." + base,
				Message: fmt.Sprintf("%s: `%s` under the conf.d root `dimensions:` is the name of the "+
					"severity=critical tier of `%s`, not a base, so it declares nothing and no `%s{...}` key is "+
					"served. A per-dimension critical threshold is `%s{label=\"v\"}: \"N:critical\"`, its labels "+
					"declared under `%s`.",
					f.Name, base, tier, base, tier, tier),
			})
		}
	}
	return out
}

// checkRootReservedKeys reports findingkind.FindingRootDefaultsReservedKey for
// each `_` key under the root carrier's `defaults:` mapping that reaches no
// tenant (config.RootDefaultsReservedKey). A null is no write (#2518) and a
// routing key is the route generator's to judge; a file in ParseFailed is
// skipped, as checkRootCriticalKeys does. The fix is subtree_default_reserved_key's
// (reservedKeyFixAt, worded for the root): where the key's reader reads it.
func checkRootReservedKeys(input CheckInput) []Finding {
	failed := make(map[string]bool, len(input.ParseFailed))
	for _, pf := range input.ParseFailed {
		failed[pf] = true
	}
	var out []Finding
	for _, f := range input.DefaultsFiles {
		if failed[f.Name] || strings.Contains(f.Name, "/") {
			continue
		}
		doc := decodeDefaultsDoc(f.Data)
		if doc == nil || !wrappedInMapping(doc) {
			continue
		}
		block := config.ExtractDefaultsBlock(doc)
		keys := make([]string, 0, len(block))
		for k, v := range block {
			if v != nil && config.RootDefaultsReservedKey(k) && !config.IsRoutingKey(k) {
				keys = append(keys, k)
			}
		}
		sort.Strings(keys)
		for _, k := range keys {
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     findingkind.FindingRootDefaultsReservedKey,
				Field:    f.Name + ":defaults." + k,
				Message: fmt.Sprintf("%s: `%s` under the conf.d root `defaults:` reaches no tenant: the exporter "+
					"reads the root `defaults:` for thresholds only, while /effective shows the value as inherited "+
					"(not_served root_defaults_reserved). %s", f.Name, k, reservedKeyFixAt(k, true)),
			})
		}
	}
	return out
}
