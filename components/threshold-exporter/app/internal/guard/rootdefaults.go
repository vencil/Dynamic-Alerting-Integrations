package guard

// The `defaults:` wrapper of a `_defaults.yaml` (#2386): two shapes where the
// wrapper decides which keys of the file reach any tenant, and neither shows
// in the merged maps the other checks read.
//
//  1. root_defaults_unwrapped — the conf.d ROOT carrier has no `defaults:`
//     mapping (no such key, or the key with no value). The exporter reads that file into config.ThresholdConfig, whose
//     platform thresholds are the `defaults:` field only, so a threshold at
//     the top level is not served on /metrics; the defaults-chain merge
//     behind /effective (config.ExtractDefaultsBlock) takes a document with
//     no `defaults:` mapping as the block itself, so /effective shows it.
//     ROOT only: below the root that merge is what serves the values.
//
//  2. defaults_toplevel_ignored — a carrier at ANY level whose `defaults:`
//     is a mapping (`{}` included) while the top level holds other keys.
//     config.ExtractDefaultsBlock then returns the mapping alone, so those
//     top-level keys never enter the defaults merge. Below the root that is
//     the difference between a file without the wrapper (or with
//     `defaults:` and no value, which the merge also reads whole) and the
//     same file wrapped: measured, a subtree's top-level
//     `_severity_dedup: disable` was served as `disable` unwrapped and as
//     `enable` once `defaults: {}` was added.
//
// Both judge the file's own bytes through the exporter's functions; they
// skip a file in ParseFailed (the exporter drops it; exit 3 names it once)
// and a document that does not decode or is not a mapping.

import (
	"fmt"
	"reflect"
	"sort"
	"strings"

	"gopkg.in/yaml.v3"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// FindingRootDefaultsUnwrapped (error, TenantID ""; #2386): the conf.d root
// defaults carrier is a mapping with no top-level `defaults:` key, and it has
// top-level keys the exporter's root decode does not read. Field is the
// file's root-relative path.
const FindingRootDefaultsUnwrapped FindingKind = "root_defaults_unwrapped"

// FindingDefaultsTopLevelIgnored (error, TenantID ""; #2386): a defaults
// carrier whose `defaults:` is a mapping and whose top level also holds keys
// no reader takes from there (topLevelIgnored). Field is the file's
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
// `_custom_alerts`. They are not reported as ignored. The same list is
// validate-config's (tests/shared/defaults_wrapper_matrix.json pins both).
var TopLevelReadElsewhere = []string{"_custom_alerts", "_routing_defaults", "_routing_enforced"}

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

// topLevelIgnored lists, sorted, the top-level keys of a wrapped doc that
// would act if merged and that no reader takes from there: a threshold (no
// `_` prefix) or a reserved tenant key (config.IsReservedKey) other than a
// `_routing*` one, and not `defaults` or a config.ThresholdConfig field.
// Not reported: any other `_` key, which acts in neither shape (e.g. `_x:
// &x`, a key that only carries a YAML anchor), and `_routing*` keys — the
// route generator does not read routing from a defaults block either
// (routing_in_unread_location), so wrapping changes nothing for them.
func topLevelIgnored(doc map[string]any) []string {
	elsewhere := map[string]bool{}
	for _, k := range TopLevelReadElsewhere {
		elsewhere[k] = true
	}
	var out []string
	for k := range doc {
		if k == "defaults" || rootDecodedKeys[k] || elsewhere[k] {
			continue
		}
		if !strings.HasPrefix(k, "_") || (config.IsReservedKey(k) && !strings.HasPrefix(k, "_routing")) {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

func quoteKeys(keys []string) string { return "`" + strings.Join(keys, "`, `") + "`" }

// checkDefaultsWrapper reports both shapes over input.DefaultsFiles. The root
// carrier is the one whose root-relative path has no directory part.
//
// ⚠️ root_defaults_unwrapped does not report `_`-prefixed keys: they are the
// reserved keys other readers take from the top level, and a `_routing*`
// key the route generator does not read there is
// routing_in_unread_location's finding. A metric key that starts with `_`
// is therefore not caught by it.
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
		if wrappedInMapping(doc) {
			ignored := topLevelIgnored(doc)
			if len(ignored) == 0 {
				continue
			}
			fix := "Move them under `defaults:`, or leave `defaults:` with no value so the whole document is merged."
			if root {
				fix = "Below `defaults:` the root file holds numbers only (a non-numeric value there drops the whole block); " +
					"a threshold among them goes under `defaults:`."
			}
			out = append(out, Finding{
				Severity: SeverityError,
				Kind:     FindingDefaultsTopLevelIgnored,
				Field:    f.Name,
				Message: fmt.Sprintf("%s: `defaults:` is a mapping, so the defaults merge reads only the keys under it; "+
					"the top-level key(s) %s reach no tenant. %s", f.Name, quoteKeys(ignored), fix),
			})
			continue
		}
		// Not wrapped: the merge reads the whole document (no `defaults:`
		// key, or `defaults:` with no value), while the root decode reads
		// thresholds only from a `defaults:` mapping.
		if !root {
			continue
		}
		var dropped []string
		for k := range doc {
			if !strings.HasPrefix(k, "_") && !rootDecodedKeys[k] {
				dropped = append(dropped, k)
			}
		}
		if len(dropped) == 0 {
			continue
		}
		sort.Strings(dropped)
		out = append(out, Finding{
			Severity: SeverityError,
			Kind:     FindingRootDefaultsUnwrapped,
			Field:    f.Name,
			Message: fmt.Sprintf("%s: the conf.d root defaults file has no top-level `defaults:` mapping. "+
				"threshold-exporter reads the root file's platform thresholds only from under `defaults:`, "+
				"so the top-level key(s) %s are not served on /metrics, although /effective "+
				"(the defaults-chain merge) shows them. Put them under `defaults:`.",
				f.Name, quoteKeys(dropped)),
		})
	}
	return out
}
