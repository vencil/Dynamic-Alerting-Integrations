package guard

// Root `_defaults.yaml` with no `defaults:` wrapper (#2386).
//
// The exporter reads the conf.d ROOT defaults carrier into
// config.ThresholdConfig, whose platform thresholds are the `defaults:`
// field only: a threshold written at the top level of that file is not a
// field of the struct, so /metrics serves nothing for it and the load logs
// nothing. The defaults-chain merge behind /effective (and behind this
// guard's effective configs) takes a document with no `defaults:` mapping as
// the block itself (config.ExtractDefaultsBlock), so the same value IS
// visible there — the merge-based checks cannot see the gap. Hence a check
// on the file's shape rather than on the merged maps.
//
// ⛔ ROOT ONLY. A subtree carrier without the wrapper is merged by the
// hierarchical plane, which takes the whole document as the block, and its
// values are served. platform-defaults.schema.json requires `defaults:` in
// every `_defaults*` file (owner ruling D2(b) on #2386, shape uniformity);
// this check reports only where the missing wrapper changes what is served.

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

// rootDecodedKeys are the top-level keys the exporter's root decode reads:
// the yaml tags of config.ThresholdConfig, taken from the struct so this set
// cannot drift from it. TestRootDecodedKeysMatchSchema pins them to the
// non-`_` properties of platform-defaults.schema.json, which validate-config's
// root_defaults row reads for the same judgement.
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

// checkRootDefaultsWrapper reports the root carrier among input.DefaultsFiles
// (the one whose root-relative path has no directory part) when it is a
// mapping without a `defaults` key that has at least one top-level key which
// the exporter's root decode does not read (rootDecodedKeys) and which does
// not start with `_`. Those are the keys /metrics drops while /effective
// shows them.
//
// ⚠️ `_`-prefixed keys are not reported: they are the reserved keys other
// readers take from the top level (`_routing_defaults`, `_custom_alerts` …);
// a `_routing*` key the route generator does not read there is
// routing_in_unread_location's finding. A metric key that starts with `_`
// is therefore not caught here.
//
// A file in input.ParseFailed is skipped: the exporter drops it and exit 3
// names it once. A document that is empty, null or not a mapping is not
// reported: empty / null is a placeholder the exporter accepts, and a
// non-mapping fails the exporter's decode (exit 3).
func checkRootDefaultsWrapper(input CheckInput) []Finding {
	failed := make(map[string]bool, len(input.ParseFailed))
	for _, pf := range input.ParseFailed {
		failed[pf] = true
	}
	var out []Finding
	for _, f := range input.DefaultsFiles {
		if strings.Contains(f.Name, "/") || failed[f.Name] {
			continue
		}
		var doc any
		if err := yaml.Unmarshal(f.Data, &doc); err != nil {
			continue
		}
		m, ok := doc.(map[string]any)
		if !ok {
			continue
		}
		if _, wrapped := m["defaults"]; wrapped {
			continue
		}
		var dropped []string
		for k := range m {
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
				f.Name, "`"+strings.Join(dropped, "`, `")+"`"),
		})
	}
	return out
}
