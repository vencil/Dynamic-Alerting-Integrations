package config

// The counterfactual of a tenant line (#4330): does deleting key from the
// tenant's own file change what the exporter serves for that tenant?
//
// ⛔ NO READING OF ITS OWN. The answer is two builds and two resolves by the
// exporter's own code: the tree as written, and the tree with the line
// deleted from the file's decoded config, built by BuildFlatConfig (subtree
// defaults, profiles, platform entries and the root all applied as the
// exporter applies them) and resolved from now on: at every minute of the UTC
// day at which either side's schedules may serve a different value, now and
// at every later instant either build says it must be built again at (its
// ValidUntil: the next `expires:` of the tree, which the merge decides —
// #4263), each side built again for that instant. A predicate
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
// tenant the same from now on (sameRowsAllDay). A key the
// tenant's file does not hold, or a tenant whose file this scope cannot
// give, is reported false (not unchanged): the caller then gives no advice.
func (s *ScopedTenants) ServedUnchangedWithout(cands map[string][]string, now time.Time) (map[string]map[string]bool, error) {
	if s == nil || s.scan == nil || s.built == nil {
		return nil, fmt.Errorf("counterfactual needs the scope's scan and build")
	}
	quiet := log.New(io.Discard, "", 0)
	in := loadDirBuildInput(s.scan, s.scan.AbsRoot, quiet, discardLogf)
	bases := map[time.Time]*ThresholdConfig{} // instant → the tree as written, built for it
	baseAt := func(at time.Time) (*ThresholdConfig, error) {
		if b, ok := bases[at]; ok {
			return b, nil
		}
		inAt := in
		inAt.At = at
		b, err := buildWithPartials(s.scan, inAt, nil)
		if err != nil {
			return nil, err
		}
		bases[at] = &b.Config
		return &b.Config, nil
	}

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
			held, ok := decodedSpelling(p.Tenants[tenant], keys[i])
			if !ok {
				continue
			}
			m := make(map[string]ScheduledValue, len(p.Tenants[tenant]))
			for k, v := range p.Tenants[tenant] {
				if k != held {
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
		for tenant, key := range asked {
			out[tenant][key] = true
		}
		// One pair of builds per instant: now, then each instant either
		// build is valid until — the tree changes there whichever line
		// expires, so every tenant of the round is compared again.
		for at, more := now, true; more; {
			base, err := baseAt(at)
			if err != nil {
				return nil, fmt.Errorf("counterfactual build: %w", err)
			}
			inAt := in
			inAt.At = at
			cf, err := buildWithPartials(s.scan, inAt, edited)
			if err != nil {
				return nil, fmt.Errorf("counterfactual build: %w", err)
			}
			for tenant, key := range asked {
				if out[tenant][key] && !sameRowsAllDay(base, &cf.Config, tenant, at) {
					out[tenant][key] = false
				}
			}
			at, more = nextBuildInstant(at, base, &cf.Config)
		}
	}
	return out, nil
}

// nextBuildInstant is the first instant after at that a or b must be built
// again at (ValidUntil), and whether there is one.
func nextBuildInstant(at time.Time, a, b *ThresholdConfig) (time.Time, bool) {
	var next time.Time
	for _, c := range []*ThresholdConfig{a, b} {
		if t, ok := c.ValidUntil(); ok && t.After(at) && (next.IsZero() || t.Before(next)) {
			next = t
		}
	}
	return next, !next.IsZero()
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

// decodedSpelling is the key of m (a tenant's decoded map) that key, as the
// tenant's file spells it, is decoded to: the key itself, or the one with the
// same canonical dimensional spelling (canonicalDimensionalKey — label order
// and spacing are not the decode's).
func decodedSpelling(m map[string]ScheduledValue, key string) (string, bool) {
	if _, ok := m[key]; ok {
		return key, true
	}
	want, dim := canonicalDimensionalKey(key)
	if !dim {
		return "", false
	}
	for k := range m {
		if got, ok := canonicalDimensionalKey(k); ok && got == want {
			return k, true
		}
	}
	return "", false
}

func shallowTenants(in map[string]map[string]ScheduledValue) map[string]map[string]ScheduledValue {
	out := make(map[string]map[string]ScheduledValue, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

// sameRowsAllDay reports whether tenant's rows from a and from b — two
// builds for the instant at — are the same at at, each read as written and
// at the start of every segment of the day either side's schedules cut
// (addScheduleCuts).
func sameRowsAllDay(a, b *ThresholdConfig, tenant string, at time.Time) bool {
	oneA, oneB := oneTenant(a, tenant), oneTenant(b, tenant)
	if servedRowsOf(oneA, at) != servedRowsOf(oneB, at) {
		return false
	}
	cuts := sortedCuts(addScheduleCuts(addScheduleCuts(nil, a.Tenants[tenant]), b.Tenants[tenant]))
	for _, m := range cuts {
		if servedRowsOf(oneA.AtMinuteOfDay(m), at) != servedRowsOf(oneB.AtMinuteOfDay(m), at) {
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
func servedRowsOf(c *ThresholdConfig, now time.Time) string {
	rows, _ := c.resolveAtWithStats(now, nil, nil, nil)
	ss := make([]string, len(rows))
	for i, r := range rows {
		ss[i] = fmt.Sprintf("%v", r)
	}
	sort.Strings(ss)
	return fmt.Sprint(ss)
}

// ScanRoot is the resolved root the scope's walk read ("" when it has no
// scan): the tree a verdict meant to agree with this scope must judge.
func (s *ScopedTenants) ScanRoot() string {
	if s == nil || s.scan == nil {
		return ""
	}
	return s.scan.AbsRoot
}

// NotServedOwnedElsewhere reports whether a tenant key the exporter records
// as not served for reason is another finding's to report rather than a
// redundancy's (#4330): its value is refused (ValueNotServedAsWritten —
// value_not_served's own selection), or nothing of the key is served at any
// minute (notServedReasonTable's keyServed false — a key nothing reads,
// which the shared key verdict reports as unread). A key served in some part
// of the day, or switched off by its base (base_disabled), is not.
func NotServedOwnedElsewhere(key, reason string) bool {
	return ValueNotServedAsWritten(key, reason) || !notServedKeyServed(reason)
}

// CanonicalDimensionalKey is key in the canonical dimensional spelling the
// decode gives it (label order and spacing are not kept), or key itself when
// it is no dimensional key: two spellings of one tenant key compare equal
// through it (#4330).
func CanonicalDimensionalKey(key string) string {
	canon, _ := canonicalDimensionalKey(key)
	return canon
}
