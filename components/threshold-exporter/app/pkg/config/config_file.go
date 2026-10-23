package config

import (
	"fmt"
	"sort"
	"strings"
	"unicode/utf8"

	"gopkg.in/yaml.v3"
)

// ParseConfigFile is the ONE decode of a conf.d file's bytes into a
// ThresholdConfig (#1957). Every plane that asks "is this file valid, and
// which tenants does it declare" asks it here:
//
//   - the conf.d walker (ScanDirTree → parseTenantDecls): a file this
//     rejects declares NO tenants, so it is absent from Tenants / Locate and
//     therefore from ResolveEffective (tenant-api /effective), ScopeEffective
//     (da-guard) and the exporter's hierarchy plane;
//   - the exporter's flat plane (package main's parsePartialConfig, and the
//     decoded partials the walker hands over in TreeScan.Partials), which
//     builds /metrics.
//
// ⛔ WHY ONE FUNCTION. Before #1957 the walker decoded only the shape it
// needed (`tenants:` into map[string]yaml.Node) while the flat plane decoded
// the whole ThresholdConfig, so the two disagreed on every file the lenient
// decode accepted and the full one rejected: a tenant whose body is a scalar,
// or a sibling `defaults:` / `max_metrics_per_tenant:` block of the wrong
// shape. Such a tenant resolved through /effective while /metrics never
// served it, and the exporter's divergence gauge (cause "a", removed by
// #1957) existed to report it after the fact. With one decode the same file
// content gets the same verdict on every plane.
//
// ⚠️ THAT IS A STATEMENT ABOUT ONE FILE'S BYTES, NOT "THE PLANES ALWAYS HOLD
// THE SAME TENANTS". One known exception, pre-existing and tracked (a second,
// the exporter's incremental tenant-only reload keeping a now-rejected file's
// last good values, was removed by #1980 — package main's
// TestOneTenantSet_IncrementalDropsABrokenFileLikeTheStatelessReaders pins
// that it agrees with ResolveEffective / ScopeEffective now):
//   - a `_`-prefixed platform file declaring `tenants:` is never parsed for
//     tenants by the walker, but the flat plane merges its `tenants:` block,
//     so /metrics serves a tenant /effective does not know — #1982.
//
// Semantics are a plain yaml.Unmarshal into ThresholdConfig — the flat
// plane's historical decode — pinned against that oracle over a variant
// corpus by config_file_test.go, with FOUR post-decode steps: a reserved
// key's mapping is kept whole even when it has a `default` key
// (keepReservedMappings, #2859), a value written
// as null is no write (dropNullThresholds, #2518 — a `defaults:` key, and a
// threshold key of a `tenants:` / `profiles:` body), inside `defaults:`
// a spelling that writes nothing beside one that does is dropped
// (dropNullShadowingSpellings, #2418), and a reserved key written with a
// schedule keeps only its `default` — the part its readers read
// (stripScheduledReserved, #3269; recorded in ScheduledReservedEntries). The
// tenant set of a file is the key set of the returned Tenants. It also
// records, without changing any value, which entries write `_metadata` as a
// string (StringMetadataEntries, #2859).
//
// ⚠️ A TENANT FILE ASKS ParseTenantFile, NOT THIS. This accepts a tenant id
// that is not valid UTF-8, and a document only the typed decode accepts
// (#3063). A `_`-prefixed platform file keeps both (see ParseTenantFile);
// a file that DECLARES tenants must not.
//
// ⛔ parse-tree's verdict first (ADR-036 step 6, #3206; parse_verdict.go): a
// file parsetree.Parse refuses is refused here, before either decode, with an
// *UnreadableFileError naming its kind — so its error text is parse-tree's,
// also for what yaml.v3 refuses on its own (a syntax error, a repeated key).
// parseConfigFile below is the decode alone, for tests that need it.
func ParseConfigFile(data []byte) (ThresholdConfig, error) {
	if err := judgeConfFile(data); err != nil {
		return ThresholdConfig{}, err
	}
	return parseConfigFile(data, false)
}

