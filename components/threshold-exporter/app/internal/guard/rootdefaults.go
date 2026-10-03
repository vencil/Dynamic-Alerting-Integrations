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
	"reflect"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// FindingRootDefaultsUnwrapped (error, TenantID ""; #2386): the conf.d root
// defaults carrier has no `defaults:` mapping, and its top level holds keys
// /effective shows but the exporter's root decode does not read. Field is
// the file's root-relative path.
const FindingRootDefaultsUnwrapped FindingKind = "root_defaults_unwrapped"

// FindingDefaultsTopLevelIgnored (error, TenantID ""; #2386): a defaults
// carrier whose `defaults:` is a mapping and whose top level also holds keys
// the merge would otherwise take (actsWhenMerged). Field is the file's
// root-relative path.
const FindingDefaultsTopLevelIgnored FindingKind = "defaults_toplevel_ignored"

// rootDecodedKeys are the top-level keys the exporter's root decode reads:
// the yaml tags of config.ThresholdConfig, taken from the struct so this set
// cannot drift from it. TestRootDecodedKeysMatchSchema pins them to the
// non-`_` properties of platform-defaults.schema.json, which validate-config
// reads for the same judgement.
var rootDecodedKeys = func() map[string]bool {
	out := map[string]bool{}
	t := reflect.TypeOf(config.ThresholdConfig{})
	for i := 0; i < t.NumField(); i++ {
		name, _, _ := strings.Cut(t.Field(i).Tag.Get("yaml"), ",")
		if name != "" && name != "-" {
			out[name] = true
		}
	}
	return out
}()

// TopLevelReadElsewhere are the `_`-prefixed keys another reader takes from
// the top level of a defaults file: the route generator's
// `_routing_defaults` / `_routing_enforced`, and the custom-alert compiler's
// `_custom_alerts`. They are never reported. The same list is
// validate-config's (tests/shared/defaults_wrapper_matrix.json pins both).
var TopLevelReadElsewhere = []string{"_custom_alerts", "_routing_defaults", "_routing_enforced"}

var (
	readElsewhere = toSet(TopLevelReadElsewhere)
	mergeDropped  = toSet(config.MergeDroppedKeys())
)

func toSet(keys []string) map[string]bool {
	out := make(map[string]bool, len(keys))
	for _, k := range keys {
		out[k] = true
	}
	return out
}

// actsWhenMerged reports whether a top-level key of a defaults file is one
// the defaults merge takes into a tenant's config when it reads the whole
// document: a threshold (no `_` prefix) or a reserved tenant key
// (config.IsReservedKey). Excluded: `defaults` itself; a
// config.ThresholdConfig field; TopLevelReadElsewhere; a key the merge drops
// at every level (config.MergeDroppedKeys — `_metadata`), which acts in no
// shape; a `_routing*` key, which the route generator does not read from a
// defaults file in any shape (routing_in_unread_location names it); and any
// other `_` key (e.g. `_x: &x`, a key that only carries a YAML anchor).
func actsWhenMerged(k string) bool {
	if k == "defaults" || rootDecodedKeys[k] || readElsewhere[k] || mergeDropped[k] {
		return false
	}
	if !strings.HasPrefix(k, "_") {
		return true
	}
	// ⚠️ Refs #2388: this `_routing` PREFIX is wider than config.IsRoutingKey
	// (`_routing`, `_routing_<…>`), so `_routingProfile` is left out here; left
	// as is for a follow-up.
	return config.IsReservedKey(k) && !strings.HasPrefix(k, "_routing")
}

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
func wrappedInMapping(doc map[string]any) bool {
	block := config.ExtractDefaultsBlock(doc)
	return block != nil && reflect.ValueOf(block).UnsafePointer() != reflect.ValueOf(doc).UnsafePointer()
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
	"(a non-numeric value there drops the whole block), so another key has no place in this file."

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
			fix := rootNumbersOnly
			if !root {
				fix = subtreeTopLevelFix(keys, input.DeclaredStateFilters)
			}
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingDefaultsTopLevelIgnored,
				Field:    f.Name,
				Message: fmt.Sprintf("%s: `defaults:` is a mapping, so the defaults merge reads only the keys under it; "+
					"the top-level key(s) %s are left out of every tenant's merged config (/effective), "+
					"and a threshold among them is not served on /metrics. %s", f.Name, quoteKeys(keys), fix),
			})
			continue
		}
		out = append(out, Finding{
			Severity: SeverityError,
			Kind:     FindingRootDefaultsUnwrapped,
			Field:    f.Name,
			Message: fmt.Sprintf("%s: the conf.d root defaults file has no `defaults:` mapping, so /effective "+
				"(the defaults-chain merge, which then reads the whole document) shows the top-level key(s) %s, "+
				"but threshold-exporter's root decode does not read them: /metrics does not carry them from "+
				"this file. %s", f.Name, quoteKeys(keys), rootNumbersOnly),
		})
	}
	return out
}

// FindingRootDefaultsCriticalKey (warn, TenantID ""; #2544): a
// `<metric>_critical` key (either #1231 spelling) under the conf.d root
// carrier's `defaults:` mapping. The exporter serves it as a threshold of its
// own (metric `<metric>_critical`, severity=warning); it does not become
// `<metric>`'s severity=critical row, which only a tenant's override map
// carries (the tenant's own key, or one a subtree `_defaults.yaml`, a root
// platform `tenants:` entry or a profile hands it). Field is
// `<file>:defaults.<key>`. Not blocking (owner decision on #2544, option b).
const FindingRootDefaultsCriticalKey FindingKind = "root_defaults_critical_key"

// checkRootCriticalKeys reports FindingRootDefaultsCriticalKey for the
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
			out = append(out, Finding{
				Severity: SeverityWarn,
				Kind:     FindingRootDefaultsCriticalKey,
				Field:    f.Name + ":defaults." + k,
				Message: fmt.Sprintf("%s: `%s` under the conf.d root `defaults:` is served as a threshold of its own "+
					"(severity=warning), not as the severity=critical row of `%s`: only a tenant's own `%s` "+
					"(or one a subtree `_defaults.yaml`, a root platform `tenants:` entry or a profile supplies) "+
					"produces that row. A tenant deleting its own `%s` falls back to this root value only on that "+
					"separate warning series, never on the critical row, so the tenant's key is not reported "+
					"redundant against it.", f.Name, k, base, k, k),
			})
		}
	}
	return out
}
