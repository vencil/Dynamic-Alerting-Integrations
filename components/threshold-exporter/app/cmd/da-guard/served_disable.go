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
//   - changed, and a row of the same family (`<key>_critical`, `<key>{…}`:
//     a /metrics series whose metric_key is the key) is still served in
//     some part of the day → partial;
//   - changed otherwise → effective.
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
	"sort"
	"strings"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// Values of servedKey.Disable.
const (
	disableEffective = "effective"
	disablePartial   = "partial"
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
// key (as cfg spells it) → its effect and, for partial, the family keys still
// served (sorted, as cfg spells them).
func disableEffects(cfg *config.ThresholdConfig, at time.Time, name keyNamer) (
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
	family := map[string]map[string][]metricsSeries{} // tenant → baseline series, every part of its day
	var judge func(set []tenantKey, m int) error
	judge = func(set []tenantKey, m int) error {
		tenants := make([]string, len(set))
		for i, tk := range set {
			tenants[i] = tk.tenant
		}
		part := onlyTenants(cfg, tenants).AtMinuteOfDay(m)
		base, err := readCut(part, at, name)
		if err != nil {
			return err
		}
		cf, err := readCut(withoutDisables(part, set), at, name)
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
			if !base.failed {
				if family[tk.tenant] == nil {
					family[tk.tenant] = map[string][]metricsSeries{}
				}
				for k, ss := range base.series[tk.tenant] {
					family[tk.tenant][k] = append(family[tk.tenant][k], ss...)
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
				if fam := familyServed(family[tenant], k); len(fam) > 0 {
					e.effect, e.family = disablePartial, fam
				}
			}
			out[tenant][k] = e
		}
	}
	return out, nil
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

// disableEffect is one switched-off key's effect.
type disableEffect struct {
	effect string
	family []string
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
func readCut(cfg *config.ThresholdConfig, at time.Time, name keyNamer) (*cutReading, error) {
	served, _, series, _, err := keyedRowsWith(cfg, at, (*config.ThresholdConfig).ResolveAtWithKeysSilent, name)
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

// familyServed is the keys other than key whose /metrics series carry key as
// their metric_key (its `_critical`, its dimensional spellings) in some part
// of the tenant's day (series: the baseline's, every part), sorted.
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
