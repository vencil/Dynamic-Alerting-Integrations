package main

// key_verdict_day.go — the served reading the key verdict judges on
// (#3835): every tenant over its whole UTC day, never at one instant.
//
// A key is read when some reader reads it in SOME part of the day (ADR-038
// decision 1, as decision 8 already judges a `disable`): what the verdict
// says of a tree depends on --at only through what has expired (`expires:`),
// never on the time of day the check runs at.
//
// ⛔ NO READING OF ITS OWN, as for served-values. Each tenant's day is cut
// where its own values may change (ScheduleCuts of the tenant alone, as
// served_schedules.go and served_disable.go cut it); each piece is the tree
// with every schedule fixed to that minute (config.AtMinuteOfDay), resolved
// by the exporter's resolver at --at itself — so its expiry readings are
// --at's — and put through the exporter's user_threshold builder
// (thresholdmetric.Emit). servedKeys reads each piece as served-values
// reads --at, and the pieces are folded into one entry per key (foldDay).
// Tenants cut at the same minute are read together: a tenant's rows depend
// on its own map and the config-wide settings, never on another tenant's.
//
// ⛔ ROWS BEFORE THE GATHER. A piece in which two rows of a tenant give one
// series cannot be gathered: the exporter's scrape fails as a whole (HTTP
// 500) and nothing is served. Here that piece is read off the rows the
// builder made before any registry sees them — both colliding keys are
// read. The collision itself is the main gate's metrics_not_gatherable
// (gatherVerdict, over every cut of the day); the key verdict never fails
// for it, and no part of the day goes unjudged because of it.
//
// ⛔ NO VALUE JUDGED. A piece whose value the resolver refuses (non-finite,
// unparsed: served-values' value_non_finite / value_unparsed) reads the key
// all the same — the resolver read it to refuse it. Whether a value is one
// the exporter serves is the main gate's (value_not_served) and
// config.KeyValidation's, which judge every value of the day.

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/prometheus/client_golang/prometheus"

	"github.com/vencil/threshold-exporter/internal/thresholdmetric"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// dayReasonOrder is the order foldDay picks a key's reason in when no part
// of the day reads it — closest to being read first: base_disabled (read:
// the critical / dimensional phase switches it off with its base), then the
// reasons that do not change over the day, then no_row, and disabled last.
// A part in which the key reads `disable` names the day only when every
// part does: the key is then unread only because of its `disable`, which
// the verdict reads as disable_noop when that `disable` switches nothing off
// (and as read when it does). A part with no row for another reason is the
// key's defect whatever its other parts say — a `disable` elsewhere that
// switches nothing off does not turn it into a warning.
var dayReasonOrder = []string{
	keyReasonBaseDisabled,
	keyReasonShadowed,
	keyReasonUndeliverable,
	keyReasonRootDefaultRefused,
	keyReasonRetired,
	keyReasonUnregistered,
	keyReasonNotExporterKey,
	keyReasonStateFilterUndeclared,
	keyReasonNoRow,
	keyReasonDisabled,
}

// dayRank orders two readings of one key: lower is closer to being read. A
// reading the exporter reads is 0; an unread one ranks by dayReasonOrder,
// a reason not listed there after all of them, by its name.
func dayRank(e servedKey) (int, string) {
	if e.Exporter {
		return 0, ""
	}
	for i, r := range dayReasonOrder {
		if e.Reason == r {
			return i + 1, ""
		}
	}
	return len(dayReasonOrder) + 1, e.Reason
}

// closerToRead reports whether reading a ranks before reading b (dayRank).
func closerToRead(a, b servedKey) bool {
	ra, sa := dayRank(a)
	rb, sb := dayRank(b)
	if ra != rb {
		return ra < rb
	}
	return sa < sb
}

