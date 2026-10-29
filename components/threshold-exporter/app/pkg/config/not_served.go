package config

// not_served: which keys of an effective config /metrics does not serve as
// written, and why (#2296).
//
// /effective (ResolveEffective, EffectiveTree) is the WRITTEN view: every
// layer's value as the author wrote it, laid per threshold (effectiveView).
// /metrics is the exporter's build of the same tree (BuildFlatConfig) and its
// resolver. Where the two part, the effective config keeps the written value
// and names the key in EffectiveConfig.NotServed with the reason.
//
// ⛔ EVERY REASON IS THE /metrics PATH'S OWN VERDICT, RECORDED WHERE IT IS
// MADE — never inferred by comparing the two outputs. A value comparison
// cannot tell `"60:critical"` (written as text, served as 60 with severity
// critical) from a value /metrics refused, and a second set of rules here
// would drift from the build. The places, all on the /metrics path:
//
//   - parse_failed: BuildFlatConfig drops the file (FlatBuild.ParseFailed) —
//     the root `_defaults.yaml` with a non-number under `defaults:`, a chain
//     file with a syntax error, a root platform file the decode rejects.
//   - root_defaults_unwrapped: the root carrier has no `defaults:` mapping and
//     the key acts when merged (ActsWhenMerged) — /effective shows it, the
//     root decode has no field for it (FlatBuild.RootDefaultsUnread).
//   - value_rejected: applySubtreeDefaults refuses a subtree level's value
//     (not threshold-shaped) and the tenant keeps a shallower one
//     (FlatBuild.RejectedChainValues, less the keys the tenant sets itself:
//     FlatBuild.rejectedTenantSets); or a subtree level writes a reserved
//     key a subtree does not carry (subtreeReservedUnsupported, #3341) and
//     the tenant side sets none — the one entry not shown in the effective
//     config, since neither it nor /metrics lays that value
//     (withNotCarried).
//   - value_unparsed: resolveBaseRows cannot parse the tenant's value (a
//     non-finite number included, #3140; `disable:<severity>` too, #3141) and
//     serves the platform default instead (rejectRecorder).
//   - value_unparsed_dropped: the critical / dimensional / declared phase
//     cannot parse the value and serves no row for it — and so does the base
//     phase when the root writes the key as a non-finite number, leaving no
//     default to fall back to (rejectRecorder).
//   - window_invalid: a `window:` of the value's schedule is one
//     parseTimeWindow refuses — the window the resolver WARNs about and
//     matches at no minute (#2065; rejectRecorder, at the phases' resolveValue
//     call, the read that WARNs).
//   - undeliverable: a subtree key the output plane never iterates (#1976,
//     FlatBuild.UnreachableValues through undeliverableThresholds).
//   - root_null_undeclared: the root writes the key as null (#2518,
//     FlatBuild.RootNullUndeclared).
//   - root_default_refused: the root `defaults:` writes the key as a value
//     the root decode refuses (#3250, readRootDefault: `0x1F`, `abc`,
//     `.nan`), so the resolver serves no default row for it
//     (ThresholdConfig.RefusedRootDefaults).
//   - root_defaults_reserved: the root `defaults:` writes a `_` key the
//     registry lists (#3250). The root decode reads that mapping for
//     thresholds only and no row phase reads a `_` key (baseRowsSkipKey), so
//     no tenant gets it (ThresholdConfig.ReservedRootDefaults); a subtree
//     level, a platform entry or a profile hands such a key to the tenant's
//     own map, the root does not.
//   - spelling_duplicate: the winning layer's map writes the threshold
//     under another spelling too, and the decode keeps that one
//     (normalizeKeys, #2031; keyed by the losing spelling as written).
//   - unregistered: a `_` key the reserved-key registry does not list
//     (IsUnregisteredUnderscoreKey): nothing reads it, and no row phase
//     reads any `_` key (baseRowsSkipKey, #3045).
//   - unconsumed (#3378): a threshold key of the tenant's built map that no
//     row phase reads (rejectRecorder: each phase claims the keys it reads,
//     and resolveAtWithStats records the rest once the tenant's phases have
//     run — noteUnclaimed). Two sources reach it: a flat key the root
//     declares nowhere (neither in `defaults:` nor in
//     `optional_overrides:`) that a tenant file, a platform `tenants:`
//     entry or a profile writes — `redis_evicted_keys: "7"`, which only
//     ValidateTenantKeys' "unknown key" line named before; and a
//     `<base>_critical` whose base the root `defaults:` does not value —
//     the row resolveCriticalRows drops ("has no matching default") — one
//     the tenant side writes; a subtree `_defaults.yaml` never hands such a
//     key down (the overlay asks the same condition, criticalBaseValued, and
//     reports it undeliverable instead). Not recorded: a `_` key
//     (unregistered and the reserved-key findings own those), a value
//     switched off in every window (disabledEverywhere, the
//     undeliverableThresholds rule: not serving it is what was asked), and a
//     dimensional `x{…}` key, which resolveDimensionalRows serves whether the
//     root declares `x` or not.
//
//     ⛔ unconsumed is NOT one of the reasons ValueNotServedAsWritten selects
//     (#3378 owner ruling): it is named here, in `da-guard effective`'s
//     not_served, and not by da-guard's value_not_served or the exporter's
//     da_config_values_not_served. How severe a key nobody reads is belongs
//     to ADR-038's one shared verdict — an error in a tenant file, a notice
//     in a profile or a platform file — and #3016 leaves the key-name
//     judgement with that verdict alone; a second, uniform error here would
//     contradict it for the profile and platform layers.
//
// ⚠️ NOT COVERED, stated so an empty NotServed is not read as "/metrics
// serves what effective shows": a profile-layer value applyProfiles discards
// (e.g. `pg_connections: abc` in `_profiles.yaml`) and an expired
// (`expires:`) override. No recording point exists for either.
//
// The effective side only answers WHOSE value the effective config shows
// (keySources, the winning layer and file) and looks that up in the tables
// above. A #1231 retired spelling is not a reason: /metrics serves it under
// the canonical spelling (served-values' `aliases` maps one to the other).

