package config

// expiry.go — a threshold's `expires:` is decided where the tree is merged
// (#4263, #4254).
//
// ONE RULE, ONE ORACLE. At an instant t, the tenant's config is the merge of
// the tree with every value whose `expires:` has passed at t left out: an
// expired value is as if it were not written, so the tenant gets what the
// merge gives it without that value — the next layer down (a root platform
// file's `tenants:` entry, the tenant's profile, a subtree `_defaults.yaml`),
// else the root `defaults:`. The merge itself is the only definition of
// "beneath": every merge path runs unchanged on filtered inputs, and every
// reader sees an ordinary config.
//
// EXCEPT WHERE THAT WOULD GO SILENT (PREVENT #656). When, with the value
// left out, nothing serves the key (servable: no other layer gives it a
// number, a schedule or a `disable`, and the root gives it none either), the
// value is put back and served past its `expires:`. A value a reader refuses
// (`abc`) is no value here, as a refused root value is none.
//
// THE OTHER TWO READERS. Whether a value's `expires:` can take effect at all
// (honoured) is asked of the merge with every time-boxed value left out: what
// is still served there is served when this one lapses. The write check
// (ValidateTenantKeys) refuses an `expires:` that cannot take effect, and the
// expiry event (ResolveThresholdExpiriesAt) names the ones that do.
//
// A CONFIG IS AN ANSWER FOR ITS INSTANT. It holds until the next `expires:`
// along the tree (ValidUntil); a reader resolving past it builds again — the
// exporter does on the first scrape past it.

import (
	"time"
)

// expiryState is what a build decided about `expires:` at its instant.
type expiryState struct {
	at         time.Time
	validFrom  time.Time // the last `expires:` at or before at; zero: none
	validUntil time.Time // zero: no `expires:` of the tree falls after at
	// honoured: tenant → canonical key → whether the `expires:` of the value
	// the tenant gets for it can take effect.
	honoured map[string]map[string]bool
	events   []ResolvedThresholdExpiry
	// written is the tenants' merged maps of the whole tree, before
	// `expires:` was decided (DescribedTenants); nil when nothing was
	// decided away.
	written map[string]map[string]ScheduledValue
}

// ExpiryAt is the instant the config's `expires:` were decided at, and
// whether they were (a config built from a tree; one assembled by hand has
// none, and reads `expires:` at resolve time against the root alone). A tree
// with no time-boxed value is decided with no instant: its answer is the
// same at every one.
func (c *ThresholdConfig) ExpiryAt() (time.Time, bool) {
	if c == nil || c.expiry == nil {
		return time.Time{}, false
	}
	return c.expiry.at, true
}

// ValidAt reports whether the config answers for the instant t: t lies
// between the last `expires:` the build saw pass and the next one (a config
// with no decision, or with nothing time-boxed, answers for every instant).
// A reader resolving at an instant it does not answer for builds the tree
// again — the exporter does when its clock moves past either edge, set back
// included.
func (c *ThresholdConfig) ValidAt(t time.Time) bool {
	if c == nil || c.expiry == nil {
		return true
	}
	st := c.expiry
	if !st.validFrom.IsZero() && t.Before(st.validFrom) {
		return false
	}
	return st.validUntil.IsZero() || t.Before(st.validUntil)
}

// DescribedTenants is, per tenant, the lines a reader that describes the
// tenant's own lines reads (da-guard's disable effects, key verdicts, the
// served keys): every value the instant serves (Tenants), and beside it
// every value the merge left out because its `expires:` passed — the line
// is still written (#4263). Where both name one threshold, the served one is
// described. Tenants itself when the build decided nothing away.
func (c *ThresholdConfig) DescribedTenants() map[string]map[string]ScheduledValue {
	if c == nil {
		return nil
	}
	if c.expiry == nil || c.expiry.written == nil {
		return c.Tenants
	}
	out := make(map[string]map[string]ScheduledValue, len(c.expiry.written))
	for tenant, written := range c.expiry.written {
		m := make(map[string]ScheduledValue, len(written))
		for k, v := range written {
			m[k] = v
		}
		served := c.Tenants[tenant]
		var buf [2]string
		for k, v := range served {
			for _, sp := range otherSpellings(k, &buf) {
				if _, servedToo := served[sp]; !servedToo {
					delete(m, sp) // the written spelling the served one replaces
				}
			}
			m[k] = v
		}
		out[tenant] = m
	}
	return out
}