// parseConfigFile is ParseConfigFile; generic additionally requires the
// generic decode of the same parsed node to succeed (ParseTenantFile, #3063),
// before the post-decode steps run.
// It does NOT ask parse-tree (ParseConfigFile and ParseTenantFile do, first).
func parseConfigFile(data []byte, generic bool) (ThresholdConfig, error) {
	cfg, err := decodeConfigFile(data, generic)
	if err == nil {
		// #3269: a reserved key's schedule is read by no one; what every
		// reader of this decode sees is its `default` alone, and where the
		// file wrote one is recorded for the load's WARN.
		if sr := append(stripScheduledReserved("tenants", cfg.Tenants),
			stripScheduledReserved("profiles", cfg.Profiles)...); sr != nil {
			if cfg.notes == nil {
				cfg.notes = &fileNotes{}
			}
			cfg.notes.scheduledReserved = sr
		}
	}
	return cfg, err
}

// decodeConfigFile is parseConfigFile before its last step: the reserved
// keys' schedules are still in the values, as written. For the one reader
// that judges what a tenant wrote rather than what is served —
// MergeTenantOverRootPlatform's tenant layer, which its ValidateTenantKeys
// judges as the write gate judges a body (#3269).
func decodeConfigFile(data []byte, generic bool) (ThresholdConfig, error) {
	var cfg ThresholdConfig
	// Through a yaml.Node rather than straight into cfg: yaml.Unmarshal builds
	// that node anyway, so the typed decode costs the same, and the node still
	// shows what the typed decode erases — whether `_metadata` was written as
	// a string (stringMetadataEntries, #2859).
	var doc yaml.Node
	err := yaml.Unmarshal(data, &doc)
	if err == nil && doc.Kind != 0 {
		err = doc.Decode(&cfg)
		if err == nil && generic {
			// The typed decode's error, when there is one, is the message:
			// this runs only on a node the typed decode accepted.
			var raw any
			if err = doc.Decode(&raw); err != nil {
				return ThresholdConfig{}, err
			}
		}
	}
	if err == nil {
		if sm := stringMetadataEntries(&doc); sm != nil {
			cfg.notes = &fileNotes{stringMetadata: sm}
		}
		keepReservedMappings(&cfg, data)
		dropNullThresholds(&cfg, data)
		dropNullShadowingSpellings(cfg.Defaults, data)
		normalizeConfigKeys(&cfg)
	}
	return cfg, err
}

// normalizeConfigKeys re-keys every threshold map of one file's decode to the
// canonical dimensional spelling (normalizeKeys, #2031) — after the null
// drops above, which look keys up by the text the file wrote. The maps keep
// their identity when nothing is re-spelled.
func normalizeConfigKeys(cfg *ThresholdConfig) {
	var s *keySpellings
	cfg.Defaults, s = normalizeKeys(cfg.Defaults)
	tenants, profiles := normalizeBodies(cfg.Tenants), normalizeBodies(cfg.Profiles)
	cfg.spelled = s != nil || tenants || profiles
}

// normalizeBodies is normalizeKeys over each body of m, in place; it reports
// whether any body was re-spelled.
func normalizeBodies(m map[string]map[string]ScheduledValue) bool {
	spelled := false
	for name, body := range m {
		nb, s := normalizeKeys(body)
		if s != nil {
			m[name], spelled = nb, true
		}
	}
	return spelled
}

