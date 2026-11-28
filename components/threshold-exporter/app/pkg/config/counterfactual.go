package config

// The counterfactual of a tenant line (#4330): does deleting key from the
// tenant's own file change what the exporter serves for that tenant?
//
// ⛔ NO READING OF ITS OWN. The answer is two builds and two resolves by the
// exporter's own code: the tree as written, and the tree with the line
// deleted from the file's decoded config, built by BuildFlatConfig (subtree
// defaults, profiles, platform entries and the root all applied as the
// exporter applies them) and resolved at every minute of the UTC day at
// which either side's schedules may serve a different value. A predicate
// comparing the tenant's value with the inherited one is what this replaces:
// each such predicate missed a shape (#4288, #4330).
//
// A tenant's rows depend only on its own built map and the config-wide
// settings, so one build answers one candidate of every tenant at once:
// round i deletes the i-th candidate of each tenant (TestServedUnchangedWithout_RoundsMatchOneAtATime
// pins that against one build per candidate).

import (
	"fmt"
	"io"
	"log"
	"sort"
	"time"
)

// ServedUnchangedWithout reports, for every tenant of cands and every key of
// its list (keys as the tenant's own file spells them), whether deleting the
// key from that file leaves the threshold rows the exporter serves for the
// tenant the same at every minute of the UTC day around now. A key the
// tenant's file does not hold, or a tenant whose file this scope cannot
// give, is reported false (not unchanged): the caller then gives no advice.
func (s *ScopedTenants) ServedUnchangedWithout(cands map[string][]string, now time.Time) (map[string]map[string]bool, error) {
	if s == nil || s.scan == nil || s.built == nil {
		return nil, fmt.Errorf("counterfactual needs the scope's scan and build")
	}
	quiet := log.New(io.Discard, "", 0)
	in := loadDirBuildInput(s.scan, s.scan.AbsRoot, quiet, discardLogf)
	base := &s.built.Config

	files := map[string]string{} // tenant → its own file (RelKey)
	for _, ec := range s.Tenants {
		files[ec.TenantID] = ec.SourceFile
	}
	partials := map[string]ThresholdConfig{} // file → its decoded config
	partialOf := func(name string) (ThresholdConfig, bool) {
		if p, ok := partials[name]; ok {
			return p, true
		}
		p, ok := s.scan.Partials[name]
		if !ok {
			f := s.scan.Files[name]
			if f == nil {
				return ThresholdConfig{}, false
			}
			data := f.Data
			if data == nil {
				var err error
				if data, err = ReadConfFile(f.AbsPath); err != nil {
					return ThresholdConfig{}, false
				}
			}
			if p, ok = parsePartialConfig(name, f.AbsPath, data, nil, quiet); !ok {
				return ThresholdConfig{}, false
			}
		}
		partials[name] = p
		return p, true
	}

	out := make(map[string]map[string]bool, len(cands))
	rounds := 0
	for tenant, keys := range cands {
		out[tenant] = make(map[string]bool, len(keys))
		if len(keys) > rounds {
			rounds = len(keys)
		}
	}
	for i := 0; i < rounds; i++ {
		edited := map[string]ThresholdConfig{} // file → its config with this round's lines deleted
		asked := map[string]string{}           // tenant → this round's key
		for tenant, keys := range cands {
			if i >= len(keys) {
				continue
			}
			name := files[tenant]
			p, ok := edited[name]
			if !ok {
				if p, ok = partialOf(name); !ok {
					continue
				}
				p.Tenants = shallowTenants(p.Tenants)
			}
			if _, has := p.Tenants[tenant][keys[i]]; !has {
				continue
			}
			m := make(map[string]ScheduledValue, len(p.Tenants[tenant]))
			for k, v := range p.Tenants[tenant] {
				if k != keys[i] {
					m[k] = v
				}
			}
			p.Tenants[tenant] = m
			edited[name] = p
			asked[tenant] = keys[i]
		}
		if len(asked) == 0 {
			continue
		}
		cf, err := buildWithPartials(s.scan, in, edited)
		if err != nil {
			return nil, fmt.Errorf("counterfactual build: %w", err)
		}
		for tenant, key := range asked {
			out[tenant][key] = sameRowsAllDay(base, &cf.Config, tenant, now)
		}
	}
	return out, nil
}

