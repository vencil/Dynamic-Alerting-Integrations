package main

// served_schedules.go — served-values' `schedules`: what /metrics serves for
// each threshold key over the whole UTC day, not only at `--at` (#2115 (c):
// a policy over a scheduled threshold must hold in every part of the day).
//
// ⛔ NO SCHEDULE READING OF ITS OWN, as for the rest of served-values. The
// day is cut where config.ThresholdConfig.ScheduleCuts says some value may
// change (the windows parsed by the parser the resolver uses), and each piece
// is read by the exporter's resolver over config.AtMinuteOfDay — the tree
// with every schedule fixed to that minute — at `--at` itself, then put
// through the exporter's user_threshold builder (thresholdmetric.Emit) and a
// Gather, as /metrics would. Which value a window means, `disable`,
// `N:critical`, a value the resolver falls back on, an expired override,
// the cardinality cut: all of it is the resolver's verdict. The piece that
// holds `--at` must read exactly what Values reads; addSchedules refuses
// (exit 2) when it does not, so the two readings cannot drift apart
// unnoticed.

import (
	"fmt"
	"sort"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/internal/thresholdmetric"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// addSchedules fills the Schedules of every tenant of tenants (servedValues'
// reading of cfg at `at`). expiries is the collector's time-boxed threshold
// reading of the same scrape (scrape.Reserved.ThresholdExpiries).
//
// The pieces other than the one holding `at` are resolved silently: their
// WARN lines are the ones the reading at `at` already wrote.
//
// A piece of the day in which the exporter's /metrics cannot be gathered
// (its scrape fails as a whole, HTTP 500) does not fail the run: every key
// of every tenant carries that piece as a segment with Error set. At `at`
// itself servedValues' Gather is that piece's: with --schedules the run
// already failed (exit 2) before reaching here; with --schedules-only
// (#3877) it is called with instantGathered false and tenants holding
// nothing read at `at`, and the piece holding `at` is such a segment too.
// Either way the two readings must agree on whether that piece gathers.
//
// #3808: each tenant is read alone, at its own cuts only, and its readings
// are let go once its schedules are written — a tenant's user_threshold rows
// depend on its own map and the config-wide settings, never on another
// tenant's (row_count.go, not_served.go). The one effect that crosses
// tenants is the Gather: two rows of one tenant giving one series fail the
// scrape for all. So a first pass finds, tenant by tenant, the parts of the
// day its rows cannot be gathered; their bounds cut every tenant's day, and
// each such part carries the error the whole tree's Gather gives there (read
// over the tenants failing in it, which are the ones it names). The document
// is, byte for byte, the one reading the whole tree at every cut of the tree
// gave (served_schedules_differential_test.go).
func addSchedules(cfg *config.ThresholdConfig, at time.Time, tenants map[string]servedTenantValues,
	expiries []config.ResolvedThresholdExpiry, name keyNamer, instantGathered bool,
) error {
	utc := at.UTC()
	atMinute := utc.Hour()*60 + utc.Minute()

	ungatherable, err := ungatherableParts(cfg, at, name)
	if err != nil {
		return err
	}
	if gerr := ungatherable.at(atMinute); (gerr != "") == instantGathered {
		cuts := cfg.ScheduleCuts()
		ends := cutEnds(cuts)
		i := sort.SearchInts(cuts, atMinute+1) - 1
		if instantGathered {
			return fmt.Errorf("from %s to %s UTC: internal: the schedule reading at --at cannot be gathered, the served values could: %s",
				hhmm(cuts[i]), hhmm(ends[i]), gerr)
		}
		return fmt.Errorf("from %s to %s UTC: internal: the served values at --at cannot be gathered, the schedule reading at --at could",
			hhmm(cuts[i]), hhmm(ends[i]))
	}

	expiryOf := map[string]map[string]config.ResolvedThresholdExpiry{}
	for _, te := range expiries {
		canon, _ := config.CanonicalKeyFor(te.MetricKey)
		if expiryOf[te.Tenant] == nil {
			expiryOf[te.Tenant] = map[string]config.ResolvedThresholdExpiry{}
		}
		if prev, dup := expiryOf[te.Tenant][canon]; dup {
			return fmt.Errorf("internal: tenant %s: keys %q and %q both time-box %q", te.Tenant, prev.MetricKey, te.MetricKey, canon)
		}
		expiryOf[te.Tenant][canon] = te
	}

	names := make([]string, 0, len(tenants))
	for tenant := range tenants {
		names = append(names, tenant)
	}
	sort.Strings(names)
	for _, tenant := range names {
		tv := tenants[tenant]
		one := onlyTenants(cfg, []string{tenant})
		own := one.ScheduleCuts()
		seen := map[int]bool{}
		for _, m := range own {
			seen[m] = true
		}
		for _, m := range ungatherable.bounds {
			if m < config.MinutesPerDay {
				seen[m] = true
			}
		}
		cuts := make([]int, 0, len(seen))
		for m := range seen {
			cuts = append(cuts, m)
		}
		sort.Ints(cuts)
		ends := cutEnds(cuts)
		ownEnds := cutEnds(own)

		// The tenant's reading in each piece: the one at the start of its own
		// piece holding it (its rows change at its own cuts only), none where
		// the tree cannot be gathered.
		passes := make([]map[string]map[string]reading, len(cuts))
		gatherErrs := make([]string, len(cuts))
		readAt, read := -1, map[string]map[string]reading(nil)
		for i, m := range cuts {
			if gatherErrs[i] = ungatherable.at(m); gatherErrs[i] != "" {
				continue
			}
			j := sort.SearchInts(own, m+1) - 1
			if j != readAt {
				r, gerr, err := readingsAt(one.AtMinuteOfDay(own[j]), at, name)
				if err == nil && gerr != "" {
					err = fmt.Errorf("internal: tenant %s: its rows cannot be gathered, yet the first pass found they could: %s", tenant, gerr)
				}
				if err != nil {
					return fmt.Errorf("from %s to %s UTC: %w", hhmm(own[j]), hhmm(ownEnds[j]), err)
				}
				readAt, read = j, r
			}
			passes[i] = map[string]map[string]reading{tenant: read[tenant]}
			if m <= atMinute && atMinute < ends[i] {
				if err := sameAsValues(tenant, tv, read[tenant]); err != nil {
					return err
				}
			}
		}

		schedules, err := tenantSchedules(cfg, at, tenant, cuts, ends, passes, gatherErrs, expiryOf[tenant], name)
		if err != nil {
			return err
		}
		tv.Schedules = &schedules
		tenants[tenant] = tv
	}
	return nil
}

// tenantSchedules is the tenant's schedules over the pieces [cuts[i],
// ends[i]) of the day, read as passes (the tenant's entry only is used) with
// gatherErrs; expiries is the tenant's time-boxed keys by served name.
func tenantSchedules(cfg *config.ThresholdConfig, at time.Time, tenant string, cuts, ends []int,
	passes []map[string]map[string]reading, gatherErrs []string,
	expiries map[string]config.ResolvedThresholdExpiry, name keyNamer,
) (map[string]servedSchedule, error) {
	schedules := map[string]servedSchedule{}
	keys := map[string]bool{}
	for _, p := range passes {
		for k := range p[tenant] {
			keys[k] = true
		}
	}
	for k := range expiries {
		keys[k] = true
	}
	// #3140: why a segment serves no row, read off the key's written
	// value at the segment's start (nullReason). k is the served,
	// canonical name, so a retired spelling is found under it; when the
	// tenant writes both, the canonical one is read (keys' shadowed).
	writtenOf := map[string]string{}
	for wk := range cfg.Tenants[tenant] {
		canon, isAlias := config.CanonicalKeyFor(wk)
		if _, canonWritten := cfg.Tenants[tenant][canon]; isAlias && canonWritten {
			continue
		}
		writtenOf[name(tenant, canon)] = wk
	}
	recorded := recordedReasons(cfg, tenant, at)
	view := cfg.FamilyView(tenant)
	for k := range keys {
		why := func(minute int) string {
			return nullReason(cfg, tenant, k, writtenOf[k], at, minute, recorded, view)
		}
		sch := servedSchedule{Segments: daySegments(cuts, ends, passes, gatherErrs, tenant, k, why)}
		if te, ok := expiries[k]; ok {
			sv, written := cfg.Tenants[tenant][te.MetricKey]
			if !written || sv.Expiry == nil {
				return nil, fmt.Errorf("internal: tenant %s: the expiry of %q has no `expires:` in the merged config", tenant, te.MetricKey)
			}
			expired := te.Expired
			sch.Expires = sv.Expiry.Expires
			sch.Expired = &expired
		}
		schedules[k] = sch
	}
	return schedules, nil
}

// cutEnds is, for each piece starting at cuts[i], where it ends: the next
// cut, or the end of the day.
func cutEnds(cuts []int) []int {
	ends := make([]int, len(cuts))
	for i := range cuts {
		ends[i] = config.MinutesPerDay
		if i+1 < len(cuts) {
			ends[i] = cuts[i+1]
		}
	}
	return ends
}

// ungatherableDay is where in the day the whole tree's /metrics cannot be
// gathered: the pieces [bounds[i], bounds[i+1]) with errs[i] set, errs[i]
// the error the whole tree's Gather gives there ("" where it gathers). The
// last bound ends the last such piece; no bounds, no such piece.
type ungatherableDay struct {
	bounds []int
	errs   []string
}

// at is the Gather error of the piece holding minute, "" when it gathers.
func (u ungatherableDay) at(minute int) string {
	i := sort.SearchInts(u.bounds, minute+1) - 1
	if i < 0 || i >= len(u.errs) {
		return ""
	}
	return u.errs[i]
}

// ungatherableParts reads every tenant of cfg alone at its own cuts, keeping
// only where its rows cannot be gathered, and reads the tenants failing in
// each part so found together, for the error text the whole tree's Gather
// gives there: it names every series two keys give, of every tenant.
func ungatherableParts(cfg *config.ThresholdConfig, at time.Time, name keyNamer) (ungatherableDay, error) {
	type failing struct {
		from, to int
		tenant   string
	}
	var fails []failing
	names := make([]string, 0, len(cfg.Tenants))
	for tenant := range cfg.Tenants {
		names = append(names, tenant)
	}
	sort.Strings(names)
	for _, tenant := range names {
		one := onlyTenants(cfg, []string{tenant})
		cuts := one.ScheduleCuts()
		ends := cutEnds(cuts)
		for i, m := range cuts {
			_, gerr, err := readingsAt(one.AtMinuteOfDay(m), at, name)
			if err != nil {
				return ungatherableDay{}, fmt.Errorf("from %s to %s UTC: %w", hhmm(m), hhmm(ends[i]), err)
			}
			if gerr != "" {
				fails = append(fails, failing{m, ends[i], tenant})
			}
		}
	}
	if len(fails) == 0 {
		return ungatherableDay{}, nil
	}
	seen := map[int]bool{}
	for _, f := range fails {
		seen[f.from], seen[f.to] = true, true
	}
	var u ungatherableDay
	for m := range seen {
		u.bounds = append(u.bounds, m)
	}
	sort.Ints(u.bounds)
	u.errs = make([]string, len(u.bounds)-1)
	for i := range u.errs {
		m := u.bounds[i]
		var in []string
		for _, f := range fails {
			if f.from <= m && m < f.to {
				in = append(in, f.tenant)
			}
		}
		if len(in) == 0 {
			continue
		}
		_, gerr, err := readingsAt(onlyTenants(cfg, in).AtMinuteOfDay(m), at, name)
		if err == nil && gerr == "" {
			err = fmt.Errorf("internal: tenants %v cannot be gathered one by one, yet can together", in)
		}
		if err != nil {
			return ungatherableDay{}, fmt.Errorf("from %s to %s UTC: %w", hhmm(m), hhmm(u.bounds[i+1]), err)
		}
		u.errs[i] = gerr
	}
	return u, nil
}

// daySegments is key k's day over the pieces [cuts[i], ends[i]), adjacent
// pieces with the same reading (or the same Gather error) merged. A piece
// whose gatherErrs entry is set is a segment carrying that error.
func daySegments(cuts, ends []int, passes []map[string]map[string]reading, gatherErrs []string, tenant, k string,
	why func(minute int) string,
) []scheduleSegment {
	var segs []scheduleSegment
	var last reading
	lastServed, lastErr, lastReason := false, "", ""
	for i, m := range cuts {
		gerr := gatherErrs[i]
		r, served := passes[i][tenant][k]
		reason := ""
		if gerr == "" && !served {
			reason = why(m)
		}
		if n := len(segs); n > 0 && gerr == lastErr && served == lastServed && reason == lastReason &&
			(!served || (sameFloat(r.value, last.value) && r.severity == last.severity && sameSeries(r.series, last.series))) {
			segs[n-1].To = hhmm(ends[i])
			continue
		}
		seg := scheduleSegment{From: hhmm(m), To: hhmm(ends[i])}
		switch {
		case gerr != "":
			seg.Error = gerr
		case served:
			seg.Value = jsonFloat(r.value)
			sev := r.severity
			seg.Severity = &sev
			seg.Series = r.series
		default:
			seg.Value, seg.Reason = jsonNull, reason
		}
		segs = append(segs, seg)
		last, lastServed, lastErr, lastReason = r, served, gerr, reason
	}
	return segs
}

// sameAsValues checks that the piece of the day holding `at` reads, for the
// tenant, exactly the threshold keys, values, severities and series Values
// reads.
func sameAsValues(tenant string, tv servedTenantValues, piece map[string]reading) error {
	for k, sev := range tv.Severities {
		r, ok := piece[k]
		if !ok || r.severity != sev || jsonFloat(r.value) != tv.Values[k] || !sameSeries(r.series, tv.Series[k]) {
			return fmt.Errorf("internal: tenant %s: key %q: the schedule reading at --at differs from the served value", tenant, k)
		}
	}
	for k := range piece {
		if _, ok := tv.Severities[k]; !ok {
			return fmt.Errorf("internal: tenant %s: key %q: the schedule reading at --at serves a key the served values do not", tenant, k)
		}
	}
	return nil
}

// readingsAt is what /metrics serves of the user_threshold rows of cfg at
// `at`, per tenant and threshold key: the exporter's resolver (silent), the
// exporter's user_threshold builder, and a Gather over them. A row the
// builder drops is not served. gatherErr, when not empty, says why the
// Gather fails — /metrics then serves nothing, so readings is nil; err is
// any other failure.
func readingsAt(cfg *config.ThresholdConfig, at time.Time, name keyNamer) (readings map[string]map[string]reading, gatherErr string, err error) {
	keyed, _, err := cfg.ResolveAtWithKeysSilent(at)
	if err != nil {
		return nil, "", err
	}
	rows := make([]config.ResolvedThreshold, len(keyed))
	for i, k := range keyed {
		rows[i] = k.ResolvedThreshold
	}
	results := make([]emitResult, len(rows))
	reg := prometheus.NewRegistry()
	reg.MustRegister(thresholdRows{rows: rows, report: func(i int, m prometheus.Metric, err error) {
		results[i] = emitResult{reported: true, metric: m, err: err}
	}})
	gathered, gerr := gatherSeries(reg)
	if gerr != nil {
		msg := "the exporter's /metrics cannot be gathered, so its scrape fails " +
			"as a whole (HTTP 500) and nothing is served"
		if keys := sameSeriesKeys(keyed, results, name); keys != "" {
			return nil, msg + keys, nil // as notGatherableError.Error
		}
		return nil, msg + ": " + gerr.Error(), nil
	}
	served, _, err := groupRows(keyed, results)
	if err != nil {
		return nil, "", err
	}
	series, err := keySeries(keyed, results, gathered)
	if err != nil {
		return nil, "", err
	}
	out := make(map[string]map[string]reading, len(served))
	for tenant, byKey := range served {
		for name, rs := range byKey {
			if name == customAlertsKey {
				continue
			}
			r, err := keyReading(tenant, name, rs)
			if err != nil {
				return nil, "", err
			}
			r.series = series[tenant][name]
			if len(r.series) != len(rs) {
				return nil, "", fmt.Errorf("internal: tenant %s: key %q owns %d rows but %d series", tenant, name, len(rs), len(r.series))
			}
			if err := checkKeySeries(tenant, name, r.series); err != nil {
				return nil, "", err
			}
			if out[tenant] == nil {
				out[tenant] = map[string]reading{}
			}
			out[tenant][name] = r
		}
	}
	return out, "", nil
}

// thresholdRows is a collector of the user_threshold rows alone, built by
// the exporter's own builder.
type thresholdRows struct {
	rows   []config.ResolvedThreshold
	report func(i int, m prometheus.Metric, err error)
}

// Describe sends nothing: unchecked, as the exporter's collector.
func (thresholdRows) Describe(chan<- *prometheus.Desc) {}

func (c thresholdRows) Collect(ch chan<- prometheus.Metric) {
	thresholdmetric.Emit(ch, c.rows, c.report)
}

// hhmm renders a minute of the day (0..1440) as "HH:MM"; 1440 is "24:00".
func hhmm(m int) string {
	return fmt.Sprintf("%02d:%02d", m/60, m%60)
}

// nullReason is why threshold key served (the served name) — written in
// cfg's spelling, "" for a key the tenant's map does not write — serves no
// row from minute of the UTC day of at (#3140): keyReasonDisabled for a
// disable written for that part of the day, keyReasonValueNonFinite /
// keyReasonValueUnparsed for a value the resolver refuses (and recorded
// refusing), keyReasonBaseDisabled for a `<base>_critical` / `<base>{…}` key
// — written or a root `defaults:` one — whose base the tenant switches off
// for that part of the day (#3046, FamilyView.SwitchedOffWithBase: the
// expiry read at at, as the resolver reads each part of the day), else ""
// (nothing to serve: a lapsed or absent value).
func nullReason(cfg *config.ThresholdConfig, tenant, served, written string, at time.Time, minute int,
	recorded func(canon string) string, view config.FamilyView,
) string {
	utc := at.UTC()
	t := time.Date(utc.Year(), utc.Month(), utc.Day(), 0, minute, 0, 0, time.UTC)
	sv, ok := cfg.Tenants[tenant][written]
	if !ok {
		if view.SwitchedOffWithBase(served, at, t) {
			return keyReasonBaseDisabled // a root `defaults:` dimensional default (#3046)
		}
		return ""
	}
	canon, _ := config.CanonicalKeyFor(written)
	if config.DisabledAt(sv, t) {
		return keyReasonDisabled
	}
	if r := config.RefusedValueAt(canon, sv, t); r != "" && isRecordedRefusal(recorded(canon)) {
		return refusedReason(r)
	}
	if view.SwitchedOffWithBase(written, at, t) {
		return keyReasonBaseDisabled // #3046
	}
	return ""
}
