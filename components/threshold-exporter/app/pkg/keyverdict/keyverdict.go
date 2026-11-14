// Package keyverdict is the one verdict on "does any reader read this key"
// (ADR-038 decisions 4–7): for every key of every tenant, read / unread /
// undecidable with a severity and a reason, and the findings a report
// prints — deduplicated by file and key, the profile-layer judgment.
//
// ⛔ NO READING OF ITS OWN. Judge joins readings the caller hands it, each
// produced by the exporter's own code (da-guard builds them: served-values
// with disable effects, effective, the root carrier's policy rules, the
// root platform files' `profiles:` as parse-tree reads them). It decides
// nothing about what the exporter serves: that is the served reading's.
//
// A key is read when some reader reads it (the exporter, a reader the
// reserved-key registry declares in the key's layer, a Policy-as-Code rule
// that names it) in some part of the UTC day; unread when none does all day
// (#3835: the served reading is the whole day's, ServedKey; only what has
// expired — `expires:` — depends on the instant it is read at);
// undecidable when no root platform file was read (Input.Undecidable) and
// the key is a threshold key — `_` keys are judged as usual (decision 7). A `_` key the registry does not
// list is read by no reader (#3045).
//
// Every unread key is an error, wherever it is written — a tenant file, a
// profile, a subtree `_defaults.yaml`, a root platform file's `tenants:`
// (decision 4, #3394). Two exceptions: a key unread only because its
// `disable` switches nothing off is a warning, and a `_state_<filter>` whose
// filter the root does not declare is a notice (the platform's missing
// declaration, decision 7). The severity describes the defect, not who is
// blocked: which gate refuses what is Blocks / NewBlocks (decision 6).
//
// A `disable` is read when it switches something off in some part of the
// day (effective); a key unread only because of a `disable` that switches
// nothing off (every part of the day it is unread in, it reads `disable`)
// is unread at warning, disable_noop — except on a `_` key the registry
// does not list, which is a misspelt reserved key, unread at the severity of
// any other unread key. A key with no row in some part of the day for any
// other reason is unread for that reason, whatever its `disable` does
// elsewhere.
//
// A base's `disable` switches its family off with it (#3046), so a
// `<base>_critical` / `<base>{…}` key served no row for that reason
// (ReasonBaseDisabled) is read: the exporter read it to switch it off.
//
// Values are not judged here (#3835): a key whose value the resolver
// refuses (non-finite, unparsed) is read — the main gate's value_not_served
// and config.KeyValidation judge the value, over the whole day.
//
// A `_` key written in a profile is judged on its own, from the
// registry's `profile` layer, whether or not a tenant binds the profile: a
// key no reader reads there is ONE error on that file, with the tenants
// that bind the profile — it replaces the finding the tenants' keys would
// give (#3042).
//
// A threshold key the threshold registry retired (`retired_keys:`, #3829)
// is unread at error, reason `retired`, whatever reads it and in whichever
// layer it is written — a root `defaults:` included, where the exporter
// still serves a row for it; its Hint names the key that replaces it.
//
// A tenant file's top-level key other than `tenants`
// (config.TenantFileTopLevelKey) is no tenant's key and no reader reads it:
// ONE error on that file, naming no tenant (#3671).
//
// Input.Tenants, given, is the subset the readings were computed for (a
// write's tenants, ADR-038 §5): only those tenants are judged; a profile's
// finding is judged over them only.
package keyverdict

