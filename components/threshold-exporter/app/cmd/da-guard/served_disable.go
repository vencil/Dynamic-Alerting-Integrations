package main

// served_disable.go — served-values --disable-effects (ADR-038 decision 8):
// for every key a tenant switches off (`disable`, in any part of the day),
// whether switching it off changes the user_threshold rows /metrics serves.
// A `_` key the reserved-key registry lists is left out (registeredReserved).
//
// ⛔ A COUNTERFACTUAL, NOT A RULE. Nothing here decides from a key's name or
// from the defaults whether a `disable` has something to switch off — the
// exporter reads `disable` in five places (base, declared, `_critical`,
// dimensional, state filter), and a rule written here would drift from
// them. Instead the tree is resolved again with the `disable` taken away
// (the key left out of the tenant's map in every part of the day in which it
// reads `disable`; the parts in which it reads a value keep it), through the
// same collector and Gather as /metrics, at every cut of the tenant's day
// (config.ScheduleCuts), and the tenant's served rows are compared:
//
//   - unchanged in every part of the day → noop;
//   - changed → effective.
//
// A base's `disable` switches its whole family off (#3046: the resolver's
// familyHeadOff), so no row of the family (`<key>_critical`, `<key>{…}`: a
// /metrics series whose metric_key is the key) is served in a part of the
// day in which the base reads `disable`. That is checked here, on the
// baseline of every part judged (familyLeak): a family row served there is
// an internal error — a phase the cascade does not reach — never a verdict.
//
// Cost: a tenant's user_threshold rows depend on its own map and the
// platform's only, so each tenant is judged at its own schedule cuts, over a
// tree holding only the tenants judged with it. Round r takes away every
// tenant's r-th switched-off key at once: twice per round and cut, a tree of
// that round's tenants is resolved — rounds being the most keys one tenant
// switches off, not the number of tenants. A round whose Gather fails where
// the baseline's does not is split, tenant by tenant, so one tenant's
// collision is not read as another tenant's change.
//
// ⚠️ The key is taken out of the tenant's MERGED map: a `disable` that a
// lower layer (a profile, a platform `tenants:` entry) also writes is judged
// against the platform default, not against that lower layer's `disable`.