// servedDay is the served reading of every tenant of cfg over its whole UTC
// day, at `at` for expiry readings only (see the file comment): an entry of
// Keys per key, read when some part of the day reads it, else unread with
// the reason dayReasonOrder picks; Values / Severities / Dropped /
// Unserved hold what any part of the day holds (the verdict reads which
// keys they hold, not their values). undeliverable and refusedTexts are the
// same load's LoadReport.Undeliverable and refusedTextsOf. Keys are cfg's
// spelling (spellAsWritten, addDisableEffectsWith come after).
func servedDay(cfg *config.ThresholdConfig, at time.Time,
	undeliverable map[string]map[string]config.ScheduledValue, refusedTexts map[string]string,
) (map[string]servedTenantValues, error) {
	rootRefused := cfg.RefusedRootDefaults()
	byMinute := map[int][]string{}
	for tenant := range cfg.Tenants {
		for _, m := range onlyTenants(cfg, []string{tenant}).ScheduleCuts() {
			byMinute[m] = append(byMinute[m], tenant)
		}
	}
	minutes := make([]int, 0, len(byMinute))
	for m := range byMinute {
		minutes = append(minutes, m)
	}
	sort.Ints(minutes)
	out := make(map[string]servedTenantValues, len(cfg.Tenants))
	for _, m := range minutes {
		tenants := byMinute[m]
		sort.Strings(tenants)
		part := onlyTenants(cfg, tenants).AtMinuteOfDay(m)
		kept, dropped, err := rowsBeforeGather(part, at)
		if err != nil {
			return nil, fmt.Errorf("from %s UTC: %w", hhmm(m), err)
		}
		for _, tenant := range tenants {
			p := pieceReading(part, tenant, at, kept[tenant], dropped[tenant], undeliverable[tenant],
				rootRefused, refusedTexts)
			if d, ok := out[tenant]; ok {
				foldDay(d, p)
			} else {
				out[tenant] = p
			}
		}
	}
	for tenant, tv := range out {
		for k, e := range tv.Keys {
			if strings.HasPrefix(k, "_") {
				continue
			}
			// The schedule as written: a piece's value is fixed, so it has none.
			e.DeadEntries = deadEntriesOf(cfg.Tenants[tenant], undeliverable[tenant], k)
			tv.Keys[k] = e
		}
	}
	return out, nil
}

// pieceReading is one tenant's reading of one piece of the day (part: the
// tree at that minute), as servedValuesFilled reads --at: kept / dropped
// are its rows before the Gather (rowsBeforeGather). A value the resolver
// refuses reads the key (the file comment's NO VALUE JUDGED).
func pieceReading(part *config.ThresholdConfig, tenant string, at time.Time,
	kept map[string][]config.ResolvedThreshold, dropped map[string][]string,
	undeliverable map[string]config.ScheduledValue, rootRefused, refusedTexts map[string]string,
) servedTenantValues {
	overrides := part.Tenants[tenant]
	tv := servedTenantValues{Values: map[string]any{}, Severities: map[string]string{},
		Dropped: map[string][]string{}, Unserved: map[string]any{}}
	for k, rows := range kept {
		tv.Values[k] = true
		if k != customAlertsKey {
			tv.Severities[k] = rows[0].Severity
		}
	}
	for k, errs := range dropped {
		tv.Dropped[k] = errs
	}
	for _, k := range reservedValueKeys(part) {
		tv.Values[k] = true
	}
	addUnserved(tv, part, overrides, rootRefused, refusedTexts)
	for k, sv := range undeliverable {
		tv.Unserved[k] = rawScheduledValue(sv)
	}
	view := part.FamilyView(tenant)
	tv.Keys = servedKeys(tv, overrides, undeliverable, at, recordedReasons(part, tenant, at),
		func(k string) bool { return view.SwitchedOffWithBase(k, at, at) })
	addRootRefused(tv, overrides, rootRefused)
	for k, e := range tv.Keys {
		if e.Reason == keyReasonValueNonFinite || e.Reason == keyReasonValueUnparsed {
			e.Exporter, e.Reason = true, ""
			tv.Keys[k] = e
		}
	}
	return tv
}

