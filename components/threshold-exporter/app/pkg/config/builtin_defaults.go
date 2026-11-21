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
// THE OFF TABLE (#4163). Every key the platform ships no value for
// (`arming: operator`, the generated unsetThresholdKeys) is in the layer too,
// with the value off (builtinOffDefault): the key is valued at the root, so
// a tenant may set it, a profile or a subtree may give it a value and a
// tenant's `<key>_critical` row is served (even with no base value of its
// own), exactly as for a key the root gives a number — but the base serves
// no row of its own, and off switches no family off. A customer tree no
// longer lists these keys to make them settable. Two rules differ from a
// valued key: `expires:` is not honoured (expiryHonoured: lapsing to off
// would take the threshold away in silence, PREVENT #656), and the table of
// values (BuiltinDefaults, BuiltinStatesOf, the generic chain) does not hold
// them — off is a typed-Defaults value only.
//
// THE ROOT OPT-OUT (#3832 decision 3). The root `defaults:` may write a key
// of the table as `disable` (RootDisableText: that word only, no alias, no
// schedule): the layer gives it nothing, and every tenant inherits the
// `disable` as it inherits a subtree level's — the key's whole family off
// (familyHeadOff) until a subtree, a profile or the tenant gives the key a
// value. In the typed Defaults it is rootDisabledDefault, a NaN no resolver
// serves a default row for and seedBuiltinDefaults does not replace; in the
// generic chain it is the text `disable`, which rootRefusedAsBuiltin keeps.
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
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

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

// BuiltinDefaultsDigest names the built-in layer this binary was compiled
// with (builtinDefaultsDigestOf of its table of values and its off table).
func BuiltinDefaultsDigest() string { return builtinDefaultsDigest }