import (
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"time"

	"gopkg.in/yaml.v3"
)

// Reasons of NotServedKey.Reason — a closed set.
const (
	NotServedParseFailed           = "parse_failed"
	NotServedRootDefaultsUnwrapped = "root_defaults_unwrapped"
	NotServedValueRejected         = "value_rejected"
	NotServedValueUnparsed         = "value_unparsed"
	NotServedValueUnparsedDropped  = "value_unparsed_dropped"
	NotServedWindowInvalid         = "window_invalid"
	NotServedUndeliverable         = "undeliverable"
	NotServedRootNullUndeclared    = "root_null_undeclared"
	// NotServedRootDefaultRefused (#3250): the root `defaults:` value is one
	// the root decode refuses; the resolver serves no default row for it.
	NotServedRootDefaultRefused = "root_default_refused"
	// NotServedRootDefaultsReserved (#3250): a registered `_` key under the
	// root `defaults:`, which reaches no tenant.
	NotServedRootDefaultsReserved = "root_defaults_reserved"
	// NotServedSpellingDuplicate (#2031): one layer — one file's map —
	// writes a threshold under two spellings (two spellings of one
	// dimensional key, or both #1231 spellings); /metrics serves one of them
	// (keySpellings.dups).
	NotServedSpellingDuplicate = "spelling_duplicate"
	// NotServedUnregistered (#3045): a `_` key the reserved-key registry does
	// not list (IsUnregisteredUnderscoreKey); /metrics serves it in no layer.
	NotServedUnregistered = "unregistered"
	// NotServedUnconsumed (#3378): a threshold key of the tenant's built map
	// that no row phase reads — undeclared at the root, or a `_critical`
	// whose base the root `defaults:` does not value (noteUnclaimed).
	NotServedUnconsumed = "unconsumed"
)

// NotServedKey is why /metrics does not serve one key of an effective config
// as the effective config shows it.
type NotServedKey struct {
	// Reason is one of the NotServed* constants.
	Reason string `json:"reason"`
	// File is the root-relative slash path of the file whose value the
	// effective config shows (the key's winning layer) — or, for a `_` key a
	// subtree level writes non-null that the effective config does not show
	// (#3341), the deepest subtree file writing it; empty when no single file
	// applies.
	File string `json:"file,omitempty"`
}

// rootDecodedKeySet is the top-level keys the exporter's root decode reads:
// the yaml tags of ThresholdConfig, taken from the struct so the set cannot
// drift from it.
var rootDecodedKeySet = func() map[string]bool {
	out := map[string]bool{}
	t := reflect.TypeOf(ThresholdConfig{})
	for i := 0; i < t.NumField(); i++ {
		name, _, _ := strings.Cut(t.Field(i).Tag.Get("yaml"), ",")
		if name != "" && name != "-" {
			out[name] = true
		}
	}
	return out
}()