// ValidUntil is the first `expires:` of the tree after the instant the
// config was built for, and whether there is one: from that instant on the
// tree must be built again to be read. A config with no decision (ExpiryAt
// false) or none to come has none.
func (c *ThresholdConfig) ValidUntil() (time.Time, bool) {
	if c == nil || c.expiry == nil || c.expiry.validUntil.IsZero() {
		return time.Time{}, false
	}
	return c.expiry.validUntil, true
}

// expiryInstant is sv's `expires:` instant; ok false when it has none or one
// that does not parse (fail-open: never expires; ValidateTenantKeys reports
// it).
func expiryInstant(sv ScheduledValue) (time.Time, bool) {
	if sv.Expiry == nil || sv.Expiry.Expires == "" {
		return time.Time{}, false
	}
	t, err := time.Parse(time.RFC3339, sv.Expiry.Expires)
	if err != nil {
		return time.Time{}, false
	}
	return t, true
}

// expiryKeep is a filter over the merge's inputs: false leaves the value out.
type expiryKeep func(sv ScheduledValue) bool

// expiryScan is the filter of a build at at: it leaves out what has expired
// at at, and records whether the inputs hold any time-boxed value and the
// first `expires:` after at.
type expiryScan struct {
	at   time.Time
	any  bool
	last time.Time // the last `expires:` at or before at
	next time.Time // the first `expires:` after at
}

func (s *expiryScan) keep(sv ScheduledValue) bool {
	t, ok := expiryInstant(sv)
	if !ok {
		return true
	}
	s.any = true
	if !t.After(s.at) {
		if t.After(s.last) {
			s.last = t
		}
		return false
	}
	if s.next.IsZero() || t.Before(s.next) {
		s.next = t
	}
	return true
}

// keepUntimed leaves out every time-boxed value: the merge with it is what
// a value's lapse would leave at the latest (honoured).
func keepUntimed(sv ScheduledValue) bool {
	_, ok := expiryInstant(sv)
	return !ok
}

// filterPartials is configs with every `tenants:` and `profiles:` value keep
// refuses left out; a partial with none is shared, not copied.
func filterPartials(configs map[string]ThresholdConfig, keep expiryKeep) map[string]ThresholdConfig {
	out := make(map[string]ThresholdConfig, len(configs))
	for name, p := range configs {
		tenants, tCopied := filterBodies(p.Tenants, keep)
		profiles, pCopied := filterBodies(p.Profiles, keep)
		if tCopied || pCopied {
			p.Tenants, p.Profiles = tenants, profiles
		}
		out[name] = p
	}
	return out
}

// filterBodies is bodies without the values keep refuses, and whether that
// left anything out (bodies itself, unchanged, when not).
func filterBodies(bodies map[string]map[string]ScheduledValue, keep expiryKeep) (map[string]map[string]ScheduledValue, bool) {
	var out map[string]map[string]ScheduledValue
	for name, body := range bodies {
		var kept map[string]ScheduledValue
		for k, sv := range body {
			if keep(sv) {
				continue
			}
			if kept == nil {
				kept = make(map[string]ScheduledValue, len(body))
				for k2, v2 := range body {
					kept[k2] = v2
				}
			}
			delete(kept, k)
		}
		if kept == nil {
			continue
		}
		if out == nil {
			out = make(map[string]map[string]ScheduledValue, len(bodies))
			for n2, b2 := range bodies {
				out[n2] = b2
			}
		}
		out[name] = kept
	}
	if out == nil {
		return bodies, false
	}
	return out, true
}