// dropNullShadowingSpellings removes from a decoded `defaults:` map every
// spelling of a threshold that the file does not write (null, ±Inf, NaN —
// levelWritesSpelling) while it writes another spelling of the same
// threshold (#2418).
//
// ⛔ WHY. `Defaults` is map[string]float64, so a null decodes to a PRESENT 0
// (since #2518 dropNullThresholds takes every null out first; the ±Inf /
// NaN half below is what still reaches here). With the canonical spelling
// written as null beside the retired spelling
// at 30 in the root `_defaults.yaml`, resolve's canonical-wins dedup
// (canonicalizeDefaults) then served the null's 0 over the 30, while the
// walker's defaults fold — which da-guard and /effective read — drops the
// null and keeps the 30. The guard therefore judged a tenant writing the
// retired spelling at 30 redundant, and deleting it moved /metrics from 30
// to 0.
//
// "Does this level write spelling s" is levelWritesSpelling, the predicate
// the subtree overlay (applySubtreeDefaults) and the walker's fold
// (noteSpellingWriters) already share: the root level answers it the same
// way, so the canonical-wins dedup inside one file is among the spellings
// that file WRITES.
//
// What this changes, all of it:
//   - a null beside the other spelling's value: served that value, not 0;
//   - `.inf` / `-.inf` / `.nan` beside the other spelling's value: served
//     that value, not ±Inf / NaN (a non-finite number is not threshold-
//     shaped, so it writes nothing either);
//   - every caller of this decode gets it — the conf.d root carrier AND
//     file mode's single config file (loadFile → ParseTenantFile).
//
// What stays: a non-finite spelling with NO written twin decodes as before,
// and a file writing both spellings with values is untouched (canonical
// wins). A null never reaches here: dropNullThresholds runs first and takes
// every null out of `defaults:` (#2518), twin or not.
//
// Fast path: the raw re-decode happens only when the map holds two
// spellings of one threshold — never for a file without aliased keys.
func dropNullShadowingSpellings(defaults map[string]float64, data []byte) {
	var pairs []string
	var buf [2]string
	for k := range defaults {
		for _, s := range otherSpellings(k, &buf) {
			if _, both := defaults[s]; both {
				pairs = append(pairs, k)
				break
			}
		}
	}
	if len(pairs) == 0 {
		return
	}
	var raw struct {
		Defaults map[string]any `yaml:"defaults"`
	}
	if yaml.Unmarshal(data, &raw) != nil {
		return // cannot happen: the typed decode of the same bytes succeeded
	}
	var drop []string
	for _, k := range pairs {
		if levelWritesSpelling(raw.Defaults, k) {
			continue
		}
		for _, s := range otherSpellings(k, &buf) {
			if _, in := defaults[s]; in && levelWritesSpelling(raw.Defaults, s) {
				drop = append(drop, k)
				break
			}
		}
	}
	for _, k := range drop {
		delete(defaults, k)
	}
}

// ParseTenantFile is ParseConfigFile for a file that declares tenants — a
// non-`_` conf.d file (the walker's parseTenantDecls) or the single config
// file of file mode — plus two rejections: a tenant id that is not valid
// UTF-8 (#2266; YAML can spell one with `!!binary`), and a document whose
// generic decode fails (#3063). The non-UTF-8 error names every such id,
// sorted, so the message is the same on every run.
//
// ⛔ WHY THE GENERIC DECODE (#3063). The typed decode is not the strictest
// reader of a tenant file. ScheduledValue's scalar branch keeps the scalar's
// text and never resolves its tag, and a top-level key ThresholdConfig has no
// field for is skipped unread, so `mysql_connections: !!int zzleak42` — an
// explicit tag the text does not satisfy — decoded fine here while every
// other reader of the same bytes refused it: the merge plane's
// decodeTenantFile (merged_hash, /effective, da-guard), customalerts.Extract
// (tenant-api), and the route generator (int() of the text). The file was
// then served on /metrics with the threshold at its default and nowhere
// else. The judgement added here is decodeTenantFile's own — the same
// yaml.Node decoded into `any` — not a third one (a per-scalar tag check
// would cover the scalar branch and still miss the skipped top-level key).
//
// So the criterion is "the generic decode fails", and the mismatched tag is
// only its commonest case. Everything else the generic decode refuses and
// the typed decode lets through is rejected too, most of it inside a
// top-level key with no field (which the typed decode never visits): a
// duplicate key under an `x-anchors:` helper block, an unhashable key there,
// a merge of a scalar (`<<: 5`), a broken tag (`!!float abc`,
// `!!bool maybe`, `!!binary '@@@'`), a self-referencing alias
// (`extra: &a {b: *a}`). A file whose generic decode succeeds — a tag the
// text satisfies (`!!int 5`, `!!str 5`, `!!float 1.5`, `!!bool true`)
// included — is accepted as before.
//
// The whole file is rejected, not the one tenant whose block holds the
// scalar: decodeTenantFile fails for the whole document, so the merge plane
// has no tenant of it either, and rejecting the file keeps both planes on
// the same tenant set.
//
// Cost: one more decode of the node yaml.Unmarshal already built — no second
// parse of the bytes. It is paid where a tenant file is judged: the walker
// (a file whose bytes changed; every file on a cold scan) and tenant-api's
// tenantConfigError / loadAllTenants — never on the /metrics scrape.
//
// ⚠️ A `_` platform file keeps ParseConfigFile's verdict (WHY NOT IN
// ParseConfigFile below): rejecting one drops every tenant's defaults, a
// different decision from this one, which #3063 did not take (#3131).
//
// ⛔ WHY REJECT THE FILE, NOT THE TENANT. A declared tenant id becomes a
// Prometheus label value, and client_golang's WithLabelValues panics on
// non-UTF-8 — on the scrape goroutine, which has no recover, so the whole
// exporter died. Rejecting the file gives it the verdict every other bad
// tenant file gets (skipped whole, WARN, da_config_parse_failure_total), and
// since the walker's verdict is the flat plane's too, on every plane.
//
// ⛔ WHY NOT IN ParseConfigFile. A `_` platform file's `tenants:` entry names
// a tenant; it never declares one — the merge keeps an entry only for a
// tenant some tenant file declares (declaredTenantIDs), and the platform
// overlay is looked up by declared id. A non-UTF-8 key there therefore can
// never reach a label (every declared id passed this check) and is dropped
// and WARNed as an orphan (reportPlatformOrphans). Rejecting the whole
// platform file instead dropped every tenant's defaults with it (#2266
// blind review, F1).
//
// ⛔ parse-tree's verdict first, as in ParseConfigFile (ADR-036 step 6,
// #3206). It refuses everything the generic decode refuses (parse-tree decodes
// the whole document into `any` too) and more — a second document, a tag
// outside its allowed set (`!custom` included, which both decodes read as a
// string), a byte order mark past the start, a null key — so the #3063 decode
// below now refuses nothing parse-tree lets through; it stays as the decode
// this function's verdict was built on.
func ParseTenantFile(data []byte) (ThresholdConfig, error) {
	if err := judgeConfFile(data); err != nil {
		return ThresholdConfig{}, err
	}
	return parseTenantFileDecode(data)
}