// RootDecodedKeys is a copy of the set of top-level keys the exporter's root
// decode (ThresholdConfig) has a field for. da-guard's root_defaults_unwrapped
// check and the build's FlatBuild.RootDefaultsUnread read the same set.
func RootDecodedKeys() map[string]bool {
	out := make(map[string]bool, len(rootDecodedKeySet))
	for k := range rootDecodedKeySet {
		out[k] = true
	}
	return out
}

// topLevelReadElsewhere (reserved_keys_gen.go, from the reserved-key
// registry; ADR-038) are the `_`-prefixed keys another reader takes from the
// top level of a defaults file: the route generator's `_routing_defaults` /
// `_routing_enforced`, the custom-alert compiler's `_custom_alerts`, and
// Policy-as-Code's `_policies` (moved from internal/guard, #2296).

// TopLevelReadElsewhere is a copy of topLevelReadElsewhere, for da-guard.
func TopLevelReadElsewhere() []string {
	return append([]string(nil), topLevelReadElsewhere...)
}

// ActsWhenMerged reports whether a top-level key of a defaults file is one
// the defaults merge takes into a tenant's config when it reads the whole
// document: a threshold (no `_` prefix) or a reserved tenant key
// (IsReservedKey). Excluded: `defaults` itself; a ThresholdConfig field
// (RootDecodedKeys); TopLevelReadElsewhere; a key the merge drops at every
// level (MergeDroppedKeys — `_metadata`), which acts in no shape; a
// `_routing*` key, which the route generator does not read from a defaults
// file in any shape (the generator's own routing findings name it); and any
// other `_` key (e.g. `_x: &x`, a key that only carries a YAML anchor).
//
// ONE predicate for da-guard's root_defaults_unwrapped /
// defaults_toplevel_ignored findings and the build's RootDefaultsUnread
// (#2296), so the two cannot name different keys.
func ActsWhenMerged(k string) bool {
	if k == "defaults" || rootDecodedKeySet[k] || mergeDroppedKeys[k] {
		return false
	}
	for _, r := range topLevelReadElsewhere {
		if k == r {
			return false
		}
	}
	if !strings.HasPrefix(k, "_") {
		return true
	}
	// ⚠️ Refs #2388: this `_routing` PREFIX is wider than IsRoutingKey
	// (`_routing`, `_routing_<…>`), so `_routingProfile` is left out here; left
	// as is for a follow-up.
	return IsReservedKey(k) && !strings.HasPrefix(k, "_routing")
}

// rootDefaultsUnread is FlatBuild.RootDefaultsUnread for the root carrier's
// bytes, which the root decode accepted as partial: when the document has no
// `defaults:` mapping — so the defaults-chain merge (ExtractDefaultsBlock)
// takes the whole document as the block — its top-level keys that act when
// merged (ActsWhenMerged, da-guard's root_defaults_unwrapped predicate; a
// ThresholdConfig field is never one), sorted. nil otherwise.
//
// A non-empty partial.Defaults means the file HAS a `defaults:` mapping, so
// the common tree pays no second decode.
func rootDefaultsUnread(data []byte, partial *ThresholdConfig) []string {
	if len(partial.Defaults) > 0 {
		return nil
	}
	var raw any
	if yaml.Unmarshal(data, &raw) != nil {
		return nil
	}
	doc, ok := normalizeYAMLToJSON(raw).(map[string]any)
	if !ok {
		return nil
	}
	if _, wrapped := doc["defaults"].(map[string]any); wrapped {
		return nil
	}
	var out []string
	for k := range doc {
		if ActsWhenMerged(k) {
			out = append(out, k)
		}
	}
	sort.Strings(out)
	return out
}

// UnreadKey is one top-level key of a file that the exporter's decode of
// that file has no place for (FlatBuild.RootDefaultsUnread).
type UnreadKey struct {
	File string // root-relative slash path
	Key  string
}