// filterLevels is a subtree chain's parsed levels (ParseDefaultsFiles)
// without the `defaults:` values keep refuses, read as the overlay reads them
// (scheduledValueFromRaw); a level with none is shared.
func filterLevels(parsed map[string]map[string]any, keep expiryKeep) map[string]map[string]any {
	var out map[string]map[string]any
	for path, level := range parsed {
		var kept map[string]any
		for k, raw := range level {
			m, isMap := raw.(map[string]any)
			if !isMap {
				continue // a scalar carries no `expires:`
			}
			if _, has := m["expires"]; !has {
				continue
			}
			sv, ok := scheduledValueFromRaw(raw)
			if !ok || keep(sv) {
				continue
			}
			if kept == nil {
				kept = make(map[string]any, len(level))
				for k2, v2 := range level {
					kept[k2] = v2
				}
			}
			delete(kept, k)
		}
		if kept == nil {
			continue
		}
		if out == nil {
			out = make(map[string]map[string]any, len(parsed))
			for p2, l2 := range parsed {
				out[p2] = l2
			}
		}
		out[path] = kept
	}
	if out == nil {
		return parsed
	}
	return out
}

// servable reports whether view serves the tenant something for key (any
// #1231 spelling): the tenant's merged map holds a value a reader takes — a
// number, a schedule or a `disable` (isThresholdShaped) — or, with none, the
// root gives the key a value a resolver serves (lapsesToRoot). The silence
// test of PREVENT #656.
func servable(view *ThresholdConfig, tenant, key string, canonDefaults map[string]float64) bool {
	canon, _ := canonicalKeyFor(key)
	if held, ok := heldAcrossSpellings(view.Tenants[tenant], canon); ok && isThresholdShaped(view.Tenants[tenant][held]) {
		return true
	}
	return lapsesToRoot(canon, canonDefaults)
}

// lapsesToRoot reports whether, with no value of the tenant's for key
// (canonical), the root `defaults:` serves it one. For a base key that is
// expiryHonoured (a refused root value of a key with a platform value falls
// to the built-in one, #3832). A `<base>_critical` or dimensional key has no
// such layer: only a finite root number is one (#4173, #2031).
func lapsesToRoot(key string, defaults map[string]float64) bool {
	if CriticalRowKey(key) || isDimensionalKey(key) {
		v, valued := defaults[key]
		return valued && finiteThreshold(v)
	}
	return expiryHonoured(key, defaults)
}

// heldAcrossSpellings is the key under which m holds key's threshold — key
// itself, else another #1231 spelling of it — and whether it does.
func heldAcrossSpellings(m map[string]ScheduledValue, key string) (string, bool) {
	if _, ok := m[key]; ok {
		return key, true
	}
	var buf [2]string
	for _, s := range otherSpellings(key, &buf) {
		if _, ok := m[s]; ok {
			return s, true
		}
	}
	return "", false
}

// decideExpiry finishes now — the merge at at with what expired left out:
// every expired value whose absence leaves its key unserved is put back, and
// the build's decisions (expiryState) are attached. full is the merge of the
// whole tree, untimed the merge with every time-boxed value left out, next
// the first `expires:` after at (zero: none).
//
// The events are the expired values that gave way — the one each tenant got
// in the whole tree — and the time-boxed values now serves that can give way.
// ⚠️ Of several expired values stacked over one key, only the one the tenant
// got names its expiry: the ones under it never served.
func decideExpiry(at time.Time, full, now, untimed *ThresholdConfig, last, next time.Time) {
	st := &expiryState{at: at, validFrom: last, validUntil: next, honoured: map[string]map[string]bool{},
		written: full.Tenants}
	nowDefaults := canonicalizeDefaults(now.Defaults)
	untimedDefaults := canonicalizeDefaults(untimed.Defaults)
	for tenant, overrides := range full.Tenants {
		eachTimed(overrides, func(key, canon string, sv ScheduledValue, t time.Time) {
			if t.After(at) {
				return // not expired: judged on now, below
			}
			if !servable(now, tenant, canon, nowDefaults) {
				putBack(now, tenant, key, sv)
				return // judged on now, below: honoured false
			}
			if st.honoured[tenant] == nil {
				st.honoured[tenant] = map[string]bool{}
			}
			st.honoured[tenant][canon] = true // it gave way (a write check reads the value as written)
			st.events = append(st.events, ResolvedThresholdExpiry{
				Tenant: tenant, MetricKey: key, Expires: t, ExpiresText: sv.Expiry.Expires, Reason: sv.Expiry.Reason, Expired: true,
			})
		})
	}
	for tenant, overrides := range now.Tenants {
		eachTimed(overrides, func(key, canon string, sv ScheduledValue, t time.Time) {
			honoured := servable(untimed, tenant, canon, untimedDefaults)
			if st.honoured[tenant] == nil {
				st.honoured[tenant] = map[string]bool{}
			}
			st.honoured[tenant][canon] = honoured
			if honoured && t.After(at) {
				st.events = append(st.events, ResolvedThresholdExpiry{
					Tenant: tenant, MetricKey: key, Expires: t, ExpiresText: sv.Expiry.Expires, Reason: sv.Expiry.Reason,
				})
			}
		})
	}
	now.expiry = st
}