import (
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// Verdicts.
const (
	Read        = "read"
	Unread      = "unread"
	Undecidable = "undecidable"
)

// Severities: those decision 4 gives the gates once they block.
const (
	SeverityError   = "error"
	SeverityWarning = "warning"
	SeverityNotice  = "notice"
)

// Finding kinds.
const (
	KindUnread      = "unread"
	KindDisableNoop = "disable_noop"
	// KindDisableCascade (#3046): a `<base>_critical` / `<base>{…}` key the
	// tenant's own file writes, switched off in some part of the day with
	// its base's `disable` (from any layer) — what the old semantics kept
	// served. A warning: the key is read, its value serves nothing there.
	KindDisableCascade = "disable_cascade"
	// KindProfileNotJudged: a threshold key of a profile no tenant binds —
	// not judged (whether the exporter reads it is measured on a tenant),
	// said so rather than left to read as judged and fine (#3395).
	KindProfileNotJudged = "profile_not_judged"
	// KindValueUnread (#3591, ADR-038 decision 10): a reserved key's value
	// the exporter reads in no way it was written to be read — an unknown
	// word, a `_metadata` field TenantMetadata does not know, a `_profile`
	// naming no profile — judged in the file and layer that writes it
	// (config.LayerValueProblems). The key is read; its value, or part of
	// it, is not. An error in a tenant file; a notice in a root platform
	// file's `tenants:` entry or a profile, which the tenant cannot fix in
	// its own file.
	KindValueUnread = "value_unread"
	// KindDeadEntry (#3502): a key read, whose schedule has an entry
	// /metrics serves at no minute of the UTC day — a window the windows
	// written before it cover, or a `default` the windows cover
	// (config.ScheduledValue.DeadEntries, the served reading's
	// dead_entries). An error on the file the value comes from — the
	// tenant's own file, a root platform file's entry, a profile or a
	// subtree `_defaults.yaml` (decision 6): the author wrote a value that
	// never applies. DeadEntries lists the entries; Reason is the first's.
	KindDeadEntry = "dead_entry"
	// KindScheduleUnread (#3834): a reserved key written with a schedule —
	// a time window (`overrides:`) or an `expires:` / `reason:` — that its
	// readers ignore: every reader reads the `default` alone (#3269,
	// config.LayerScheduleProblems). The key and its `default` are read;
	// the schedule is not. Judged in the file and layer that writes it as
	// KindValueUnread is (decision 10): an error in a tenant file, a notice
	// in a root platform file's `tenants:` entry or a profile.
	KindScheduleUnread = "schedule_unread"
)

// Reasons this package reads from the served reading or gives itself. The
// served reading's other reasons (no_row, not_exporter_key, …) pass through
// as they are.
const (
	// ReasonDisabled: the served reading's reason for a key switched off in
	// every part of the day it is not read in (#3835); whether that changed
	// what /metrics serves is Disable's.
	ReasonDisabled = "disabled"
	// ReasonBaseDisabled: the served reading's reason for a `<base>_critical`
	// / `<base>{…}` key served no row in some part of the day because the
	// tenant switches its base off there (#3046) — read, by the phase that
	// switches it off.
	ReasonBaseDisabled = "base_disabled"
	// ReasonRetired: a retired reserved key, or a retired threshold key (the
	// threshold registry's `retired_keys:`, #3829) — the latter an error in
	// every layer whatever reads it, with a Hint naming its replacement.
	ReasonRetired = "retired"
	// ReasonUnregistered: a `_` key the registry does not list.
	ReasonUnregistered = "unregistered"
	// ReasonStateFilterUndeclared: `_state_<filter>` for a filter the root
	// `state_filters:` does not declare (the platform's to fix).
	ReasonStateFilterUndeclared = "state_filter_undeclared"
	// ReasonNotServed: a key the served reading has no entry for, read by
	// no reader.
	ReasonNotServed = "not_served"
	// ReasonDisableNoop: a disable that switches nothing off.
	ReasonDisableNoop = "disable_noop"
	// ReasonNotReadInProfile: a registered `_` key no reader reads in the
	// profile layer.
	ReasonNotReadInProfile = "not_read_in_profile"
	// ReasonProfileUnbound: KindProfileNotJudged's reason.
	ReasonProfileUnbound = "profile_unbound"
	// ReasonTenantFileTopLevel (#3671): a top-level key of a tenant file
	// other than config.TenantFileTopLevelKey's — the exporter takes a
	// tenant's keys from `tenants.<id>` alone, and no other reader reads a
	// tenant file.
	ReasonTenantFileTopLevel = "tenant_file_top_level"
)

// Disable effects (served-values' keys[].disable).
const (
	DisableEffective = "effective"
	DisableNoop      = "noop"
)

// ServedKey is what the served reading says of one key of a tenant's merged
// config over the whole UTC day (#3835: da-guard's servedDay, served-values'
// keys[] read at every part of the day and folded).
type ServedKey struct {
	// Exporter: the exporter reads the key in some part of the day.
	Exporter bool
	// Reason: why it does not; "" when it does. Never a reason about the
	// key's value (served-values' value_non_finite / value_unparsed): a
	// value the resolver refuses is a key it reads.
	Reason string
	// Registered, for a `_` key: whether the registry lists it; nil for a
	// threshold key.
	Registered *bool
	// Declared: the registry's other readers of a `_` key, reader → layers.
	Declared map[string][]string
	// Disable: what switching the key off does (Disable*); "" when it
	// reads no disable.
	Disable string
	// SwitchedOffWith: the base whose `disable` switches this key off in
	// some part of the day (#3046); "" for none.
	SwitchedOffWith string
	// DeadEntries: the entries of the key's schedule /metrics serves at no
	// minute of the day (#3502); nil for none.
	DeadEntries []config.DeadEntry
}

// ServedTenant is one tenant's served reading.
type ServedTenant struct {
	// Keys: every key of the tenant's merged config (and the unserved keys
	// it inherits), as the tenant's config spells them.
	Keys map[string]ServedKey
	// Values: the keys /metrics serves a row for in some part of the day
	// (canonical spelling) and the reserved keys the exporter's resolvers
	// read. Only which keys it holds is read, never their values (#3835: no
	// value read at one instant reaches the verdict).
	Values map[string]any
	// Rows: every key (canonical spelling) with a /metrics threshold row or
	// a row /metrics dropped (served-values' severities ∪ dropped) — not
	// Values alone, which lists every tenant's `_silent_mode`, `_metadata`,
	// … whether or not anything set them.
	Rows map[string]bool
}

// EffectiveTenant is one tenant's effective config (da-guard effective, as
// written).
type EffectiveTenant struct {
	Config  map[string]any
	Sources map[string]config.KeySource
	// Profile is the profile the tenant is bound to; "" for none.
	Profile string
	// DefaultsChain: the chain's files, root first.
	DefaultsChain []string
	// NotServed: effective's not_served (keys as written). A
	// profile_not_filled entry is a key the tenant's profile writes that the
	// exporter does not fill in (#1189), in no layer of Config (#3395).
	NotServed map[string]config.NotServedKey
}

// PolicyHead is the first path element of a Policy-as-Code rule's
// `target` / `when.target`, and the tenants the rule leaves out.
type PolicyHead struct {
	Head     string
	Excluded map[string]bool
}

// Input is the readings Judge joins.
type Input struct {
	Served    map[string]ServedTenant
	Effective map[string]EffectiveTenant
	Heads     []PolicyHead
	// Undecidable: no root platform file was read (decision 7).
	Undecidable bool
	// RootProfiles: root platform file → its `profiles:` mapping (profile
	// name → body), for the root platform files that have one and a reader
	// reads. nil: the profile layer is not judged.
	RootProfiles map[string]map[string]any
	// Values: the reserved values the exporter does not read, per file and
	// layer (LoadReport.ValueProblems). Only those of the tenants Served or
	// Effective hold are judged; a profile's names the tenants among them
	// that bind it.
	Values []config.LayerValueProblem
	// Schedules (#3834): the reserved values written with a schedule their
	// readers ignore, per file and layer (LoadReport.ScheduleProblems),
	// judged as Values are.
	Schedules []config.LayerValueProblem
	// TenantFiles (#3671): tenant file (scan key) → its top-level keys as
	// the file spells them (parse-tree's content), for the tenant files the
	// caller judges. nil: none judged.
	TenantFiles map[string][]string
	// Retired (#3829): the hint for a retired threshold key, "" for any other
	// key. nil: config.RetiredThresholdHint (the registry's `retired_keys:`).
	Retired func(key string) string
	// Whole: Served and Effective hold every tenant of the tree (no
	// `--tenant` subset, no file the load dropped, no tenant set aside) —
	// only then is a profile no tenant binds known to be one
	// (unboundProfileFindings).
	Whole bool
}

// KeyVerdict is the verdict on one key of one tenant. The JSON is
// `da-guard key-verdict`'s per-key entry (and explain-keys').
type KeyVerdict struct {
	Tenant string `json:"-"`
	// Key as the tenant's config spells it.
	Key string `json:"key"`
	// Layer: the effective layer the tenant's value comes from; nil when
	// effective has no source for the key.
	Layer *string `json:"layer"`
	// File: the file the tenant's value comes from; nil as Layer.
	File *string `json:"file"`
	// ReadBy: the readers that read it, sorted; [] for none.
	ReadBy   []string `json:"read_by"`
	Verdict  string   `json:"verdict"`
	Severity *string  `json:"severity"`
	Reason   *string  `json:"reason"`
	Disable  *string  `json:"disable"`
	// Hint (#3829): for a retired threshold key, what replaces it; nil
	// otherwise.
	Hint *string `json:"hint"`
	// SwitchedOffWith: the base whose `disable` switches this key off in
	// some part of the day (#3046); nil for none.
	SwitchedOffWith *string `json:"switched_off_with"`
	// DeadEntries (#3502): the entries of the key's schedule /metrics
	// serves at no minute of the UTC day, as the served reading names them;
	// [] for none.
	DeadEntries []config.DeadEntry `json:"dead_entries"`
	// Value: the effective config's value as written; nil when absent.
	// (v3, #3835: no `served` — what /metrics served at one instant is
	// served-values'.)
	Value any `json:"value"`
}

// Finding is one thing to report: a tenant's own key, or a platform file /
// profile key reaching Tenants.
type Finding struct {
	Severity string  `json:"severity"`
	Kind     string  `json:"kind"`
	Key      string  `json:"key"`
	File     *string `json:"file"`
	// Tenants: sorted; [] for a profile no tenant binds and for a tenant
	// file's top-level key, which is no tenant's.
	Tenants []string `json:"tenants"`
	Reason  *string  `json:"reason"`
	// Hint (#3829): a retired threshold key's hint, naming what replaces
	// it; nil for any other finding.
	Hint *string `json:"hint"`
	// Base: a disable_cascade's base, whose `disable` switches the key off;
	// nil for any other kind.
	Base *string `json:"base"`
	// DeadEntries: a dead_entry's entries, in the order written; nil for
	// any other kind.
	DeadEntries []config.DeadEntry `json:"dead_entries"`
	// Blocks: a gate that judges the whole tree refuses it (Blocking).
	Blocks bool `json:"blocks"`
	// own: the finding is about the entry of its one tenant in File (a
	// value_unread in a tenant file) — what Blocks reads as Block.Own where
	// the key's verdict row has no file (effective carries no `_metadata`).
	own bool
	// reading: a value_unread's value as its reader reads it, a
	// dead_entry's entries as written (config.DeadEntriesReading) — what
	// makes two such Blocks the same one (NewBlocks); "" for any other kind.
	reading string
}

// Tree is the verdict on a tree.
type Tree struct {
	// Keys: tenant → its keys, sorted by key.
	Keys        map[string][]KeyVerdict
	Findings    []Finding
	Undecidable bool
	// Blocks: some finding blocks, or some key is undecidable — a gate that
	// judges the whole tree refuses it (len(Blocks(tree)) > 0).
	Blocks bool
	// UndecidableBlocks: some key is undecidable, and so blocks (Blocks
	// with Kind Undecidable) — what a report of the undecidable keys says.
	UndecidableBlocks bool
}

// registryLayers maps KeySource.Layer to the registry's layer. The root
// `defaults:` (level 0) is no registry layer.
var registryLayers = map[string]string{
	config.KeyLayerTenant:   "tenant",
	config.KeyLayerPlatform: "platform_tenants",
	config.KeyLayerProfile:  "profile",
}

// fileLevelKey reports whether key, written at the top level of a defaults
// file, is the file's own key and not a tenant's: a ThresholdConfig field
// (`optional_overrides`, `state_filters`…), a key another reader takes from
// the file's top level (`_routing_defaults`, `_policies`…), or a routing
// key. Effective shows it as inherited when the file has no `defaults:`
// wrapper (the merge reads the whole document) — which takes it into no
// tenant's config (config.ActsWhenMerged) — so it is not judged as a
// tenant's key; with the wrapper effective does not show it at all. Inside
// `defaults:` such a key is reported by validate-config's `root_defaults`
// row and the route generator, not here (#3016: once the gates block, a
// valid routing-only `_defaults.yaml` must not fail them).
func fileLevelKey(key string) bool {
	if config.RootDecodedKeys()[key] || config.IsRoutingKey(key) {
		return true
	}
	for _, k := range config.TopLevelReadElsewhere() {
		if k == key {
			return true
		}
	}
	return false
}

// Judge is the verdict over the readings in.
func Judge(in Input) Tree {
	// A key in the exporter's canonical spelling (#1231, canonicalSpelling):
	// it looks a key up in the served reading, and it is how a policy rule
	// names a key — one rule for both (#3556).
	canon := canonicalSpelling
	policy := canonicalSpelling
	retired := in.Retired
	if retired == nil {
		retired = config.RetiredThresholdHint
	}
	keys := map[string][]KeyVerdict{}
	rootDefaults := map[string]string{}
	tenants := map[string]bool{}
	for t := range in.Served {
		tenants[t] = true
	}
	for t := range in.Effective {
		tenants[t] = true
	}
	for _, tenant := range sortedKeys(tenants) {
		sv, hasSv := in.Served[tenant]
		ef, hasEf := in.Effective[tenant]
		if hasEf && len(ef.DefaultsChain) > 0 {
			rootDefaults[tenant] = ef.DefaultsChain[0]
		}
		all := map[string]bool{}
		for k := range sv.Keys {
			all[k] = true
		}
		for k := range ef.Config {
			all[k] = true
		}
		rows := make([]KeyVerdict, 0, len(all))
		for _, key := range sortedKeys(all) {
			c := canon(key)
			held, hasRows := false, false
			if hasSv {
				_, a := sv.Values[key]
				_, b := sv.Values[c]
				held = a || b
				hasRows = sv.Rows[key] || sv.Rows[c]
			}
			sk, hasSk := sv.Keys[key]
			src, hasSrc := ef.Sources[key]
			if hasSrc && src.Layer == config.KeyLayerDefaults && fileLevelKey(key) {
				continue
			}
			var value any
			if hasEf {
				value = ef.Config[key]
			}
			rows = append(rows, judgeKey(tenant, key, sk, hasSk, src, hasSrc, value, held, hasRows,
				in.Heads, in.Undecidable, policy, retired))
		}
		rows = append(rows, profileNotFilled(tenant, ef.NotServed, all)...)
		sort.SliceStable(rows, func(i, j int) bool { return rows[i].Key < rows[j].Key })
		keys[tenant] = rows
	}
	findings := findingsOf(keys, rootDefaults)
	findings = append(findings, valueFindings(in.Values, tenants, in.Effective, in.Undecidable)...)
	findings = append(findings, valueFindings(in.Schedules, tenants, in.Effective, in.Undecidable)...)
	findings = append(findings, tenantFileFindings(in.TenantFiles)...)
	if in.RootProfiles != nil {
		findings = mergeProfileFindings(keys, findings, profileLayerFindings(in.RootProfiles, in.Effective))
		if in.Whole {
			findings = append(findings, unboundProfileFindings(in.RootProfiles, in.Effective)...)
		}
	}
	sortFindings(findings)
	for i := range findings {
		findings[i].Blocks = Blocking(findings[i].Severity)
	}
	t := Tree{Keys: keys, Findings: findings, Undecidable: in.Undecidable}
	bs := Blocks(t)
	t.Blocks = len(bs) > 0
	for _, b := range bs {
		t.UndecidableBlocks = t.UndecidableBlocks || b.Kind == Undecidable
	}
	return t
}

// judgeKey is the verdict on one key (the module doc's rules). held: the
// served reading's Values holds the key (ServedTenant.Values).
func judgeKey(tenant, key string, sk ServedKey, hasSk bool, src config.KeySource, hasSrc bool,
	value any, held, hasRows bool, heads []PolicyHead, undecidable bool,
	policy func(string) string, retired func(string) string) KeyVerdict {
	layer := ""
	if hasSrc {
		layer = src.Layer
	}
	regLayer := registryLayers[layer]
	if layer == config.KeyLayerDefaults && hasSrc && src.Level != nil && *src.Level > 0 {
		regLayer = "subtree_defaults"
	}
	reserved := strings.HasPrefix(key, "_")

	readBy := map[string]bool{}
	reason, disable := sk.Reason, sk.Disable
	declared := sk.Declared
	var registered *bool
	if hasSk {
		registered = sk.Registered
	} else if reserved {
		// A key the exporter's build does not take into the tenant's config
		// (no served entry): the registry's own declaration.
		readers, ok := config.RegisteredReaders(key)
		declared, registered = readers, &ok
	}
	misspeltReserved := reserved && registered != nil && !*registered

	switch {
	case hasSk && sk.Exporter:
		readBy["exporter"] = true
	case !hasSk && hasRows:
		// Not a key of the tenant's merged config: a threshold it inherits,
		// which /metrics serves for it.
		readBy["exporter"] = true
	case !hasSk && reserved && contains(declared["exporter"], regLayer) && held:
		// A reserved key the tenant inherits from a layer the exporter
		// reads it in (`_state_<f>`, `_severity_dedup` from a subtree
		// `_defaults.yaml`), which the exporter's resolver read.
		readBy["exporter"] = true
	case !hasSk && strings.HasPrefix(key, "_state_") && !held:
		// Inherited `_state_<filter>` with no reading: the filter is not
		// declared where the exporter reads declarations (decision 7).
		reason = ReasonStateFilterUndeclared
	}
	if hasSk && disable == DisableEffective {
		// Its `disable` switches something off in some part of the day: the
		// exporter read it to do so (decision 8) — whatever the key's reason
		// in the rest of the day (#3835).
		readBy["exporter"] = true
		reason = ""
	}
	if hasSk && reason == ReasonBaseDisabled {
		// #3046: read by the critical / dimensional phase, which switches
		// it off with its base.
		readBy["exporter"] = true
		reason = ""
	}
	for reader, layers := range declared {
		if reader != "exporter" && contains(layers, regLayer) {
			readBy[reader] = true
		}
	}
	// Policy-as-Code reads what its view holds: every threshold key, and a
	// `_` key only when the registry lists it. A rule naming a `_` key
	// outside that view finds nothing there.
	inPolicyView := !reserved || (registered != nil && *registered)
	if inPolicyView && namedByPolicy(tenant, key, heads, policy) {
		readBy["policy_engine"] = true
	}

	// #3829: a threshold key the registry retired is refused whatever reads
	// it and wherever it is written — the exporter still serves a row for a
	// retired key a `defaults:` declares, and only the registry knows no
	// rule pack reads it any more.
	hint := ""
	if !reserved {
		hint = retired(key)
	}

	verdict, severity := "", ""
	switch {
	case hint != "":
		verdict, severity, reason = Unread, SeverityError, ReasonRetired
	case len(readBy) > 0:
		verdict, reason = Read, ""
	case undecidable && !reserved:
		verdict = Undecidable
	default:
		verdict = Unread
		switch {
		case misspeltReserved:
			// A `_` key the registry does not list is a misspelt reserved
			// key (`_routng: disable`), not a switch-off with nothing to
			// switch.
			severity = SeverityError
			if reason != ReasonRetired {
				reason = ReasonUnregistered
			}
		case reason == ReasonDisabled && disable == DisableNoop:
			// Unread only because of its `disable` — every part of the day
			// the key is unread in, it reads `disable` — and that switches
			// nothing off. A key with no row in some part of the day for
			// another reason (no_row: a key the platform does not declare)
			// keeps that reason and its severity (#3835).
			severity, reason = SeverityWarning, ReasonDisableNoop
		case reason == ReasonStateFilterUndeclared:
			// The root's missing declaration, not a misspelling (decision 7).
			severity = SeverityNotice
		default:
			severity = SeverityError
		}
		if reason == "" && !hasSk {
			reason = ReasonNotServed
		}
	}
	return KeyVerdict{
		Tenant: tenant, Key: key, Layer: nilIfEmpty(layer, hasSrc), File: nilIfEmpty(src.File, hasSrc),
		ReadBy: sortedKeys(readBy), Verdict: verdict, Severity: optional(severity), Reason: optional(reason),
		Disable: optional(disable), Hint: optional(hint), SwitchedOffWith: optional(sk.SwitchedOffWith),
		DeadEntries: nonNilEntries(sk.DeadEntries), Value: value,
	}
}

// namedByPolicy: a rule that applies to the tenant names the key itself, in
// either #1231 spelling (canonicalSpelling).
// A wildcard head (`mysql_*`) is compared as written, so it names only a key
// literally spelt that way — never every misspelling it would match.
func namedByPolicy(tenant, key string, heads []PolicyHead, policy func(string) string) bool {
	c := policy(key)
	for _, h := range heads {
		if !h.Excluded[tenant] && policy(h.Head) == c {
			return true
		}
	}
	return false
}

// canonicalSpelling is a key in the exporter's canonical spelling
// (config.CanonicalKeyFor): the one rule every reader compares #1231
// spellings by — Policy-as-Code included, which asks it through `da-guard
// key-spellings` rather than keeping a copy (#3556).
func canonicalSpelling(key string) string {
	c, _ := config.CanonicalKeyFor(key)
	return c
}

// groupKey groups the findings of keys: (kind, file, key, own tenant,
// reading).
type groupKey struct {
	kind, key string
	file      *string
	tenant    string // the tenant for a key of the tenant's own file; "" else
	// reading: a dead_entry's entries (config.DeadEntriesReading) — a root
	// platform file's `tenants:` entries write one key with one value per
	// tenant, so the same file and key are not the same entries.
	reading string
}

func (g groupKey) id() string {
	f := "\x01"
	if g.file != nil {
		f = *g.file
	}
	return g.kind + "\x00" + f + "\x00" + g.key + "\x00" + g.tenant + "\x00" + g.reading
}

// findingsOf is the findings of keys: deduplicated by (kind, file, key) for
// platform-side keys; one per tenant key for the tenant's own file. An
// undeclared state filter is the platform's: one finding per key, on the
// root `_defaults.yaml` that should declare it, with every tenant it
// reaches — never charged to a tenant's own file (decision 6, 7).
func findingsOf(keys map[string][]KeyVerdict, rootDefaults map[string]string) []Finding {
	type group struct {
		gk groupKey
		vs []KeyVerdict
	}
	var order []string
	groups := map[string]*group{}
	add := func(gk groupKey, v KeyVerdict) {
		id := gk.id()
		g, ok := groups[id]
		if !ok {
			g = &group{gk: gk}
			groups[id] = g
			order = append(order, id)
		}
		g.vs = append(g.vs, v)
	}
	for _, tenant := range sortedKeys(keys) {
		for _, v := range keys[tenant] {
			var kinds []string
			if v.Verdict == Unread {
				k := KindUnread
				if deref(v.Reason) == ReasonDisableNoop {
					k = KindDisableNoop
				}
				kinds = append(kinds, k)
			}
			if v.SwitchedOffWith != nil && deref(v.Layer) == config.KeyLayerTenant {
				// #3046: the tenant's own sibling the base's `disable`
				// switches off — a sibling it inherits is the cascade asked.
				kinds = append(kinds, KindDisableCascade)
			}
			if len(v.DeadEntries) > 0 && v.Verdict == Read {
				// #3502: read in some part of the day, and part of its
				// schedule never applies — the schedule as written, judged
				// whatever --at is (#3835). A key no reader reads all day has
				// its own finding; its entries are not judged on top of it.
				kinds = append(kinds, KindDeadEntry)
			}
			for _, kind := range kinds {
				var gk groupKey
				if deref(v.Reason) == ReasonStateFilterUndeclared {
					// The platform's to fix: `state_filters:` is declared in
					// the root `_defaults.yaml` only.
					var root *string
					if r, ok := rootDefaults[v.Tenant]; ok {
						root = &r
					}
					gk = groupKey{kind: kind, file: root, key: v.Key}
				} else {
					gk = groupKey{kind: kind, file: v.File, key: v.Key}
					if deref(v.Layer) == config.KeyLayerTenant {
						gk.tenant = v.Tenant
					}
					if kind == KindDeadEntry {
						gk.reading = config.DeadEntriesReading(v.DeadEntries)
					}
				}
				add(gk, v)
			}
		}
	}
	out := make([]Finding, 0, len(order))
	for _, id := range order {
		g := groups[id]
		sev := deref(g.vs[0].Severity)
		if sev == "" {
			sev = SeverityNotice
		}
		tenantSet := map[string]bool{}
		for _, v := range g.vs {
			tenantSet[v.Tenant] = true
		}
		f := Finding{Severity: sev, Kind: g.gk.kind, Key: g.gk.key, File: g.gk.file,
			Tenants: sortedKeys(tenantSet), Reason: g.vs[0].Reason}
		if g.gk.kind == KindUnread {
			f.Hint = g.vs[0].Hint
		}
		switch g.gk.kind {
		case KindDisableCascade:
			reason := ReasonBaseDisabled
			f.Severity, f.Reason, f.Base = SeverityWarning, &reason, g.vs[0].SwitchedOffWith
		case KindDeadEntry:
			des := g.vs[0].DeadEntries
			reason := des[0].Reason
			f.Severity, f.Reason, f.DeadEntries, f.reading = SeverityError, &reason, des, g.gk.reading
		}
		out = append(out, f)
	}
	sortFindings(out)
	return out
}

// valueFindings is a value_unread finding — schedule_unread for a value
// whose problem is its schedule (LayerValueProblem.Schedule) — for every
// value of values about a tenant the verdict judges (tenants): in a tenant file an error on that
// tenant's entry; in a root platform file's `tenants:` entry a notice on
// that tenant; in a profile a notice naming the judged tenants that bind it
// ([] for none). Never charged to a tenant through a layer it inherits
// (config.LayerValueProblems). With no root platform file read
// (undecidable), a name the root files would define (`_profile`) is not
// judged: which profiles exist is not known (decision 7).
func valueFindings(values []config.LayerValueProblem, tenants map[string]bool,
	effective map[string]EffectiveTenant, undecidable bool) []Finding {
	var out []Finding
	for _, v := range values {
		if undecidable && v.RefersToRoot {
			continue
		}
		file, reason := v.File, v.Problem
		kind := KindValueUnread
		if v.Schedule {
			kind = KindScheduleUnread
		}
		f := Finding{Kind: kind, Key: v.Key, File: &file, Reason: &reason, reading: v.Reading}
		switch v.Layer {
		case config.ValueLayerTenant, config.ValueLayerPlatform:
			if !tenants[v.Owner] {
				continue
			}
			f.Tenants = []string{v.Owner}
			f.Severity, f.own = SeverityNotice, false
			if v.Layer == config.ValueLayerTenant {
				f.Severity, f.own = SeverityError, true
			}
		case config.ValueLayerProfile:
			bound := map[string]bool{}
			for t, te := range effective {
				if tenants[t] && te.Profile != "" && te.Profile == v.Owner {
					bound[t] = true
				}
			}
			f.Severity, f.Tenants = SeverityNotice, sortedKeys(bound)
		default:
			continue
		}
		out = append(out, f)
	}
	return out
}

// tenantFileFindings is one unread error per top-level key of a tenant file
// that config.TenantFileTopLevelKey does not admit, on that file (#3671). It
// names no tenant: the key is in no tenant's config, so no tenant's verdict
// row carries it — and a tenant entry the same file writes under the same
// name is the tenant's own key, judged on its own.
func tenantFileFindings(files map[string][]string) []Finding {
	var out []Finding
	for _, file := range sortedKeys(files) {
		for _, key := range files[file] {
			if config.TenantFileTopLevelKey(key) {
				continue
			}
			out = append(out, Finding{Severity: SeverityError, Kind: KindUnread, Key: key, File: optional(file),
				Tenants: []string{}, Reason: optional(ReasonTenantFileTopLevel)})
		}
	}
	return out
}

// profileLayerFindings: the notices for `_` keys written in a profile that
// no reader reads in the `profile` layer.
func profileLayerFindings(byFile map[string]map[string]any, effective map[string]EffectiveTenant) []Finding {
	type found struct {
		reason  string
		tenants map[string]bool
	}
	got := map[[2]string]*found{}
	for _, file := range sortedKeys(byFile) {
		for name, body := range byFile[file] {
			m, ok := body.(map[string]any)
			if !ok {
				continue
			}
			bound := map[string]bool{}
			for t, te := range effective {
				if te.Profile != "" && te.Profile == name {
					bound[t] = true
				}
			}
			for key := range m {
				if !strings.HasPrefix(key, "_") {
					continue
				}
				declared, registered := config.RegisteredReaders(key)
				inProfile := false
				for _, layers := range declared {
					if contains(layers, "profile") {
						inProfile = true
					}
				}
				if inProfile {
					continue
				}
				reason := ReasonUnregistered
				switch {
				case config.RetiredHint(key) != "":
					reason = ReasonRetired
				case registered:
					reason = ReasonNotReadInProfile
				}
				fk := [2]string{file, key}
				if got[fk] == nil {
					got[fk] = &found{reason: reason, tenants: map[string]bool{}}
				}
				for t := range bound {
					got[fk].tenants[t] = true
				}
			}
		}
	}
	out := make([]Finding, 0, len(got))
	for fk, f := range got {
		file, reason := fk[0], f.reason
		out = append(out, Finding{Severity: SeverityError, Kind: KindUnread, Key: fk[1], File: &file,
			Tenants: sortedKeys(f.tenants), Reason: &reason})
	}
	return out
}

// profileNotFilled is a row for every key the tenant's profile writes that
// the exporter does not fill in — the root declares it without a platform
// value (#1189) — as effective names it (not_served profile_not_filled,
// #3395): no reader reads the profile's value, so it is unread, on the
// profile file. A key the tenant's config holds otherwise (all) has its
// own row.
func profileNotFilled(tenant string, notServed map[string]config.NotServedKey, all map[string]bool) []KeyVerdict {
	var out []KeyVerdict
	for _, key := range sortedKeys(notServed) {
		ns := notServed[key]
		if ns.Reason != config.NotServedProfileNotFilled || all[key] {
			continue
		}
		layer, file := config.KeyLayerProfile, ns.File
		sev, reason := SeverityError, ns.Reason
		out = append(out, KeyVerdict{Tenant: tenant, Key: key, Layer: &layer, File: &file, ReadBy: []string{},
			Verdict: Unread, Severity: &sev, Reason: &reason, DeadEntries: []config.DeadEntry{}})
	}
	return out
}

// unboundProfileFindings is a notice for every threshold key of a profile
// no tenant binds (#3395, ADR-038 decision 4): whether the exporter reads
// it is what it does on a tenant, and no tenant has it, so it is not
// judged — said so, once per file and key, rather than left to read as
// judged and fine. A `_` key there is judged by the registry
// (profileLayerFindings) and is not listed here.
func unboundProfileFindings(byFile map[string]map[string]any, effective map[string]EffectiveTenant) []Finding {
	bound := map[string]bool{}
	for _, te := range effective {
		if te.Profile != "" {
			bound[te.Profile] = true
		}
	}
	got := map[[2]string]bool{}
	for file, profiles := range byFile {
		for name, body := range profiles {
			m, ok := body.(map[string]any)
			if !ok || bound[name] {
				continue
			}
			for key := range m {
				if !strings.HasPrefix(key, "_") {
					got[[2]string{file, key}] = true
				}
			}
		}
	}
	out := make([]Finding, 0, len(got))
	for fk := range got {
		file, reason := fk[0], ReasonProfileUnbound
		out = append(out, Finding{Severity: SeverityNotice, Kind: KindProfileNotJudged, Key: fk[1], File: &file,
			Tenants: []string{}, Reason: &reason})
	}
	return out
}

// mergeProfileFindings is findings with the profile-layer judgment taking
// the place of a tenant-path unread finding that only profile-layer keys
// gave — the same (file, key) — and adding the rest. A (file, key) the
// tenants also get from a root file's `tenants:` keeps the tenant-path
// finding; one some tenant's reader reads (a Policy-as-Code rule naming it)
// is no finding.
func mergeProfileFindings(keys map[string][]KeyVerdict, findings, byProfile []Finding) []Finding {
	fk := func(file *string, key string) [2]string {
		return [2]string{deref(file) + "\x00" + boolStr(file != nil), key}
	}
	layers := map[[2]string]map[string]bool{}
	read := map[[2]string]bool{}
	for _, rows := range keys {
		for _, v := range rows {
			k := fk(v.File, v.Key)
			switch {
			case v.Verdict == Unread:
				if layers[k] == nil {
					layers[k] = map[string]bool{}
				}
				layers[k][deref(v.Layer)+"\x00"+boolStr(v.Layer != nil)] = true
			case v.Verdict == Read && deref(v.Layer) == config.KeyLayerProfile:
				read[k] = true
			}
		}
	}
	var kept []Finding
	for _, f := range byProfile {
		if !read[fk(f.File, f.Key)] {
			kept = append(kept, f)
		}
	}
	byProfile = kept
	profileOnly := map[[2]string]bool{}
	profileLayer := config.KeyLayerProfile + "\x00true"
	for k, ls := range layers {
		if len(ls) == 1 && ls[profileLayer] {
			profileOnly[k] = true
		}
	}
	ours := map[[2]string]bool{}
	for _, f := range byProfile {
		ours[fk(f.File, f.Key)] = true
	}
	out := make([]Finding, 0, len(findings)+len(byProfile))
	theirs := map[[2]string]bool{}
	for _, f := range findings {
		k := fk(f.File, f.Key)
		if f.Kind == KindUnread && profileOnly[k] && ours[k] {
			continue
		}
		out = append(out, f)
		if f.Kind == KindUnread {
			theirs[k] = true
		}
	}
	for _, f := range byProfile {
		if !theirs[fk(f.File, f.Key)] {
			out = append(out, f)
		}
	}
	return out
}

// sortFindings orders findings by severity (error, warning, notice), file
// ("" for none), key, kind, tenants.
func sortFindings(fs []Finding) {
	rank := map[string]int{SeverityError: 0, SeverityWarning: 1, SeverityNotice: 2}
	r := func(s string) int {
		if v, ok := rank[s]; ok {
			return v
		}
		return 3
	}
	sort.SliceStable(fs, func(i, j int) bool {
		a, b := fs[i], fs[j]
		if r(a.Severity) != r(b.Severity) {
			return r(a.Severity) < r(b.Severity)
		}
		if deref(a.File) != deref(b.File) {
			return deref(a.File) < deref(b.File)
		}
		if a.Key != b.Key {
			return a.Key < b.Key
		}
		if a.Kind != b.Kind {
			return a.Kind < b.Kind
		}
		return lessStrings(a.Tenants, b.Tenants)
	})
}

// lessStrings is Python's tuple order on two string tuples.
func lessStrings(a, b []string) bool {
	for i := 0; i < len(a) && i < len(b); i++ {
		if a[i] != b[i] {
			return a[i] < b[i]
		}
	}
	return len(a) < len(b)
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func sortedKeys[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// nonNilEntries is des, [] for none (a JSON list, never null).
func nonNilEntries(des []config.DeadEntry) []config.DeadEntry {
	if des == nil {
		return []config.DeadEntry{}
	}
	return des
}

func optional(s string) *string {
	if s == "" {
		return nil
	}
	return &s
}

func nilIfEmpty(s string, present bool) *string {
	if !present {
		return nil
	}
	return &s
}

func deref(s *string) string {
	if s == nil {
		return ""
	}
	return *s
}

func boolStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
}
