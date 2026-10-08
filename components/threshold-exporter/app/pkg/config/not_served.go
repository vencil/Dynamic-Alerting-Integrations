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
//     the root decode has no field for the key (FlatBuild.RootDefaultsUnread).
//   - value_rejected: applySubtreeDefaults refuses a subtree level's value
//     (not threshold-shaped) and the tenant keeps a shallower one
//     (FlatBuild.RejectedChainValues).
//   - value_unparsed: resolveBaseRows cannot parse the tenant's value and
//     serves the platform default instead (rejectRecorder).
//   - value_unparsed_dropped: the critical / dimensional / declared phase
//     cannot parse the value and serves no row for it (rejectRecorder).
//   - undeliverable: a subtree key the output plane never iterates (#1976,
//     FlatBuild.UnreachableValues through undeliverableThresholds).
//   - root_null_undeclared: the root writes the key as null (#2518,
//     FlatBuild.RootNullUndeclared).
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
	NotServedUndeliverable         = "undeliverable"
	NotServedRootNullUndeclared    = "root_null_undeclared"
)

// NotServedKey is why /metrics does not serve one key of an effective config
// as the effective config shows it.
type NotServedKey struct {
	// Reason is one of the NotServed* constants.
	Reason string `json:"reason"`
	// File is the root-relative slash path of the file whose value the
	// effective config shows (the key's winning layer); empty when no single
	// file applies.
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

// rootDefaultsUnread is FlatBuild.RootDefaultsUnread for the root carrier's
// bytes, which the root decode accepted as partial: when the document has no
// `defaults:` mapping — so the defaults-chain merge (ExtractDefaultsBlock)
// takes the whole document as the block — its top-level keys the root decode
// has no field for, sorted. nil otherwise.
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
		if !rootDecodedKeySet[k] {
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
// cannot parse (#2296): tenant → canonical key → reason (NotServedValueUnparsed
// or NotServedValueUnparsedDropped). nil = record nothing, which is what every
// scrape passes: the record sites sit inside the resolver's existing WARN
// branches, so the steady state pays one nil check per unparseable value.
type rejectRecorder struct {
	byTenant map[string]map[string]string
}

// note records key of tenant under reason; a dropped row outranks a value
// the resolver fell back from (a key read at several minutes keeps the
// stronger verdict).
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
	if m[key] == NotServedValueUnparsedDropped {
		return
	}
	m[key] = reason
}

// recordUnparsed resolves cfg at every ScheduleCuts minute of the UTC day
// with a recorder and returns what it recorded: the verdict does not depend
// on the time of day the request is made. Silent (logf nil).
func recordUnparsed(cfg *ThresholdConfig, now time.Time) map[string]map[string]string {
	rec := &rejectRecorder{}
	for _, m := range cfg.ScheduleCuts() {
		cfg.AtMinuteOfDay(m).resolveAtWithStats(now, nil, nil, rec)
	}
	return rec.byTenant
}

// servedVerdicts is one build's not-served tables, for the effective
// resolver (#2296). Built from the build ScopeEffective / ResolveEffective
// already ran over the same scan.
type servedVerdicts struct {
	parseFailed   map[string]bool            // root-relative scan keys (FlatBuild.ParseFailed)
	rootUnread    map[string]bool            // root carrier keys (FlatBuild.RootDefaultsUnread)
	rejected      map[string]map[string]bool // file → keys (FlatBuild.RejectedChainValues)
	undeliverable map[string]map[string]bool // tenant → keys (undeliverableThresholds)
	rootNull      map[string]map[string]bool // tenant → keys (FlatBuild.RootNullUndeclared)

	// cfg is the build's config; unparsed is recordUnparsed over it,
	// computed on the first tenant that asks (all, when all is set — the
	// whole-tree mode resolves every tenant — else only that tenant's copy).
	cfg      *ThresholdConfig
	all      bool
	unparsed map[string]map[string]string
	done     bool
}

func newServedVerdicts(built *FlatBuild, all bool) *servedVerdicts {
	v := &servedVerdicts{cfg: &built.Config, all: all}
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
	v.rejected = built.RejectedChainValues
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

// unparsedOf is recordUnparsed's verdicts for tenantID.
func (v *servedVerdicts) unparsedOf(tenantID string) map[string]string {
	if v.all {
		if !v.done {
			v.unparsed = recordUnparsed(v.cfg, time.Now())
			v.done = true
		}
		return v.unparsed[tenantID]
	}
	overrides, ok := v.cfg.Tenants[tenantID]
	if !ok {
		return nil
	}
	one := *v.cfg
	one.Tenants = map[string]map[string]ScheduledValue{tenantID: overrides}
	return recordUnparsed(&one, time.Now())[tenantID]
}

// chainFileUnread reports whether chain file rel is one /metrics does not
// read at all because it does not parse — the effective resolve then reads
// it as an empty file and names it in ChainParseFailed.
func (v *servedVerdicts) chainFileUnread(rel string) bool {
	return v.parseFailed[rel]
}

// candidates reports whether any table could name a key of this tenant, so
// a tenant with none skips the attribution (keySources) entirely.
func (v *servedVerdicts) candidates(tenantID string, chain []string, unparsed map[string]string) bool {
	if len(v.parseFailed) > 0 || len(v.rootUnread) > 0 || len(unparsed) > 0 ||
		len(v.undeliverable[tenantID]) > 0 || len(v.rootNull[tenantID]) > 0 {
		return true
	}
	for _, f := range chain {
		if len(v.rejected[f]) > 0 {
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
		case atRoot && v.rootUnread[k]:
			reason = NotServedRootDefaultsUnwrapped
		case src.Layer == KeyLayerDefaults && !atRoot && v.rejected[src.File][k]:
			reason = NotServedValueRejected
		case unparsed[canon] == NotServedValueUnparsedDropped:
			reason = NotServedValueUnparsedDropped
		case unparsed[canon] == NotServedValueUnparsed && !atRoot:
			// At the root, the default /metrics falls back to IS the value
			// the effective config shows.
			reason = NotServedValueUnparsed
		case v.undeliverable[tenantID][k]:
			reason = NotServedUndeliverable
		case v.rootNull[tenantID][k]:
			reason = NotServedRootNullUndeclared
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

// relRejected converts applySubtreeDefaults' absolute-path rejection table
// to root-relative slash paths (FlatBuild.RejectedChainValues).
func relRejected(rootDir string, byAbs map[string]map[string]bool) map[string]map[string]bool {
	if len(byAbs) == 0 {
		return nil
	}
	out := make(map[string]map[string]bool, len(byAbs))
	for p, keys := range byAbs {
		out[subtreeRelPath(rootDir, filepath.Clean(p))] = keys
	}
	return out
}