// eachTimed calls f for every threshold value of one tenant's merged map
// with a parsed `expires:` — not a `_` key, and not a retired spelling the
// map also writes canonically (it never governed the value).
func eachTimed(overrides map[string]ScheduledValue, f func(key, canon string, sv ScheduledValue, t time.Time)) {
	for key, sv := range overrides {
		t, ok := expiryInstant(sv)
		if !ok {
			continue
		}
		canon, isAlias := canonicalKeyFor(key)
		if isAlias {
			if _, canonToo := overrides[canon]; canonToo {
				continue
			}
		}
		if baseRowsSkipKey(canon) {
			continue
		}
		f(key, canon, sv, t)
	}
}

// putBack writes sv, the tenant's value of key in the whole tree, into its
// merged map of now in place of any other spelling of the threshold.
func putBack(now *ThresholdConfig, tenant, key string, sv ScheduledValue) {
	m := now.Tenants[tenant]
	if m == nil {
		m = map[string]ScheduledValue{}
		now.Tenants[tenant] = m
	} else {
		cp := make(map[string]ScheduledValue, len(m)+1)
		for k, v := range m {
			cp[k] = v
		}
		m = cp
		now.Tenants[tenant] = m
	}
	var buf [2]string
	for _, s := range otherSpellings(key, &buf) {
		delete(m, s)
	}
	m[key] = sv
}

// expiryCanLapse reports whether the `expires:` of tenant's value of key
// (canonical) can take effect: the build's decision (honoured) when c was
// built from a tree, else the root alone (expiryHonoured), as before the
// decision moved to the merge.
func (c *ThresholdConfig) expiryCanLapse(tenant, key string, canonDefaults map[string]float64) bool {
	if c.expiry != nil {
		if h, ok := c.expiry.honoured[tenant][key]; ok {
			return h
		}
	}
	return expiryHonoured(key, canonDefaults)
}

// HoldsTimedValue reports whether any `tenants:` or `profiles:` value of
// configs carries a parsed `expires:` — the inputs a merge must decide
// `expires:` over (package main's incremental reload patches only a tree with
// none: a patch keeps the previous merge, which answered for another instant).
func HoldsTimedValue(configs map[string]ThresholdConfig) bool {
	s := expiryScan{at: time.Time{}}
	filterPartials(configs, s.keep)
	return s.any
}