import (
	"errors"
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// Values of servedKey.Disable.
const (
	disableEffective = "effective"
	disableNoop      = "noop"
)

// switchedOff reports whether sv reads `disable` in some part of the day.
func switchedOff(sv config.ScheduledValue) bool {
	for _, seg := range sv.DaySegments() {
		if config.IsDisabled(strings.ToLower(strings.TrimSpace(seg.Value))) {
			return true
		}
	}
	return false
}

// registeredReserved reports whether k is a `_` key the reserved-key
// registry lists: its `disable` acts on a reading other than the
// user_threshold rows compared here (a state filter, a silence, the
// dedup sentinel, a route), and its reader judges it.
func registeredReserved(k string) bool {
	if !strings.HasPrefix(k, "_") {
		return false
	}
	_, ok := config.RegisteredReaders(k)
	return ok
}

// tenantKey is one (tenant, key) of the tree's merged config.
type tenantKey struct{ tenant, key string }

// disableEffects is the effect of every switched-off key of cfg: tenant →
// key (as cfg spells it) → its effect.
func disableEffects(cfg *config.ThresholdConfig, at time.Time, name keyNamer, quiet bool) (
	map[string]map[string]disableEffect, error,
) {
	perTenant := map[string][]string{}
	rounds := 0
	for tenant, overrides := range cfg.Tenants {
		for k, sv := range overrides {
			if switchedOff(sv) && !registeredReserved(k) {
				perTenant[tenant] = append(perTenant[tenant], k)
			}
		}
		sort.Strings(perTenant[tenant])
		if n := len(perTenant[tenant]); n > rounds {
			rounds = n
		}
	}
	out := map[string]map[string]disableEffect{}
	if rounds == 0 {
		return out, nil
	}

	// Judged tenant by tenant at the tenant's own cuts, over a tree holding
	// only the round's tenants: a tenant's user_threshold rows depend on its
	// own map and the platform's, never on another tenant's.
	ownCuts := map[string]map[int]bool{}
	for tenant := range perTenant {
		ownCuts[tenant] = map[int]bool{}
		for _, m := range onlyTenants(cfg, []string{tenant}).ScheduleCuts() {
			ownCuts[tenant][m] = true
		}
	}
	changed := map[tenantKey]bool{}
	// cascaded: tenant → family key → the base whose `disable` switches it
	// off in some part of the day (#3046) — a key of the base's family
	// served with the `disable` taken away and not served with it.
	cascaded := map[string]map[string]string{}
	var judge func(set []tenantKey, m int) error
	judge = func(set []tenantKey, m int) error {
		tenants := make([]string, len(set))
		for i, tk := range set {
			tenants[i] = tk.tenant
		}
		part := onlyTenants(cfg, tenants).AtMinuteOfDay(m)
		base, err := readCut(part, at, name, quiet)
		if err != nil {
			return err
		}
		cf, err := readCut(withoutDisables(part, set), at, name, quiet)
		if err != nil {
			return err
		}
		if (cf.failed || base.failed) && len(set) > 1 {
			// A Gather that fails fails for every tenant in it: which tenant's
			// rows broke it is not known, so one by one. (A tenant judged alone
			// whose own part of the day cannot be gathered either way gives no
			// evidence there; its other parts of the day decide.)
			for _, tk := range set {
				if err := judge([]tenantKey{tk}, m); err != nil {
					return err
				}
			}
			return nil
		}
		for _, tk := range set {
			if cf.fingerprint(tk.tenant) != base.fingerprint(tk.tenant) {
				changed[tk] = true
			}
			if err := familyLeak(part, base, tk, at); err != nil {
				return err
			}
			if !cf.failed && !base.failed {
				for _, k := range switchedOffFamily(base.series[tk.tenant], cf.series[tk.tenant], tk.key) {
					if cascaded[tk.tenant] == nil {
						cascaded[tk.tenant] = map[string]string{}
					}
					cascaded[tk.tenant][k] = tk.key
				}
			}
		}
		return nil
	}
	for round := 0; round < rounds; round++ {
		byCut := map[int][]tenantKey{}
		for tenant, keys := range perTenant {
			if round >= len(keys) {
				continue
			}
			for m := range ownCuts[tenant] {
				byCut[m] = append(byCut[m], tenantKey{tenant, keys[round]})
			}
		}
		cuts := make([]int, 0, len(byCut))
		for m := range byCut {
			cuts = append(cuts, m)
		}
		sort.Ints(cuts)
		for _, m := range cuts {
			set := byCut[m]
			sort.Slice(set, func(i, j int) bool { return set[i].tenant < set[j].tenant })
			if err := judge(set, m); err != nil {
				return nil, err
			}
		}
	}

	for tenant, keys := range perTenant {
		out[tenant] = map[string]disableEffect{}
		for _, k := range keys {
			e := disableEffect{effect: disableNoop}
			if changed[tenantKey{tenant, k}] {
				e.effect = disableEffective
			}
			out[tenant][k] = e
		}
	}
	for tenant, byKey := range cascaded {
		if out[tenant] == nil {
			out[tenant] = map[string]disableEffect{}
		}
		for k, base := range byKey {
			e := out[tenant][k]
			e.switchedOffWith = base
			out[tenant][k] = e
		}
	}
	return out, nil
}

// switchedOffFamily is the keys of key's family (a /metrics series whose
// metric_key is key, the key itself aside) served in one part of the day
// with key's `disable` taken away (without) and not served with it (with):
// the keys the `disable` switches off with its base (#3046), read off
// /metrics, not decided by a rule here. Sorted, as cfg spells them.
func switchedOffFamily(with, without map[string][]metricsSeries, key string) []string {
	canon, _ := config.CanonicalKeyFor(key)
	var out []string
	for k, ss := range without {
		if kc, _ := config.CanonicalKeyFor(k); kc == canon {
			continue
		}
		if _, served := with[k]; served {
			continue
		}
		for _, s := range ss {
			if s.MetricKey == canon {
				out = append(out, k)
				break
			}
		}
	}
	sort.Strings(out)
	return out
}

// onlyTenants is cfg holding only the given tenants (their maps shared).
func onlyTenants(cfg *config.ThresholdConfig, tenants []string) *config.ThresholdConfig {
	out := *cfg
	out.Tenants = make(map[string]map[string]config.ScheduledValue, len(tenants))
	for _, t := range tenants {
		out.Tenants[t] = cfg.Tenants[t]
	}
	return &out
}

// disableEffect is one key's reading under --disable-effects: for a key
// that reads `disable`, its effect; for a key of a family a base's
// `disable` switches off in some part of the day, that base (#3046).
type disableEffect struct {
	effect          string
	switchedOffWith string
}

// withoutDisables is cfg (one part of the day: every value fixed) with each
// key of set left out of its tenant's map where it reads `disable`. The maps
// of the other tenants are shared, not copied.
func withoutDisables(cfg *config.ThresholdConfig, set []tenantKey) *config.ThresholdConfig {
	out := *cfg
	out.Tenants = make(map[string]map[string]config.ScheduledValue, len(cfg.Tenants))
	for t, m := range cfg.Tenants {
		out.Tenants[t] = m
	}
	for _, tk := range set {
		sv, ok := cfg.Tenants[tk.tenant][tk.key]
		if !ok || !config.IsDisabled(strings.ToLower(strings.TrimSpace(sv.Default))) {
			continue
		}
		m := make(map[string]config.ScheduledValue, len(out.Tenants[tk.tenant]))
		for k, v := range out.Tenants[tk.tenant] {
			if k != tk.key {
				m[k] = v
			}
		}
		out.Tenants[tk.tenant] = m
	}
	return &out
}

// cutReading is what /metrics serves in one part of the day: per tenant, the
// rows it keeps (a row the collector drops is not served: ADR-038 decision 8
// compares what is served), and the series of each kept key. failed: the
// Gather fails, nothing is served for anyone.
type cutReading struct {
	failed bool
	rows   map[string]string                     // tenant → fingerprint
	series map[string]map[string][]metricsSeries // tenant → key → series
}

func (r *cutReading) fingerprint(tenant string) string {
	if r.failed {
		return "\x00not gatherable"
	}
	return r.rows[tenant]
}

// readCut reads cfg (one part of the day) through keyedRowsWith, silently.
// A Gather failure is a reading (failed), not an error.
func readCut(cfg *config.ThresholdConfig, at time.Time, name keyNamer, quiet bool) (*cutReading, error) {
	served, _, series, _, err := keyedRowsWith(cfg, at, (*config.ThresholdConfig).ResolveAtWithKeysSilent, name, quiet)
	var ng *notGatherableError
	if errors.As(err, &ng) {
		return &cutReading{failed: true}, nil
	}
	if err != nil {
		return nil, err
	}
	r := &cutReading{rows: map[string]string{}, series: series}
	for t := range served {
		var parts []string
		for k, rows := range served[t] {
			for _, row := range rows {
				parts = append(parts, "kept\x00"+k+"\x00"+rowOrder(row))
			}
		}
		sort.Strings(parts)
		r.rows[t] = strings.Join(parts, "\x01")
	}
	return r, nil
}

// familyLeak is the #3046 invariant on one part of the day (part, its
// baseline reading base): when the tenant switches tk.key's family off there
// (config.FamilyHeadOffAt, the resolver's own reading), no row of the
// family is served. A family row served is an internal error: the cascade
// misses a phase. Nothing to check when the part cannot be gathered.
func familyLeak(part *config.ThresholdConfig, base *cutReading, tk tenantKey, at time.Time) error {
	if base.failed || !part.FamilyHeadOffAt(tk.tenant, tk.key, at) {
		return nil
	}
	if fam := familyServed(base.series[tk.tenant], tk.key); len(fam) > 0 {
		return fmt.Errorf("internal: tenant %s switches %q off, yet its family %v is served (#3046)", tk.tenant, tk.key, fam)
	}
	return nil
}

// familyServed is the keys other than key whose /metrics series carry key as
// their metric_key (its `_critical`, its dimensional spellings) in one part
// of the tenant's day (series: that part's), sorted.
func familyServed(series map[string][]metricsSeries, key string) []string {
	canon, _ := config.CanonicalKeyFor(key)
	var out []string
	for k, ss := range series {
		if k == canon {
			continue
		}
		for _, s := range ss {
			if s.MetricKey == canon {
				out = append(out, k)
				break
			}
		}
	}
	sort.Strings(out)
	return out
}