// rejectRecorder collects, while the resolver runs, the tenant values it
// cannot parse (#2296): tenant → canonical key → reason (NotServedValueUnparsed,
// NotServedValueUnparsedDropped, NotServedWindowInvalid from
// noteInvalidWindows, or NotServedUnconsumed from noteUnclaimed). nil = record nothing, which is what every scrape
// passes: the record sites sit inside the resolver's existing WARN branches,
// so the steady state pays one nil check per unparseable value.
//
// #3378: it also collects, per tenant, the keys of the tenant's map a row
// phase reads (claim) — each phase claims at the place it looks the key up —
// and once the tenant's phases have run, resolveAtWithStats records every
// threshold key none of them claimed as NotServedUnconsumed (noteUnclaimed).
// The claim sites carry the same nil check, so a scrape pays one per read.
type rejectRecorder struct {
	byTenant map[string]map[string]string
	// claimed is the keys of the tenant being resolved that a row phase has
	// read; emptied by noteUnclaimed after each tenant.
	claimed map[string]struct{}
}

// recordedRank orders the reasons a recorder keeps for one key: a value no
// row phase reads (#3378) outranks a dropped row, which outranks a value
// the resolver fell back from, which outranks a window that never applies
// (#2065) — a key read at several minutes, or with both a bad value and a
// bad window, keeps the strongest verdict. unconsumed is the strongest
// because its fix is the key itself: no value written for it, at any
// minute, can be served, while a dropped row is a value a phase read and
// could serve once written as a number. (A key is unconsumed only when no
// phase claimed it, and the phases record their other verdicts only on keys
// they read, so the two meet only across readings of the day, where the
// claims do not change — the rank is for completeness.)
func recordedRank(reason string) int {
	switch reason {
	case NotServedUnconsumed:
		return 4
	case NotServedValueUnparsedDropped:
		return 3
	case NotServedValueUnparsed:
		return 2
	case NotServedWindowInvalid:
		return 1
	}
	return 0
}

// note records key of tenant under reason unless the key already holds a
// verdict of the same or a higher rank (recordedRank).
func (r *rejectRecorder) note(tenant, key, reason string) {
	if r == nil {
		return
	}
	if r.byTenant == nil {
		r.byTenant = map[string]map[string]string{}
	}
	m := r.byTenant[tenant]
	if m == nil {
		m = map[string]string{}
		r.byTenant[tenant] = m
	}
	if cur, ok := m[key]; ok && recordedRank(cur) >= recordedRank(reason) {
		return
	}
	m[key] = reason
}

// claim records that a row phase read key (a canonical key of the tenant's
// map) for the tenant being resolved (#3378). Called only with a recorder,
// at the place the phase looks the key up: whether the phase then serves
// it, falls back to the default, drops it or finds it switched off is that
// phase's own verdict, recorded (or not) by note.
func (r *rejectRecorder) claim(key string) {
	if r.claimed == nil {
		r.claimed = map[string]struct{}{}
	}
	r.claimed[key] = struct{}{}
}

// noteUnclaimed records NotServedUnconsumed for each key of overrides (the
// tenant's canonical map, as the phases read it) that no phase claimed,
// then forgets the claims for the next tenant (#3378). Left out: a `_` key
// — no row phase reads one (baseRowsSkipKey), and NotServedUnregistered or
// the reserved-key findings name it — and a value switched off in every
// window (disabledEverywhere, the rule undeliverableThresholds' exclusion 3
// applies): `disable` asks for no row.
func (r *rejectRecorder) noteUnclaimed(tenant string, overrides map[string]ScheduledValue) {
	for k, sv := range overrides {
		if _, read := r.claimed[k]; read || baseRowsSkipKey(k) || disabledEverywhere(sv) {
			continue
		}
		r.note(tenant, k, NotServedUnconsumed)
	}
	clear(r.claimed)
}

// ValueNotServedAsWritten reports whether a NotServed verdict on key is about
// a threshold value as written that /metrics does not serve as written
// (#2065): reason is value_unparsed, value_unparsed_dropped, window_invalid,
// value_rejected or spelling_duplicate (#2031), and key is a threshold key — not `_`-prefixed
// ("not reserved ⇒ is a threshold", the tenant schema's rule; a reserved
// key a subtree `_defaults.yaml` writes is refused by the same overlay and
// named by da-guard's subtree_default_reserved_key, or by the route
// generator's routing findings for a routing key). The
// exporter's da_config_values_not_served and da-guard's value_not_served
// both select with it.
//
// ⛔ NOT unconsumed (#3378, owner ruling): a value no row phase reads is a
// key nobody reads, and how severe that is belongs to ADR-038's one shared
// verdict — an error in a tenant file, a notice in a profile or a platform
// file; #3016 keeps the key-name judgement in that verdict alone. Selecting
// unconsumed here would make it a uniform error (value_not_served) and a
// gauge count beside that verdict, contradicting it for the profile and
// platform layers. TestValueNotServedAsWritten_NotUnconsumed pins it.
func ValueNotServedAsWritten(key, reason string) bool {
	if strings.HasPrefix(key, "_") {
		return false
	}
	for _, r := range valueNotServedReasons {
		if reason == r {
			return true
		}
	}
	return false
}

