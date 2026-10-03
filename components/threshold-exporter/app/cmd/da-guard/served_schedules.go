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
// itself the run already failed (exit 2) before reaching here — servedValues'
// Gather is that piece's.
func addSchedules(cfg *config.ThresholdConfig, at time.Time, tenants map[string]servedTenantValues,
	expiries []config.ResolvedThresholdExpiry,
) error {
	cuts := cfg.ScheduleCuts()
	ends := make([]int, len(cuts))
	for i := range cuts {
		ends[i] = config.MinutesPerDay
		if i+1 < len(cuts) {
			ends[i] = cuts[i+1]
		}
	}
	// The piece that holds `at` is the reading Values came from.
	utc := at.UTC()
	cur := sort.SearchInts(cuts, utc.Hour()*60+utc.Minute()+1) - 1

	passes := make([]map[string]map[string]reading, len(cuts))
	gatherErrs := make([]string, len(cuts))
	for i, m := range cuts {
		r, gerr, err := readingsAt(cfg.AtMinuteOfDay(m), at)
		if err == nil && gerr != "" && i == cur {
			err = fmt.Errorf("internal: the schedule reading at --at cannot be gathered, the served values could: %s", gerr)
		}
		if err != nil {
			return fmt.Errorf("from %s to %s UTC: %w", hhmm(m), hhmm(ends[i]), err)
		}
		passes[i], gatherErrs[i] = r, gerr
	}

	for tenant, tv := range tenants {
		if err := sameAsValues(tenant, tv, passes[cur][tenant]); err != nil {
			return err
		}
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

	for tenant, tv := range tenants {
		keys := map[string]bool{}
		for _, p := range passes {
			for k := range p[tenant] {
				keys[k] = true
			}
		}
		for k := range expiryOf[tenant] {
			keys[k] = true
		}
		for k := range keys {
			sch := servedSchedule{Segments: daySegments(cuts, ends, passes, gatherErrs, tenant, k)}
			if te, ok := expiryOf[tenant][k]; ok {
				sv, written := cfg.Tenants[tenant][te.MetricKey]
				if !written || sv.Expiry == nil {
					return fmt.Errorf("internal: tenant %s: the expiry of %q has no `expires:` in the merged config", tenant, te.MetricKey)
				}
				expired := te.Expired
				sch.Expires = sv.Expiry.Expires
				sch.Expired = &expired
			}
			tv.Schedules[k] = sch
		}
	}
	return nil
}

// daySegments is key k's day over the pieces [cuts[i], ends[i]), adjacent
// pieces with the same reading (or the same Gather error) merged. A piece
// whose gatherErrs entry is set is a segment carrying that error.
func daySegments(cuts, ends []int, passes []map[string]map[string]reading, gatherErrs []string, tenant, k string) []scheduleSegment {
	var segs []scheduleSegment
	var last reading
	lastServed, lastErr := false, ""
	for i, m := range cuts {
		gerr := gatherErrs[i]
		r, served := passes[i][tenant][k]
		if n := len(segs); n > 0 && gerr == lastErr && served == lastServed &&
			(!served || (sameFloat(r.value, last.value) && r.severity == last.severity)) {
			segs[n-1].To = hhmm(ends[i])
			continue
		}
		seg := scheduleSegment{From: hhmm(m), To: hhmm(ends[i])}
		switch {
		case gerr != "":
			seg.Error = gerr
		case served:
			seg.Value = jsonFloat(r.value)
			seg.Severity = r.severity
		default:
			seg.Value = jsonNull
		}
		segs = append(segs, seg)
		last, lastServed, lastErr = r, served, gerr
	}
	return segs
}

// sameAsValues checks that the piece of the day holding `at` reads, for the
// tenant, exactly the threshold keys, values and severities Values reads.
func sameAsValues(tenant string, tv servedTenantValues, piece map[string]reading) error {
	for k, sev := range tv.Severities {
		r, ok := piece[k]
		if !ok || r.severity != sev || jsonFloat(r.value) != tv.Values[k] {
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
func readingsAt(cfg *config.ThresholdConfig, at time.Time) (readings map[string]map[string]reading, gatherErr string, err error) {
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
	if _, gerr := reg.Gather(); gerr != nil {
		return nil, fmt.Sprintf("the exporter's /metrics cannot be gathered, so its scrape fails "+
			"as a whole (HTTP 500) and nothing is served%s: %v", sameSeriesKeys(keyed, results), gerr), nil
	}
	served, _, err := groupRows(keyed, results)
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