// MergeFlatAt is package main's flat merge (MergePartialConfigs, the
// built-in layer unless withheld, the profiles) answering for at, with the
// tree's `expires:` decided as BuildFlatConfig decides them (#4263). It
// returns the merged config and the built-in keys seeded. The profiles' WARNs
// are not logged here: the caller's ApplyProfiles, a no-op fill over the
// result, logs them once.
func MergeFlatAt(configs map[string]ThresholdConfig, exists map[string]struct{}, withheld bool, at time.Time) (ThresholdConfig, []string) {
	if at.IsZero() {
		at = time.Now()
	}
	run := func(keep expiryKeep) (ThresholdConfig, []string) {
		m := mergePartialConfigs(filterPartials(configs, keep), exists)
		var seeded []string
		if !withheld {
			seeded = seedBuiltinDefaults(m.Defaults)
		}
		m.applyProfiles(discardLogf)
		return m, seeded
	}
	timed := expiryScan{at: at}
	now, seeded := run(timed.keep)
	if !timed.any {
		now.expiry = &expiryState{} // nothing time-boxed
		return now, seeded
	}
	full, _ := run(func(ScheduledValue) bool { return true })
	untimed, _ := run(keepUntimed)
	decideExpiry(at, &full, &now, &untimed, timed.last, timed.next)
	return now, seeded
}

// SingleFileAt is package main's single-file load after the decode (the
// built-in layer under the file's `defaults:`, then the profiles) answering
// for at, with the file's `expires:` decided as BuildFlatConfig decides a
// tree's (#4263). It returns the config and the built-in keys seeded; the
// profiles' WARNs go to logf once.
func SingleFileAt(cfg ThresholdConfig, at time.Time, logf func(format string, args ...any)) (ThresholdConfig, []string) {
	if at.IsZero() {
		at = time.Now()
	}
	run := func(keep expiryKeep, logf func(string, ...any)) (ThresholdConfig, []string) {
		c := cfg
		c.Defaults = make(map[string]float64, len(cfg.Defaults))
		for k, v := range cfg.Defaults {
			c.Defaults[k] = v
		}
		c.Tenants, _ = filterBodies(cfg.Tenants, keep)
		c.Profiles, _ = filterBodies(cfg.Profiles, keep)
		// applyProfiles fills the tenants' maps in place: they are copies.
		tenants := make(map[string]map[string]ScheduledValue, len(c.Tenants))
		for tid, body := range c.Tenants {
			cp := make(map[string]ScheduledValue, len(body))
			for k, v := range body {
				cp[k] = v
			}
			tenants[tid] = cp
		}
		c.Tenants = tenants
		seeded := seedBuiltinDefaults(c.Defaults)
		c.applyProfiles(logf)
		return c, seeded
	}
	timed := expiryScan{at: at}
	now, seeded := run(timed.keep, logf)
	if !timed.any {
		now.expiry = &expiryState{} // nothing time-boxed
		return now, seeded
	}
	full, _ := run(func(ScheduledValue) bool { return true }, discardLogf)
	untimed, _ := run(keepUntimed, discardLogf)
	decideExpiry(at, &full, &now, &untimed, timed.last, timed.next)
	return now, seeded
}

// eachUnparsedExpiry calls f for every threshold value of one tenant's
// merged map whose `expires:` does not parse (expiryInstant fails open on
// it), with the text and the parse error — not a `_` key, nor a retired
// spelling the map also writes canonically.
func eachUnparsedExpiry(overrides map[string]ScheduledValue, f func(key, text string, err error)) {
	for key, sv := range overrides {
		if sv.Expiry == nil || sv.Expiry.Expires == "" {
			continue
		}
		_, err := time.Parse(time.RFC3339, sv.Expiry.Expires)
		if err == nil {
			continue
		}
		canon, isAlias := canonicalKeyFor(key)
		if isAlias {
			if _, canonToo := overrides[canon]; canonToo {
				continue
			}
		}
		if baseRowsSkipKey(canon) {
			continue
		}
		f(key, sv.Expiry.Expires, err)
	}
}

// expiryReadAt is the instant a reader of c reads `expires:` at when it
// resolves at now: the build's instant when the build decided them — past
// its ValidUntil c keeps answering for that instant until the rebuild
// replaces it, instead of lapsing a value to the root the merge never chose
// (#4263) — else now.
func (c *ThresholdConfig) expiryReadAt(now time.Time) time.Time {
	if c != nil && c.expiry != nil && !c.expiry.at.IsZero() {
		return c.expiry.at
	}
	return now
}
