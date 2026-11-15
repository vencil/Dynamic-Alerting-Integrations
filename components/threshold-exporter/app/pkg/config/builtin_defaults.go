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
// THE BUILT-IN LAYER (#3832). The table is the bottom of every reader's
// merge, below the conf.d root's `defaults:`. One rule decides all of its
// behaviour: a value the built-in layer gives has the effect of the same
// number written under the root `defaults:`; only the source differs
// (key_sources `layer: builtin`, no file). So a key the root gives a number
// takes the root's number; a key the root does not write, writes as null, or
// writes as a value the root decode refuses takes the built-in value; and
// every rule that holds for a root-valued key (a subtree or tenant `disable`,
// a tenant's `<key>_critical` row, the key verdict) holds for it.
//
// Both representations of the defaults take it in, from this file only:
//
//   - the typed ThresholdConfig.Defaults (/metrics, served-values, the
//     tenant-api write check): seedBuiltinDefaults, after the root carrier is
//     merged;
//   - the generic defaults chain (/effective, merged_hash, key_sources,
//     /simulate): builtinChainBase under the fold (foldDefaults' first
//     input) and under the view (viewChainBlocks).
//
// ⛔ WITHHELD WHEN THE ROOT CANNOT BE READ. A root carrier that exists but
// does not parse, or cannot be stat'ed or read (TreeScan.RootDefaultsUnreadable),
// may hold the overrides and opt-outs of this table; serving the platform's
// values then would switch on alerts the tree switched off. The tree serves
// no default row at all, as before the built-in layer. A tree with no root
// carrier, or one whose `defaults:` is empty, is a legal tree and gets the
// whole table.
//
// ⚠️ No key of the table may have a #1231 alias spelling
// (TestBuiltinDefaults_NoAliasSpelling): the spelling machinery of the chain
// (dropShallowerSpellings and the like) is not taught a level with no file.
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

	"github.com/vencil/threshold-exporter/pkg/confdname"
)

// builtinDefaultEntry is one line of the generated table.
type builtinDefaultEntry struct {
	key   string
	value float64
}

// builtinDefaults is the generated table by key.
var builtinDefaults = builtinDefaultsOf(builtinDefaultEntries)

func builtinDefaultsOf(entries []builtinDefaultEntry) map[string]float64 {
	out := make(map[string]float64, len(entries))
	for _, e := range entries {
		out[e.key] = e.value
	}
	return out
}

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

// seedBuiltinDefaults lays the built-in layer under d, a merged root
// `defaults:` (ThresholdConfig.Defaults), in place: every built-in key d
// holds no finite value for gets the platform's value. A value the root
// decode refused (a NaN, refusedDefault) falls to the built-in value: the
// refusal is still reported, from the file's bytes (RootDefaultRefusals),
// and a refused text never leaves an alert off.
func seedBuiltinDefaults(d map[string]float64) {
	for k, v := range builtinDefaults {
		if cur, ok := d[k]; ok && finiteThreshold(cur) {
			continue
		}
		d[k] = v
	}
}

// SeedBuiltinDefaults is seedBuiltinDefaults, for package main's flat
// incremental reload (config.go) and its single-file load, each of which
// asks RootBuiltinWithheld itself first.
func SeedBuiltinDefaults(d map[string]float64) { seedBuiltinDefaults(d) }

// builtinChainBase is the built-in layer as a defaults-chain block
// (map[string]any, values float64), a fresh map per call; nil when builtin
// is false (the layer is withheld).
func builtinChainBase(builtin bool) map[string]any {
	if !builtin {
		return nil
	}
	out := make(map[string]any, len(builtinDefaults))
	for k, v := range builtinDefaults {
		out[k] = v
	}
	return out
}

// RootDefaultsUnreadable reports whether the scan holds, at the conf.d root,
// a defaults carrier it could not stat or read (an Unreadable entry whose
// base name is a carrier's: a dangling or looping link, a file it may not
// read, a special file). The built-in layer is withheld from such a tree
// (see the top of this file). A root carrier that reads but does not parse
// is the build's own verdict (FlatBuild.RootDefaultsUnjudged), not this.
func (s *TreeScan) RootDefaultsUnreadable() bool {
	if s == nil {
		return false
	}
	for _, u := range s.Unreadable {
		if !strings.Contains(u.RelKey, "/") && confdname.IsDefaults(u.RelKey) {
			return true
		}
	}
	return false
}

// RootBuiltinWithheld reports whether the built-in layer is withheld from
// the tree the scan walked: the root holds a defaults carrier the walk could
// not stat or read (RootDefaultsUnreadable), or the selected root carrier is
// one the exporter's build drops whole (DropsConfFile: a syntax error, or a
// document its typed decode refuses, such as a list where a number goes).
// Every reader asks this one predicate, so /metrics, /effective, the
// tenant-api write check and the exporter's merged_hash withhold together.
// A carrier whose bytes the scan did not keep is read from disk; one that
// cannot be read then withholds too.
func (s *TreeScan) RootBuiltinWithheld() bool {
	if s == nil {
		return false
	}
	if s.RootDefaultsUnreadable() {
		return true
	}
	key := selectedRootCarrierKey(s)
	if key == "" {
		return false
	}
	f := s.Files[key]
	if f == nil {
		return false
	}
	data := f.Data
	if data == nil {
		b, err := ReadConfFile(f.AbsPath)
		if err != nil {
			return true
		}
		data = b
	}
	return DropsConfFile(key, data, true)
}

// rootRefusedAsBuiltin is the chain's blocks as the effective view and
// key_sources read them: the root level's (rootLevel; -1 = none) built-in
// keys whose value the root decode refuses taken out, so the built-in value
// shows through, as /metrics serves it (seedBuiltinDefaults). blocks itself
// when builtin is false or nothing is taken out; a block that changes is
// copied, blocks is not written.
func rootRefusedAsBuiltin(blocks []map[string]any, rootLevel int, builtin bool) []map[string]any {
	if !builtin || rootLevel < 0 || rootLevel >= len(blocks) {
		return blocks
	}
	b := withoutRefusedBuiltin(blocks[rootLevel])
	if len(b) == len(blocks[rootLevel]) {
		return blocks
	}
	out := append([]map[string]any(nil), blocks...)
	out[rootLevel] = b
	return out
}

// withBuiltinBase is the chain's blocks with the built-in layer laid first
// (builtinChainBase), what effectiveView lays under the tenant side; blocks
// itself when builtin is false.
func withBuiltinBase(blocks []map[string]any, builtin bool) []map[string]any {
	if !builtin {
		return blocks
	}
	return append([]map[string]any{builtinChainBase(true)}, blocks...)
}

// withoutRefusedBuiltin is a root block without its built-in keys whose
// value the root decode refuses (refusedRootThresholds); block itself when
// there is none.
func withoutRefusedBuiltin(block map[string]any) map[string]any {
	refused := refusedBuiltinKeys(block)
	if len(refused) == 0 {
		return block
	}
	out := make(map[string]any, len(block))
	for k, v := range block {
		if !refused[k] {
			out[k] = v
		}
	}
	return out
}

// refusedBuiltinKeys is the built-in keys of a root block whose value the
// root decode refuses; nil when none.
func refusedBuiltinKeys(block map[string]any) map[string]bool {
	var out map[string]bool
	for k := range refusedRootThresholds(block) {
		if _, ok := builtinDefaults[k]; ok {
			if out == nil {
				out = map[string]bool{}
			}
			out[k] = true
		}
	}
	return out
}