// valueNotServedReasons is the reasons ValueNotServedAsWritten selects: the
// ones whose cause is a value as written. spelling_duplicate (#2031): a
// spelling of a threshold the same map also writes under the spelling
// /metrics serves. Not unconsumed (#3378; see ValueNotServedAsWritten).
var valueNotServedReasons = []string{
	NotServedValueUnparsed, NotServedValueUnparsedDropped, NotServedWindowInvalid,
	NotServedValueRejected, NotServedSpellingDuplicate,
}

// ValueNotServedReasons is a copy of the reasons ValueNotServedAsWritten
// selects — the closed label set of the exporter's
// da_config_values_not_served{reason}, so the gauge and da-guard's
// value_not_served read one list.
func ValueNotServedReasons() []string {
	return append([]string(nil), valueNotServedReasons...)
}

// ValuesNotServed is what the resolver behind /metrics records about the
// tenant values of c it does not serve as written, over the whole UTC day:
// tenant → canonical key → NotServedValueUnparsed,
// NotServedValueUnparsedDropped, NotServedWindowInvalid (#2065) or
// NotServedUnconsumed (#3378: no row phase reads the key). It is
// the table `da-guard effective`'s not_served reads. Silent; c is not
// modified. nil when nothing is recorded.
//
// ⚠️ Tenant values only: a subtree `_defaults.yaml` value the build refused
// is FlatBuild.RejectedChainValues (less rejectedTenantSets), and the other
// NotServed reasons are the build's, not the resolver's.
//
// It resolves c with a recorder over every minute of the UTC day at which
// some tenant value may resolve differently, so the verdict does not depend
// on the time of day the request is made.
//
// A tenant's rows depend only on its own map and the config-wide settings,
// so the day is read per tenant. The whole config is resolved once as
// written, at now: that is every reading of a tenant without a schedule, and
// where the phases read each scheduled value's windows (noteInvalidWindows;
// AtMinuteOfDay drops the schedules). Each tenant with a schedule is then
// resolved alone (a one-tenant copy) at the start of each segment of its own
// day (addScheduleCuts) but the one now falls in (#2065 r1: the resolve
// as written already read it). Resolving every tenant at the union of every
// tenant's cuts records the same table
// (TestValuesNotServed_PerTenantCutsMatchUnionOfCuts) at the cost of
// tenants × all cuts.
func (c *ThresholdConfig) ValuesNotServed(now time.Time) map[string]map[string]string {
	rec := &rejectRecorder{}
	c.resolveAtWithStats(now, nil, nil, rec)
	utc := now.UTC()
	nowMinute := utc.Hour()*60 + utc.Minute()
	var one ThresholdConfig
	for tenant, overrides := range c.Tenants {
		seen := addScheduleCuts(nil, overrides)
		if seen == nil {
			continue
		}
		cuts := sortedCuts(seen)
		// The segment now's minute falls in was read by the resolve above:
		// no window of this tenant's starts or ends between its start and
		// now, so the values there are the values at now.
		current := sort.SearchInts(cuts, nowMinute+1) - 1
		one = *c
		one.Tenants = map[string]map[string]ScheduledValue{tenant: overrides}
		for i, m := range cuts {
			if i == current {
				continue
			}
			one.AtMinuteOfDay(m).resolveAtWithStats(now, nil, nil, rec)
		}
	}
	return rec.byTenant
}

// noteInvalidWindows records NotServedWindowInvalid for key of tenant when a
// `window:` of sv is one parseTimeWindow refuses — the window the resolver
// WARNs about and matches at no minute. Called by the resolve phases right
// where they read sv with resolveValue (the read that WARNs), only with a
// recorder.
func (r *rejectRecorder) noteInvalidWindows(tenant, key string, sv ScheduledValue) {
	for _, o := range sv.Overrides {
		if _, _, ok := parseTimeWindow(o.Window, nil); !ok {
			r.note(tenant, key, NotServedWindowInvalid)
			return
		}
	}
}