// foldDay folds one piece's reading p of a tenant into its day so far d:
// every key either holds, and for a key both read, the reading closer to
// being read (closerToRead; the earlier piece on a tie). A key one side has
// no Keys entry for is, on that side, either served — a key the tenant does
// not write (a root dimensional default, #2031) has an entry only where it
// has no row — or not the tenant's at all: served there, it is read.
func foldDay(d, p servedTenantValues) {
	read := func(e servedKey) servedKey {
		e.Exporter, e.Reason, e.Refused = true, "", ""
		return e
	}
	for k, e := range d.Keys {
		if _, ok := p.Keys[k]; !ok && hasRow(p, k) {
			d.Keys[k] = read(e)
		}
	}
	for k, e := range p.Keys {
		prev, ok := d.Keys[k]
		switch {
		case !ok && hasRow(d, k):
			d.Keys[k] = read(e)
		case !ok || closerToRead(e, prev):
			d.Keys[k] = e
		}
	}
	for k, v := range p.Values {
		if _, ok := d.Values[k]; !ok {
			d.Values[k] = v
		}
	}
	for k, v := range p.Severities {
		if _, ok := d.Severities[k]; !ok {
			d.Severities[k] = v
		}
	}
	for k, v := range p.Dropped {
		if _, ok := d.Dropped[k]; !ok {
			d.Dropped[k] = v
		}
	}
	for k, v := range p.Unserved {
		if _, ok := d.Unserved[k]; !ok {
			d.Unserved[k] = v
		}
	}
}

// hasRow reports whether the reading tv has a /metrics row, kept or
// dropped, for key k (its canonical spelling, as Values and Dropped key
// them).
func hasRow(tv servedTenantValues, k string) bool {
	canon, _ := config.CanonicalKeyFor(k)
	_, kept := tv.Values[canon]
	_, dropped := tv.Dropped[canon]
	return kept || dropped
}

// reservedValueKeys is the reserved keys servedValuesFilled's Values holds
// for every tenant of cfg, whatever their reading: `_metadata`,
// `_severity_dedup`, `_silent_mode`, and `_state_<filter>` for every filter
// the root declares (key_verdict_day_test.go holds the two equal).
func reservedValueKeys(cfg *config.ThresholdConfig) []string {
	out := []string{"_metadata", "_severity_dedup", "_silent_mode"}
	for name := range cfg.StateFilters {
		out = append(out, "_state_"+name)
	}
	sort.Strings(out)
	return out
}

// rowsBeforeGather is the user_threshold rows the exporter builds for cfg at
// `at`, per tenant and key, before any registry gathers them: the rows the
// builder (thresholdmetric.Emit) makes, and apart, the rejection of each it
// drops (groupRows). Never a Gather verdict: see the file comment.
func rowsBeforeGather(cfg *config.ThresholdConfig, at time.Time) (
	kept map[string]map[string][]config.ResolvedThreshold, dropped map[string]map[string][]string, err error,
) {
	keyed, _, err := cfg.ResolveAtWithKeysSilent(at)
	if err != nil {
		return nil, nil, err
	}
	rows := make([]config.ResolvedThreshold, len(keyed))
	for i, k := range keyed {
		rows[i] = k.ResolvedThreshold
	}
	results := make([]emitResult, len(rows))
	ch := make(chan prometheus.Metric, 64)
	done := make(chan struct{})
	go func() {
		for range ch { //nolint:revive // drained: only the builder's verdict on each row is read
		}
		close(done)
	}()
	thresholdmetric.Emit(ch, rows, func(i int, m prometheus.Metric, err error) {
		results[i] = emitResult{reported: true, metric: m, err: err}
	})
	close(ch)
	<-done
	return groupRows(keyed, results)
}
