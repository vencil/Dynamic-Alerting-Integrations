package config

// builtin_defaults.go — the thresholds the platform ships armed, compiled into
// every reader of conf.d (#3832).
//
// The threshold registry (rule-packs/threshold-registry.yaml) marks a key
// `arming: default` when the platform ships its value armed. The table of
// those keys and values is GENERATED into builtin_defaults_gen.go by
// `check_threshold_registry.py --regen`; `--ci` fails while it is stale. This
// file is the read access to it.
//
// ⛔ NOTHING RESOLVES A THRESHOLD FROM THIS TABLE YET. It is the source the
// built-in layer of #3832 will read; until that lands, a key a tree does not
// write has no value, exactly as before.
//
// Why a digest and not a version: the exporter, da-tools (da-guard) and
// tenant-api are released on lines of their own, and the rule packs have no
// release line at all, so "the same version" names nothing two of them can
// compare. Two components hold the same table when their digests are equal.

import (
	"crypto/sha256"
	"encoding/hex"
	"sort"
	"strconv"
	"strings"
)

// BuiltinDefaults is a copy of the built-in table: every default-armed
// threshold key and the platform's value for it.
func BuiltinDefaults() map[string]float64 {
	out := make(map[string]float64, len(builtinDefaults))
	for k, v := range builtinDefaults {
		out[k] = v
	}
	return out
}

// BuiltinDefault is the platform's value for key and whether the built-in
// table holds one. key is matched exactly: a `_critical` or a dimensional
// spelling of a default-armed key is not in the table.
func BuiltinDefault(key string) (float64, bool) {
	v, ok := builtinDefaults[key]
	return v, ok
}

// BuiltinDefaultsDigest names the built-in table this binary was compiled
// with (builtinDefaultsDigestOf of it).
func BuiltinDefaultsDigest() string { return builtinDefaultsDigest }

// builtinDefaultsDigestOf is the digest of a table: the first 12 hex digits
// of the SHA-256 of its `key=value` lines, sorted by key, each ended by a
// newline. A value is written without a fraction when it is integral (`80`),
// otherwise in the shortest form that reads back as the same number (`0.5`)
// — the text the generator writes, so the generated constant and this
// function agree on every table (TestBuiltinDefaultsDigest_IsOfTheTable).
func builtinDefaultsDigestOf(table map[string]float64) string {
	keys := make([]string, 0, len(table))
	for k := range table {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		b.WriteString(k)
		b.WriteByte('=')
		v := table[k]
		if v == 0 {
			v = 0 // a negative zero is written as the generator writes it: `0`
		}
		b.WriteString(strconv.FormatFloat(v, 'f', -1, 64))
		b.WriteByte('\n')
	}
	sum := sha256.Sum256([]byte(b.String()))
	return hex.EncodeToString(sum[:])[:12]
}