// parseTenantFileDecode is ParseTenantFile without parse-tree's verdict: the
// typed and generic decodes and the non-UTF-8 id rejection. For tests.
func parseTenantFileDecode(data []byte) (ThresholdConfig, error) {
	cfg, err := parseConfigFile(data, true)
	if err != nil {
		return cfg, err
	}
	var bad []string
	for tid := range cfg.Tenants {
		if !utf8.ValidString(tid) {
			bad = append(bad, tid)
		}
	}
	if len(bad) > 0 {
		sort.Strings(bad)
		if len(bad) > 1 {
			return ThresholdConfig{}, fmt.Errorf("tenant ids %q are not valid UTF-8", bad)
		}
		return ThresholdConfig{}, fmt.Errorf("tenant id %q is not valid UTF-8", bad[0])
	}
	return cfg, nil
}

// keepReservedMappings undoes ScheduledValue's structured decode for reserved
// keys (#2859). ScheduledValue.UnmarshalYAML cannot see the key its value sits
// under, so a mapping with a `default` key is decoded as a scheduled
// threshold — {default, overrides, expires, reason} — and every other key is
// dropped. For a threshold that is the format; for a reserved key such as
// `_metadata: {default: x, environment: production}` it threw environment
// away, while the same mapping without `default` kept it. A reserved key's
// mapping is never a scheduled value, so it is stored the way every other
// mapping is: re-serialized whole into Default (the arbitrary-mapping branch).
//
// Only the reserved keys whose value is a mapping by schema
// (mappingReservedKeys). A name reference such as `_profile` keeps its
// `{default: <name>}` form, which elects that name (#2433), and keys whose
// schema admits a string or a mapping keep their decode.
//
// The bytes are re-read generically, as dropNullThresholds does, and only
// when the typed decode shows a candidate (mayHaveStructuredReserved): a
// second decode of every file doubled the cost of a cold scan.
func keepReservedMappings(cfg *ThresholdConfig, data []byte) {
	if !mayHaveStructuredReserved(cfg.Tenants) && !mayHaveStructuredReserved(cfg.Profiles) {
		return
	}
	var raw struct {
		Tenants  map[string]map[string]any `yaml:"tenants"`
		Profiles map[string]map[string]any `yaml:"profiles"`
	}
	if yaml.Unmarshal(data, &raw) != nil {
		// Cannot happen for a tenant file: ParseTenantFile's generic decode
		// of the same node succeeded first (#3063). A file read through
		// ParseConfigFile alone can get here — a scalar in a body whose
		// explicit tag its text does not satisfy (the typed decode never
		// resolves it) — and keeps the typed decode, as before.
		return
	}
	keepReservedBodies(cfg.Tenants, raw.Tenants)
	keepReservedBodies(cfg.Profiles, raw.Profiles)
}