// servedVerdicts is one build's not-served tables, for the effective
// resolver (#2296). Built from the build ScopeEffective / ResolveEffective
// already ran over the same scan.
type servedVerdicts struct {
	parseFailed   map[string]bool                       // root-relative scan keys (FlatBuild.ParseFailed)
	rootUnread    map[string]bool                       // root carrier keys (FlatBuild.RootDefaultsUnread)
	rejected      map[string]map[string]bool            // file → keys (FlatBuild.RejectedChainValues)
	tenantSets    map[string]map[string]map[string]bool // tenant → file → keys of rejected it sets (FlatBuild.rejectedTenantSets)
	undeliverable map[string]map[string]bool            // tenant → keys (undeliverableThresholds)
	rootNull      map[string]map[string]bool            // tenant → keys (FlatBuild.RootNullUndeclared)
	rootRefused   map[string]string                     // root `defaults:` keys (ThresholdConfig.RefusedRootDefaults)
	rootReserved  map[string]bool                       // root `defaults:` keys (ThresholdConfig.ReservedRootDefaults)

	// cfg is the build's config; unparsed is ValuesNotServed over it,
	// computed on the first tenant that asks (all, when all is set — the
	// whole-tree mode resolves every tenant — else only that tenant's copy).
	cfg      *ThresholdConfig
	all      bool
	unparsed map[string]map[string]string
	done     bool
	// now is the instant ValuesNotServed reads expires: at — the caller's
	// (#3368: pkg/config reads no wall clock).
	now time.Time

	// noUnparsed skips ValuesNotServed: the caller reads only verdicts the
	// switch in notServed decides before the resolver's record
	// (RejectedShownCache.Shown reads value_rejected).
	noUnparsed bool

	// gate is set for ScopeEffective (da-guard's gate, #2065): the tables
	// name what /metrics does not serve, but a chain file the build dropped
	// is not read as an empty file — the gate keeps stopping on it
	// (chainFileUnread).
	gate bool
}

func newServedVerdicts(built *FlatBuild, all bool, now time.Time) *servedVerdicts {
	v := &servedVerdicts{cfg: &built.Config, all: all, now: now}
	if refused := built.Config.RefusedRootDefaults(); len(refused) > 0 {
		v.rootRefused = refused
	}
	for _, k := range built.Config.ReservedRootDefaults() {
		if v.rootReserved == nil {
			v.rootReserved = map[string]bool{}
		}
		v.rootReserved[k] = true
	}
	if len(built.ParseFailed) > 0 {
		v.parseFailed = make(map[string]bool, len(built.ParseFailed))
		for _, k := range built.ParseFailed {
			v.parseFailed[k] = true
		}
	}
	for _, u := range built.RootDefaultsUnread {
		if v.rootUnread == nil {
			v.rootUnread = map[string]bool{}
		}
		v.rootUnread[u.Key] = true
	}
	v.rejected, v.tenantSets = built.RejectedChainValues, built.rejectedTenantSets
	for tenantID, keys := range undeliverableThresholds(built.UnreachableValues) {
		if v.undeliverable == nil {
			v.undeliverable = map[string]map[string]bool{}
		}
		v.undeliverable[tenantID] = make(map[string]bool, len(keys))
		for k := range keys {
			v.undeliverable[tenantID][k] = true
		}
	}
	for tenantID, keys := range built.RootNullUndeclared {
		if v.rootNull == nil {
			v.rootNull = map[string]map[string]bool{}
		}
		v.rootNull[tenantID] = make(map[string]bool, len(keys))
		for _, k := range keys {
			v.rootNull[tenantID][k.Key] = true
		}
	}
	return v
}

// unparsedOf is ValuesNotServed's verdicts for tenantID.
func (v *servedVerdicts) unparsedOf(tenantID string) map[string]string {
	if v.noUnparsed {
		return nil
	}
	if v.all {
		if !v.done {
			v.unparsed = v.cfg.ValuesNotServed(v.now)
			v.done = true
		}
		return v.unparsed[tenantID]
	}
	return v.cfg.TenantValuesNotServed(tenantID, v.now)
}