// builtinDefaultsDigestOf is the digest of a layer: the first 12 hex digits
// of the SHA-256 of its `key=value` lines, sorted by key, each ended by a
// newline — one line per key of table, and `key=off` per key of off (#4163;
// the two never share a key). A value is written without a fraction when it
// is integral (`80`), otherwise in the shortest form that reads back as the
// same number (`0.5`) — the text the generator writes, so the generated
// constant and this function agree on every layer
// (TestBuiltinDefaultsDigest_IsOfTheTable).
func builtinDefaultsDigestOf(table map[string]float64, off []string) string {
	text := make(map[string]string, len(table)+len(off))
	for k, v := range table {
		if v == 0 {
			v = 0 // a negative zero is written as the generator writes it: `0`
		}
		text[k] = strconv.FormatFloat(v, 'f', -1, 64)
	}
	for _, k := range off {
		text[k] = "off"
	}
	keys := make([]string, 0, len(text))
	for k := range text {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	var b strings.Builder
	for _, k := range keys {
		b.WriteString(k)
		b.WriteByte('=')
		b.WriteString(text[k])
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
// and a refused text never leaves an alert off. A root `disable`
// (RootDisabledDefault) is kept: it is the tree's opt-out of the key. It returns the keys it gave
// the platform's value, sorted (FlatBuild.BuiltinSeeded).
func seedBuiltinDefaults(d map[string]float64) []string {
	var seeded []string
	for k, v := range builtinDefaults {
		if cur, ok := d[k]; ok && (finiteThreshold(cur) || RootDisabledDefault(cur)) {
			continue
		}
		d[k] = v
		seeded = append(seeded, k)
	}
	seedBuiltinOff(d)
	sort.Strings(seeded)
	return seeded
}

// builtinOffKeys is the built-in layer's off table (#4163): every threshold
// key the platform ships no value for (`arming: operator`), the keys of the
// generated unsetThresholdKeys — one table, so the keys the unset notice
// names and the keys the layer values off cannot differ.
var builtinOffKeys = func() map[string]struct{} {
	out := make(map[string]struct{}, len(unsetThresholdKeys))
	for _, e := range unsetThresholdKeys {
		out[e.key] = struct{}{}
	}
	return out
}()

// BuiltinOff reports whether key is in the built-in layer's off table.
// key is matched exactly, as BuiltinDefault matches it.
func BuiltinOff(key string) bool {
	_, ok := builtinOffKeys[key]
	return ok
}

// BuiltinOffKeys is the off table's keys, sorted.
func BuiltinOffKeys() []string {
	keys := make([]string, 0, len(builtinOffKeys))
	for k := range builtinOffKeys {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// seedBuiltinOff lays the off table under d in place: every off key d holds
// no value for, under ANY #1231 spelling, gets the off value (builtinOffNaN).
// Unlike the table of values, a key of this table may have a retired
// spelling (`mysql_cpu` → `mysql_threads_running`): a root that values the
// retired spelling values the key, and an off value under the canonical
// spelling beside it would take its place when the defaults are
// canonicalised. A value the root decode refused (refusedDefault) is KEPT,
// unlike above: it reads exactly as off (valued, no row of its own, no
// family off) and stays a refusal, so served-values and backtest still name
// root_default_refused for a root that mistyped a key the platform ships no
// value for — replacing it with off would leave the mistake silent.
func seedBuiltinOff(d map[string]float64) {
	held := make(map[string]bool, len(d))
	for k := range d {
		canon, _ := CanonicalKeyFor(k)
		held[canon] = true
	}
	for k := range builtinOffKeys {
		if held[k] {
			continue
		}
		d[k] = builtinOffDefault()
	}
}

// SeedBuiltinDefaults is seedBuiltinDefaults, for package main's flat
// incremental reload (config.go) and its single-file load, each of which
// asks RootBuiltinWithheld itself first. It returns the keys it seeded, which
// those loads hand to BuiltinStatesOf for the exporter's state gauge (#3831).
func SeedBuiltinDefaults(d map[string]float64) []string { return seedBuiltinDefaults(d) }

// The states of BuiltinDefaultState.State (#3832).
const (
	// BuiltinStateBuiltin: the tree's `defaults:` gives the key no value, so
	// the built-in layer gives the platform's.
	BuiltinStateBuiltin = "builtin"
	// BuiltinStateOverridden: the tree's `defaults:` gives the key a number,
	// which every tenant inherits in place of the platform's.
	BuiltinStateOverridden = "overridden"
	// BuiltinStateOptedOut: the tree's root `defaults:` writes the key as
	// `disable` (RootDisableText), which every tenant inherits.
	BuiltinStateOptedOut = "opted_out"
	// BuiltinStateWithheld: the root carrier cannot be read, so the layer is
	// withheld and the key has no value at the root (RootBuiltinWithheld).
	BuiltinStateWithheld = "withheld"
)

// BuiltinDefaultState is one key of ScopedTenants.BuiltinDefaultStates.
type BuiltinDefaultState struct {
	// Value is the platform's value (the built-in table's).
	Value float64
	// State is one of the BuiltinState* constants.
	State string
	// Root is the number `defaults:` gives the key, which every tenant
	// inherits; set for BuiltinStateOverridden only.
	Root *float64
	// Tenants is, for BuiltinStateBuiltin, how many tenants of the build
	// /metrics serves the platform's value for the key (servesBuiltinValue);
	// 0 otherwise.
	Tenants int
}

// servesBuiltinValue reports whether a tenant whose merged value for a
// built-in key is sv (ok false: none) is served the root's value for it, as
// resolveBaseRows decides: no value, or one that is neither a threshold nor
// a `disable` (the row falls back to the default, value_unparsed). The
// tenant's default is read; its time windows and `expires:` are not, so the
// answer does not depend on when it is asked.
func servesBuiltinValue(sv ScheduledValue, ok bool) bool {
	if !ok {
		return true
	}
	if off, _ := thresholdRowDisabled(sv.Default); off {
		return false
	}
	_, _, parsed := parseThresholdValue(sv.Default)
	return !parsed
}

// BuiltinDefaultStates is, per key of the built-in table, what the
// exporter's own build of the tree does with it (FlatBuild.BuiltinSeeded and
// BuiltinWithheld, read off the same build as Config): the tree-level answer
// beneath every tenant's key_sources, with the number of tenants /metrics
// serves the built-in value. nil when no build ran (#3832; validate-config's
// platform_defaults row).
func (s *ScopedTenants) BuiltinDefaultStates() map[string]BuiltinDefaultState {
	if s == nil || s.built == nil {
		return nil
	}
	b := s.built
	return BuiltinStatesOf(&b.Config, b.BuiltinSeeded, b.BuiltinWithheld)
}

// BuiltinStatesOf is, per key of the built-in table, what one build of a
// tree did with it: cfg is the build's merged config (its Defaults after the
// built-in layer was laid under them), seeded the keys that layer gave the
// platform's value (seedBuiltinDefaults' answer) and withheld whether the
// layer was withheld (RootBuiltinWithheld). It is the one derivation
// behind both `da-guard effective`'s builtin_defaults
// (ScopedTenants.BuiltinDefaultStates) and the exporter's
// da_config_builtin_default_state gauge (#3831), so the two cannot name a
// key's state differently.
func BuiltinStatesOf(cfg *ThresholdConfig, seeded []string, withheld bool) map[string]BuiltinDefaultState {
	out := make(map[string]BuiltinDefaultState, len(builtinDefaults))
	for k, v := range builtinDefaults {
		st := BuiltinDefaultState{Value: v, State: BuiltinStateWithheld}
		switch {
		case withheld:
		case slices.Contains(seeded, k):
			st.State = BuiltinStateBuiltin
			for _, overrides := range cfg.Tenants {
				sv, ok := overrides[k]
				if servesBuiltinValue(sv, ok) {
					st.Tenants++
				}
			}
		case RootDisabledDefault(cfg.Defaults[k]):
			st.State = BuiltinStateOptedOut
		default:
			root := cfg.Defaults[k]
			st.State, st.Root = BuiltinStateOverridden, &root
		}
		out[k] = st
	}
	return out
}

// BuiltinStateOff: the tree's `defaults:` gives a key of the off table no
// value (not written, null, or a value the root decode refuses), so the
// built-in layer values it off (#4163).
const BuiltinStateOff = "off"

// BuiltinOffState is one key of ScopedTenants.BuiltinOffStatesAt (#4163).
type BuiltinOffState struct {
	// Pack is the rule pack whose rules read the key.
	Pack string
	// State is BuiltinStateOff, BuiltinStateOverridden (the root gives it a
	// number, in Root), BuiltinStateOptedOut (the root writes `disable`) or
	// BuiltinStateWithheld (the root carrier cannot be read).
	State string
	// Root is the number `defaults:` gives the key; BuiltinStateOverridden
	// only.
	Root *float64
	// Configured is how many tenants of the build serve the key a threshold
	// or switch it off themselves at some part of the UTC day of at — the
	// unset notice's own reading (UnsetThresholdsAt), every tenant asked.
	Configured int
}

// BuiltinOffStatesAt is, per key of the off table, what the exporter's own
// build of the tree does with it and how many of its tenants configure it at
// at: the tree-level answer beneath validate-config's platform_defaults row
// for the keys the platform ships no value for. nil when no build ran.
func (s *ScopedTenants) BuiltinOffStatesAt(at time.Time) map[string]BuiltinOffState {
	if s == nil || s.built == nil {
		return nil
	}
	cfg := &s.built.Config
	everyone := make(map[string]bool, len(cfg.Tenants))
	for t := range cfg.Tenants {
		everyone[t] = true
	}
	unset := map[string]int{}
	if !s.built.BuiltinWithheld {
		for _, u := range cfg.UnsetThresholdsAt(at, everyone) {
			unset[u.Key]++
		}
	}
	out := make(map[string]BuiltinOffState, len(unsetThresholdKeys))
	for _, e := range unsetThresholdKeys {
		st := BuiltinOffState{Pack: e.pack, State: BuiltinStateWithheld}
		if !s.built.BuiltinWithheld {
			v := cfg.Defaults[e.key]
			switch {
			case RootDisabledDefault(v):
				st.State = BuiltinStateOptedOut
			case finiteThreshold(v):
				root := v
				st.State, st.Root = BuiltinStateOverridden, &root
			default:
				st.State = BuiltinStateOff
			}
			st.Configured = len(cfg.Tenants) - unset[e.key]
		}
		out[e.key] = st
	}
	return out
}

// BuiltinStates is the closed set of BuiltinDefaultState.State, in the
// order da_config_builtin_default_state's series are created (#3831).
func BuiltinStates() []string {
	return []string{BuiltinStateBuiltin, BuiltinStateOverridden, BuiltinStateOptedOut, BuiltinStateWithheld}
}

// BuiltinDefaultKeys is the built-in table's keys, sorted.
func BuiltinDefaultKeys() []string {
	keys := make([]string, 0, len(builtinDefaults))
	for k := range builtinDefaults {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// InheritedRootDisables is, sorted, the built-in keys the root `defaults:`
// writes as `disable` (RootDisabledDefault, #3832 decision 3) whose family
// tenant's merged config leaves to that `disable`: it writes the key under
// no spelling, or writes a value the base phase does not serve in some part
// of the day — one past its `expires:` at at, or one whose default or some
// time window is no number it reads (a `disable` of its own included). A
// tenant whose value is a number it reads all day has the family back and
// is not listed. (Built-in keys have no #1231 alias spelling,
// TestBuiltinDefaults_NoAliasSpelling, so the tenant writes one spelling.) da-guard reads each key listed as
// a `disable` of the tenant's (served-values keys[], --disable-effects),
// judged against the built-in value, as it judges a tenant's own `disable`
// against the platform default.
func (c *ThresholdConfig) InheritedRootDisables(tenant string, at time.Time) []string {
	var out []string
	for k, v := range c.Defaults {
		if !RootDisabledDefault(v) {
			continue
		}
		var sv ScheduledValue
		written := false
		for tk, tv := range c.Tenants[tenant] {
			if canon, _ := CanonicalKeyFor(tk); canon == k {
				sv, written = tv, true
				break
			}
		}
		if written && !isThresholdExpired(sv, at) && servesNumberAllDay(sv) {
			continue
		}
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// servesNumberAllDay reports whether every part of sv's UTC day
// (DaySegments: its default and each time window) is a number the base
// phase reads — so no part of the day leaves the family to the root.
func servesNumberAllDay(sv ScheduledValue) bool {
	for _, seg := range sv.DaySegments() {
		if _, _, parsed := parseThresholdValue(seg.Value); !parsed {
			return false
		}
	}
	return true
}

// BuiltinDefaultText is the platform's value for built-in key key as a
// threshold text (the generator's spelling: `80`, `0.5`); "" when the
// table holds no such key.
func BuiltinDefaultText(key string) string {
	v, ok := builtinDefaults[key]
	if !ok {
		return ""
	}
	return strconv.FormatFloat(v, 'f', -1, 64)
}

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

// fillBuiltinUnder lays the built-in layer under m, a defaults chain folded
// without it, in place: every built-in key m does not hold gets the
// platform's value. It is the fold with the layer as its first input — a
// level that writes a key (any value, a schedule included) replaces it, and
// one that writes null or a schedule writing nothing leaves it (deepMerge) —
// without copying the layer through every level of every tenant's chain.
func fillBuiltinUnder(m map[string]any) {
	for k, v := range builtinDefaults {
		if _, ok := m[k]; !ok {
			m[k] = v
		}
	}
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