func keepReservedBodies(typed map[string]map[string]ScheduledValue, raw map[string]map[string]any) {
	for name, body := range raw {
		if typed[name] == nil {
			continue
		}
		for k, v := range body {
			if !mappingReservedKeys[k] || !hasDefaultKey(v) {
				continue // not a reserved mapping the structured branch took
			}
			out, err := yaml.Marshal(v)
			if err != nil {
				continue // not reachable for a mapping decoded from YAML
			}
			typed[name][k] = ScheduledValue{Default: string(out)}
		}
	}
}

// hasDefaultKey reports whether v is a mapping with a `default` key, in
// either shape a generic decode gives a mapping.
func hasDefaultKey(v any) bool {
	switch m := v.(type) {
	case map[string]any:
		_, ok := m["default"]
		return ok
	case map[any]any:
		_, ok := m["default"]
		return ok
	}
	return false
}

// mappingReservedKeys are the reserved keys whose value is a mapping and
// only a mapping (docs/schemas/tenant-config.schema.json): the ones
// keepReservedMappings keeps whole.
var mappingReservedKeys = map[string]bool{
	"_metadata":         true,
	"_routing":          true,
	"_routing_enforced": true,
	"_routing_defaults": true,
}

// mayHaveStructuredReserved reports whether a body holds a mappingReservedKeys
// value that the structured branch may have decoded. A mapping kept whole is
// re-serialized YAML, so its text has a `:`; the structured branch leaves the
// `default` scalar's text, and may leave overrides or an expiry. A `default`
// whose own text has a `:` beside no overrides is missed — accepted, against
// a second decode of every file.
func mayHaveStructuredReserved(bodies map[string]map[string]ScheduledValue) bool {
	for _, body := range bodies {
		for k := range mappingReservedKeys {
			sv, ok := body[k]
			if !ok {
				continue
			}
			if sv.Overrides != nil || sv.Expiry != nil || !strings.Contains(sv.Default, ":") {
				return true
			}
		}
	}
	return false
}

// StringMetadataEntries is the file's `tenants:` / `profiles:` entries that
// write `_metadata` as a string, as "tenants.<id>" / "profiles.<name>"
// (stringMetadataEntries). Set by ParseConfigFile only; nil for a merged
// config.
func (c *ThresholdConfig) StringMetadataEntries() []string {
	if c.notes == nil {
		return nil
	}
	return c.notes.stringMetadata
}

// TenantWritesStringMetadata reports whether a tenant config document writes
// tenants.<tenantID>._metadata as a string. The write side refuses that
// (#2859): the schema says `_metadata` is a mapping, and after the decode a
// string that reads as YAML is indistinguishable from one. Bytes that do not
// parse report false; the caller's own decode is what refuses them.
func TenantWritesStringMetadata(data []byte, tenantID string) bool {
	var doc yaml.Node
	if yaml.Unmarshal(data, &doc) != nil {
		return false
	}
	want := "tenants." + tenantID
	for _, e := range stringMetadataEntries(&doc) {
		if e == want {
			return true
		}
	}
	return false
}

// stringMetadataEntries lists the `tenants:` entries, then the `profiles:`
// ones, each in document order (merged ones last, effectivePairs), of a
// decoded document whose effective `_metadata` is a non-null scalar:
// "tenants.<id>" / "profiles.<name>".
//
// WHY IT READS THE NODE. ScheduledValue stores a scalar as its text and a
// mapping as its re-serialized YAML, both in Default, so after the typed
// decode `_metadata: "environment: production"` and `_metadata:
// {environment: production}` are the same value. The exporter keeps reading
// both (a load-side refusal would drop every tenant of the file); this list
// is what lets it say which entries still use the string form.
func stringMetadataEntries(doc *yaml.Node) []string {
	root := derefAlias(doc)
	if root.Kind == yaml.DocumentNode {
		if len(root.Content) == 0 {
			return nil
		}
		root = derefAlias(root.Content[0])
	}
	if root.Kind != yaml.MappingNode {
		return nil
	}
	var out []string
	for _, section := range []string{"tenants", "profiles"} {
		entries := effectiveValue(root, section, 0)
		if entries == nil || entries.Kind != yaml.MappingNode {
			continue
		}
		visit := func(name string, value *yaml.Node) {
			body := derefAlias(value)
			if body.Kind != yaml.MappingNode {
				return
			}
			if v := effectiveValue(body, "_metadata", 0); v != nil && v.Kind == yaml.ScalarNode && v.ShortTag() != "!!null" {
				out = append(out, section+"."+name)
			}
		}
		if !hasMergeKey(entries) {
			// The common shape, walked without allocating: no merge key, so
			// the effective pairs are the written ones (duplicates are a
			// decode error).
			for i := 0; i+1 < len(entries.Content); i += 2 {
				visit(entries.Content[i].Value, entries.Content[i+1])
			}
			continue
		}
		for _, e := range effectivePairs(entries, 0) {
			visit(e.key, e.value)
		}
	}
	return out
}