// buildWithPartials is BuildFlatConfig over scan with the files of edited
// decoded as edited says. A TreeScan holds a sync.Once and is not copied, so
// its Partials are swapped for the build and put back: the scan is the
// scope's own (ScopedTenants), read by nothing else meanwhile.
func buildWithPartials(scan *TreeScan, in FlatBuildInput, edited map[string]ThresholdConfig) (FlatBuild, error) {
	orig := scan.Partials
	merged := make(map[string]ThresholdConfig, len(orig)+len(edited))
	for k, v := range orig {
		merged[k] = v
	}
	for k, v := range edited {
		merged[k] = v
	}
	scan.Partials = merged
	defer func() { scan.Partials = orig }()
	return BuildFlatConfig(scan, in)
}

func shallowTenants(in map[string]map[string]ScheduledValue) map[string]map[string]ScheduledValue {
	out := make(map[string]map[string]ScheduledValue, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

// sameRowsAllDay reports whether tenant's rows from a and from b are the same
// as written at now and at the start of every segment of the day either
// side's schedules cut (addScheduleCuts).
func sameRowsAllDay(a, b *ThresholdConfig, tenant string, now time.Time) bool {
	oneA, oneB := oneTenant(a, tenant), oneTenant(b, tenant)
	if tenantRows(oneA, now) != tenantRows(oneB, now) {
		return false
	}
	seen := addScheduleCuts(addScheduleCuts(nil, a.Tenants[tenant]), b.Tenants[tenant])
	for _, m := range sortedCuts(seen) {
		if tenantRows(oneA.AtMinuteOfDay(m), now) != tenantRows(oneB.AtMinuteOfDay(m), now) {
			return false
		}
	}
	return true
}

func oneTenant(c *ThresholdConfig, tenant string) *ThresholdConfig {
	one := *c
	one.Tenants = map[string]map[string]ScheduledValue{}
	if m, ok := c.Tenants[tenant]; ok {
		one.Tenants[tenant] = m
	}
	return &one
}

// tenantRows is c's rows (c holds one tenant), each printed whole (labels,
// value, severity, legacy twin), sorted and joined.
func tenantRows(c *ThresholdConfig, now time.Time) string {
	rows, _ := c.resolveAtWithStats(now, nil, nil, nil)
	ss := make([]string, len(rows))
	for i, r := range rows {
		ss[i] = fmt.Sprintf("%v", r)
	}
	sort.Strings(ss)
	return fmt.Sprint(ss)
}

// RedundantThresholds is, for every tenant of cands and every key of its
// list (threshold keys as the tenant's own file spells them), whether the
// line is redundant (#4330): deleting it leaves what the exporter serves for
// the tenant the same all day (ServedUnchangedWithout), and no other finding
// owns the line — the exporter's own record of the tenant
// (TenantValuesNotServedAndDisabled, the one call both tables come from)
// neither refuses its value nor serves nothing of it, nor lists it as a
// `disable` nothing reads (notServedOwnedElsewhere).
func (s *ScopedTenants) RedundantThresholds(cands map[string][]string, now time.Time) (map[string]map[string]bool, error) {
	unchanged, err := s.ServedUnchangedWithout(cands, now)
	if err != nil {
		return nil, err
	}
	for tenant, keys := range unchanged {
		table, disabled := s.built.Config.TenantValuesNotServedAndDisabled(tenant, now)
		noop := map[string]bool{}
		for _, k := range disabled {
			noop[k] = true
		}
		for key, same := range keys {
			if !same {
				continue
			}
			canon, _ := CanonicalKeyFor(key)
			for _, k := range []string{key, canon} {
				if reason, listed := table[k]; (listed && notServedOwnedElsewhere(k, reason)) || noop[k] {
					keys[key] = false
				}
			}
		}
	}
	return unchanged, nil
}

// notServedOwnedElsewhere reports whether a tenant key the exporter records
// as not served for reason is another finding's to report: its value is
// refused (ValueNotServedAsWritten — value_not_served says what to write), or
// nothing of the key is served at any minute (notServedReasonTable's
// keyServed false: an unread key the shared key verdict reports, a value no
// layer delivers). A key served in some part of the day, or switched off by
// its base (base_disabled), is not: it may be redundant.
func notServedOwnedElsewhere(key, reason string) bool {
	return ValueNotServedAsWritten(key, reason) || !notServedKeyServed(reason)
}