// chainFileUnread reports whether chain file rel is one /metrics does not
// read at all because it does not parse — the effective resolve then reads
// it as an empty file and names it in ChainParseFailed.
func (v *servedVerdicts) chainFileUnread(rel string) bool {
	return !v.gate && v.parseFailed[rel]
}

// candidates reports whether any table could name a key of this tenant's
// view, so a tenant with none skips the attribution (keySources) entirely.
func (v *servedVerdicts) candidates(tenantID string, chain []string, unparsed map[string]string, view map[string]any) bool {
	if len(v.parseFailed) > 0 || len(v.rootUnread) > 0 || len(v.rootRefused) > 0 || len(v.rootReserved) > 0 ||
		len(unparsed) > 0 ||
		len(v.undeliverable[tenantID]) > 0 || len(v.rootNull[tenantID]) > 0 {
		return true
	}
	for _, f := range chain {
		if len(v.rejected[f]) > 0 {
			return true
		}
	}
	for k := range view {
		if IsUnregisteredUnderscoreKey(k) {
			return true
		}
	}
	return false
}

// notServed is EffectiveConfig.NotServed for one tenant: for each key of
// view, the winner's file and layer (ks) looked up in the build's tables.
// rootLevel is the chain index of the root carrier (-1: none). nil when no
// key is named.
func (v *servedVerdicts) notServed(tenantID string, view map[string]any, ks map[string]KeySource,
	rootLevel int, unparsed map[string]string,
) map[string]NotServedKey {
	var out map[string]NotServedKey
	for k := range view {
		src, ok := ks[k]
		if !ok {
			continue
		}
		atRoot := src.Layer == KeyLayerDefaults && src.Level != nil && *src.Level == rootLevel
		canon, _ := canonicalKeyFor(k)
		var reason string
		switch {
		case v.parseFailed[src.File]:
			reason = NotServedParseFailed
		case IsUnregisteredUnderscoreKey(k):
			reason = NotServedUnregistered
		case atRoot && v.rootUnread[k]:
			reason = NotServedRootDefaultsUnwrapped
		case atRoot && (v.rootRefused[k] != "" || v.rootRefused[canon] != ""):
			reason = NotServedRootDefaultRefused
		case atRoot && v.rootReserved[k]:
			reason = NotServedRootDefaultsReserved
		case src.Layer == KeyLayerDefaults && !atRoot && v.rejected[src.File][k] && !v.tenantSets[tenantID][src.File][k]:
			reason = NotServedValueRejected
		case unparsed[canon] == NotServedValueUnparsedDropped:
			reason = NotServedValueUnparsedDropped
		case unparsed[canon] == NotServedValueUnparsed && !atRoot:
			// At the root, the default /metrics falls back to IS the value
			// the effective config shows.
			reason = NotServedValueUnparsed
		case unparsed[canon] == NotServedWindowInvalid:
			reason = NotServedWindowInvalid
		case v.undeliverable[tenantID][k]:
			reason = NotServedUndeliverable
		case v.rootNull[tenantID][k]:
			reason = NotServedRootNullUndeclared
		case unparsed[canon] == NotServedUnconsumed:
			// #3378. After root_null_undeclared: a tenant value for a key
			// the root writes as null is one no phase reads, so the resolver
			// records it unconsumed too — but its cause is the root null,
			// which has a finding of its own (root_default_null_undeclared,
			// a warning): one cause, one report. undeliverable cannot meet it:
			// the overlay never writes an undeliverable key into the map the
			// resolver reads.
			reason = NotServedUnconsumed
		default:
			continue
		}
		if out == nil {
			out = map[string]NotServedKey{}
		}
		out[k] = NotServedKey{Reason: reason, File: src.File}
	}
	return out
}

// relRejected converts one of applySubtreeDefaults' absolute-path rejection
// tables (defaults file → keys: the whole tree's, or one tenant's
// tenantSets) to root-relative slash paths (FlatBuild.RejectedChainValues,
// FlatBuild.rejectedTenantSets), dropping files with no key. nil when none.
func relRejected(rootDir string, byAbs map[string]map[string]bool) map[string]map[string]bool {
	var out map[string]map[string]bool
	for p, keys := range byAbs {
		if len(keys) == 0 {
			continue
		}
		if out == nil {
			out = map[string]map[string]bool{}
		}
		out[subtreeRelPath(rootDir, filepath.Clean(p))] = keys
	}
	return out
}