// maxMergeDepth bounds how deep effectivePairs follows merge keys into
// merged mappings; a real config nests one or two.
const maxMergeDepth = 16

type nodePair struct {
	key   string
	value *yaml.Node
}

// effectivePairs is mapping m's keys as the typed decode sees them (#2859
// review): every explicit key, in document order, then each key a merge key
// (`<<`) brings in that no explicit key and no earlier merge source already
// set. That is yaml.v3's precedence: an explicit key wins over a merged one,
// and among the sources of `<<: [*a, *b]` the earlier wins. Without it,
// `<<: {_metadata: "owner: x"}` decoded as a string `_metadata` while this
// list did not name it.
func effectivePairs(m *yaml.Node, depth int) []nodePair {
	var out []nodePair
	seen := map[string]bool{}
	var merges []*yaml.Node
	for i := 0; i+1 < len(m.Content); i += 2 {
		k := m.Content[i]
		if isMergeKey(k) {
			merges = append(merges, m.Content[i+1])
			continue
		}
		if !seen[k.Value] {
			seen[k.Value] = true
			out = append(out, nodePair{k.Value, m.Content[i+1]})
		}
	}
	if depth >= maxMergeDepth {
		return out
	}
	for _, src := range merges {
		src = derefAlias(src)
		sources := []*yaml.Node{src}
		if src.Kind == yaml.SequenceNode {
			sources = src.Content
		}
		for _, s := range sources {
			s = derefAlias(s)
			if s.Kind != yaml.MappingNode {
				continue
			}
			for _, p := range effectivePairs(s, depth+1) {
				if !seen[p.key] {
					seen[p.key] = true
					out = append(out, p)
				}
			}
		}
	}
	return out
}

// effectiveValue is the value effectivePairs gives key in m, or nil.
func effectiveValue(m *yaml.Node, key string, depth int) *yaml.Node {
	if !hasMergeKey(m) {
		for i := 0; i+1 < len(m.Content); i += 2 {
			if m.Content[i].Value == key {
				return derefAlias(m.Content[i+1])
			}
		}
		return nil
	}
	for _, p := range effectivePairs(m, depth) {
		if p.key == key {
			return derefAlias(p.value)
		}
	}
	return nil
}

// hasMergeKey reports whether mapping m writes a merge key.
func hasMergeKey(m *yaml.Node) bool {
	for i := 0; i+1 < len(m.Content); i += 2 {
		if isMergeKey(m.Content[i]) {
			return true
		}
	}
	return false
}

// isMergeKey reports whether k is a YAML merge key: yaml.v3's isMerge
// (decode.go), restated because it is unexported — without its Tag "!" arm,
// which no parsed node reaches: yaml.v3 drops the non-specific tag when it
// builds the node, so `! "<<"` is the literal key "<<" (Tag !!str) here as in
// the typed decode. A file that writes `!` is refused by parse-tree before
// any decode anyway (non_specific_tag; ADR-036 step 8, #3206).
func isMergeKey(k *yaml.Node) bool {
	return k.Kind == yaml.ScalarNode && k.Value == "<<" &&
		(k.Tag == "" || k.Tag == "!!merge" || k.Tag == "tag:yaml.org,2002:merge")
}

func derefAlias(n *yaml.Node) *yaml.Node {
	for n.Kind == yaml.AliasNode && n.Alias != nil {
		n = n.Alias
	}
	return n
}
