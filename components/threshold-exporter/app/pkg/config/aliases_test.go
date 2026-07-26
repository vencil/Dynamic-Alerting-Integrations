package config

// aliases_test.go — #1231 mysql_cpu → mysql_threads_running rename mechanics.
//
// Adversarial test plan coverage (1a slice):
//   (1) equivalence invariant  — old-spelled and new-spelled configs resolve
//       to the SAME canonical row set (defaults-only / override / value:severity
//       suffix / disable three-state / expires), both with the legacy dual-emit twin
//   (2) _critical 16-cell matrix — base ∈ {old,new,both,absent} × critical ∈
//       {old,new,both,absent}, every cell explicit
//   (3) dual-emit lifecycle contract — table-driven: alias targets twin, everything
//       else (including canonical-prefix lookalikes) never does
//   (5) deprecation signal pins — notices name BOTH keys; independent pins ban the
//       "skipping" substring and the "ERROR:" prefix (Python CI fatal heuristics)
//   (6) adversarial pins — mysql_cpu_util typo stays an unknown-key Error;
//       mysql_cpu{version=""} stays rejected; dangling mysql_cpu_critical still errors
// plus the two consistency fixes that ride along: ResolveThresholdExpiriesAt keeps
// emitting the expiry event for an old-spelled override under renamed defaults, and
// ApplyProfiles' fill-in respects alias-equivalent tenant overrides (tenant beats
// profile across spellings).

import (
	"fmt"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"
)

// aliasTestNow is an arbitrary fixed instant so expires-sensitive cases are
// deterministic.
var aliasTestNow = time.Date(2026, 7, 1, 12, 0, 0, 0, time.UTC)

// rowSet renders resolved thresholds for one tenant as a sorted
// "component/metric/severity=value" list — a stable identity view that makes
// set equality assertions readable.
func rowSet(rows []ResolvedThreshold, tenant string) []string {
	out := []string{}
	for _, r := range rows {
		if r.Tenant != tenant {
			continue
		}
		out = append(out, fmt.Sprintf("%s/%s/%s=%g", r.Component, r.Metric, r.Severity, r.Value))
	}
	sort.Strings(out)
	return out
}

func assertRowSet(t *testing.T, got, want []string, context string) {
	t.Helper()
	if !reflect.DeepEqual(got, want) {
		t.Errorf("%s: resolved row set mismatch\n got: %v\nwant: %v", context, got, want)
	}
}

// --- canonicalKeyFor shape contract -----------------------------------------

func TestCanonicalKeyFor_Shapes(t *testing.T) {
	t.Parallel()
	tests := []struct {
		key       string
		wantKey   string
		wantAlias bool
	}{
		// the three canonicalizing shapes
		{"mysql_cpu", "mysql_threads_running", true},
		{"mysql_cpu_critical", "mysql_threads_running_critical", true},
		{`mysql_cpu{version="v2"}`, `mysql_threads_running{version="v2"}`, true},
		// exact-match contract: prefix lookalikes must NOT canonicalize —
		// they have to keep failing unknown-key validation (adversarial pin).
		{"mysql_cpu_util", "mysql_cpu_util", false},
		{"mysql_cpu_util_critical", "mysql_cpu_util_critical", false},
		// canonical spellings and unrelated keys pass through
		{"mysql_threads_running", "mysql_threads_running", false},
		{"mysql_threads_running_critical", "mysql_threads_running_critical", false},
		{"mysql_connections", "mysql_connections", false},
		{"_state_mysql_cpu", "_state_mysql_cpu", false},
	}
	for _, tt := range tests {
		gotKey, gotAlias := canonicalKeyFor(tt.key)
		if gotKey != tt.wantKey || gotAlias != tt.wantAlias {
			t.Errorf("canonicalKeyFor(%q) = (%q, %v), want (%q, %v)",
				tt.key, gotKey, gotAlias, tt.wantKey, tt.wantAlias)
		}
	}
}

// --- (1) equivalence invariant ----------------------------------------------

// TestAliasEquivalence_ResolveParity: a config written with the deprecated
// spelling and the equivalent config written with the canonical spelling must
// resolve to EXACTLY the same canonical row set — including the same legacy
// dual-emit twin — across defaults-only, override, inline severity suffix,
// disable three-state, and expires shapes. All four defaults×override
// spelling combinations are exercised where an override exists.
func TestAliasEquivalence_ResolveParity(t *testing.T) {
	t.Parallel()

	const tenant = "tenant-eq"
	oldK, newK := "mysql_cpu", "mysql_threads_running"

	build := func(defKey string, override *ScheduledValue, ovKey string) *ThresholdConfig {
		ov := map[string]ScheduledValue{}
		if override != nil {
			ov[ovKey] = *override
		}
		return &ThresholdConfig{
			Defaults: map[string]float64{defKey: 30},
			Tenants:  map[string]map[string]ScheduledValue{tenant: ov},
		}
	}

	past := &ExpiryMeta{Expires: "2020-01-01T00:00:00Z"}
	future := &ExpiryMeta{Expires: "2999-01-01T00:00:00Z"}

	cases := []struct {
		name     string
		override *ScheduledValue
		want     []string
	}{
		{"defaults only", nil,
			[]string{"mysql/cpu/warning=30", "mysql/threads_running/warning=30"}},
		{"tenant override", &ScheduledValue{Default: "45"},
			[]string{"mysql/cpu/warning=45", "mysql/threads_running/warning=45"}},
		{"inline severity suffix", &ScheduledValue{Default: "45:critical"},
			[]string{"mysql/cpu/critical=45", "mysql/threads_running/critical=45"}},
		{"disable three-state", &ScheduledValue{Default: "disable"},
			[]string{}},
		{"expires past reverts to default", &ScheduledValue{Default: "90", Expiry: past},
			[]string{"mysql/cpu/warning=30", "mysql/threads_running/warning=30"}},
		{"expires future keeps override", &ScheduledValue{Default: "90", Expiry: future},
			[]string{"mysql/cpu/warning=90", "mysql/threads_running/warning=90"}},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			for _, defKey := range []string{oldK, newK} {
				if tc.override == nil {
					got := rowSet(mustResolveAt(build(defKey, nil, "")), tenant)
					assertRowSet(t, got, tc.want, "defaults="+defKey)
					continue
				}
				for _, ovKey := range []string{oldK, newK} {
					got := rowSet(mustResolveAt(build(defKey, tc.override, ovKey)), tenant)
					assertRowSet(t, got, tc.want,
						fmt.Sprintf("defaults=%s override=%s", defKey, ovKey))
				}
			}
		})
	}
}

func mustResolveAt(c *ThresholdConfig) []ResolvedThreshold {
	rows, _ := c.ResolveAtWithStats(aliasTestNow)
	return rows
}

// --- (2) the _critical 16-cell matrix ----------------------------------------

// TestCriticalAliasMatrix enumerates base-default spelling {old,new,both,absent}
// × tenant _critical spelling {old,new,both,absent} — all 16 cells explicit.
// Distinct values per spelling prove WHO wins a both-spellings conflict:
// defaults old=71/new=72, critical old=91/new=92 → canonical (92/72) must win.
func TestCriticalAliasMatrix(t *testing.T) {
	t.Parallel()
	const tenant = "tenant-m"

	defaultsFor := map[string]map[string]float64{
		"old":    {"mysql_cpu": 71},
		"new":    {"mysql_threads_running": 72},
		"both":   {"mysql_cpu": 71, "mysql_threads_running": 72},
		"absent": {},
	}
	criticalFor := map[string]map[string]ScheduledValue{
		"old":    {"mysql_cpu_critical": {Default: "91"}},
		"new":    {"mysql_threads_running_critical": {Default: "92"}},
		"both":   {"mysql_cpu_critical": {Default: "91"}, "mysql_threads_running_critical": {Default: "92"}},
		"absent": {},
	}

	base71 := []string{"mysql/cpu/warning=71", "mysql/threads_running/warning=71"}
	base72 := []string{"mysql/cpu/warning=72", "mysql/threads_running/warning=72"}
	crit91 := []string{"mysql/cpu/critical=91", "mysql/threads_running/critical=91"}
	crit92 := []string{"mysql/cpu/critical=92", "mysql/threads_running/critical=92"}
	merged := func(sets ...[]string) []string {
		out := []string{}
		for _, s := range sets {
			out = append(out, s...)
		}
		sort.Strings(out)
		return out
	}

	cells := []struct {
		base, crit   string
		wantRows     []string
		wantErrors   int  // dangling-_critical errors
		wantDangling bool // error must name the NEW key spellings
		wantNotices  int
	}{
		{"old", "absent", base71, 0, false, 0},
		{"old", "old", merged(base71, crit91), 0, false, 1}, // rename notice
		{"old", "new", merged(base71, crit92), 0, false, 0},
		{"old", "both", merged(base71, crit92), 0, false, 1}, // conflict notice
		{"new", "absent", base72, 0, false, 0},
		{"new", "old", merged(base72, crit91), 0, false, 1}, // rename notice
		{"new", "new", merged(base72, crit92), 0, false, 0},
		{"new", "both", merged(base72, crit92), 0, false, 1},  // conflict notice
		{"both", "absent", base72, 0, false, 1},               // defaults-conflict notice
		{"both", "old", merged(base72, crit91), 0, false, 2},  // defaults-conflict + rename
		{"both", "new", merged(base72, crit92), 0, false, 1},  // defaults-conflict
		{"both", "both", merged(base72, crit92), 0, false, 2}, // defaults-conflict + conflict
		{"absent", "absent", []string{}, 0, false, 0},
		{"absent", "old", []string{}, 1, true, 1},  // dangling error + rename notice
		{"absent", "new", []string{}, 1, true, 0},  // dangling error
		{"absent", "both", []string{}, 1, true, 1}, // dangling error + conflict notice
	}

	for _, cell := range cells {
		t.Run("base="+cell.base+"/critical="+cell.crit, func(t *testing.T) {
			t.Parallel()
			overrides := map[string]ScheduledValue{}
			for k, v := range criticalFor[cell.crit] {
				overrides[k] = v
			}
			cfg := &ThresholdConfig{
				Defaults: defaultsFor[cell.base],
				Tenants:  map[string]map[string]ScheduledValue{tenant: overrides},
			}

			assertRowSet(t, rowSet(mustResolveAt(cfg), tenant), cell.wantRows, "rows")

			kv := cfg.ValidateTenantKeys()
			if len(kv.Errors) != cell.wantErrors {
				t.Errorf("Errors = %d (%v), want %d", len(kv.Errors), kv.Errors, cell.wantErrors)
			}
			if cell.wantDangling {
				joined := strings.Join(kv.Errors, "\n")
				// #1227 convention carried through the rename: the dangling
				// error names the NEW spellings so the fix advice is current.
				for _, want := range []string{"mysql_threads_running_critical", `"mysql_threads_running"`, "has no base metric"} {
					if !strings.Contains(joined, want) {
						t.Errorf("dangling error should mention %q, got: %s", want, joined)
					}
				}
			}
			if len(kv.Notices) != cell.wantNotices {
				t.Errorf("Notices = %d (%v), want %d", len(kv.Notices), kv.Notices, cell.wantNotices)
			}
		})
	}
}

// --- (3) dual-emit lifecycle contract ----------------------------------------

// TestDualEmitContract: ONLY keys whose canonical form is an alias target
// dual-emit; every other key — explicitly including a key that merely starts
// with the canonical name — emits exactly one row. This is the table the
// transition-window teardown will flip: when the alias entry is removed, the
// twin rows disappear with it.
func TestDualEmitContract(t *testing.T) {
	t.Parallel()
	const tenant = "tenant-d"
	tests := []struct {
		key      string
		wantRows []string
	}{
		{"mysql_threads_running", []string{"mysql/cpu/warning=80", "mysql/threads_running/warning=80"}},
		{"mysql_cpu", []string{"mysql/cpu/warning=80", "mysql/threads_running/warning=80"}},
		{"mysql_connections", []string{"mysql/connections/warning=80"}},
		{"container_cpu", []string{"container/cpu/warning=80"}},
		// canonical-prefix lookalike: exact-match reverse lookup, no twin
		{"mysql_threads_running_extra", []string{"mysql/threads_running_extra/warning=80"}},
	}
	for _, tt := range tests {
		t.Run(tt.key, func(t *testing.T) {
			t.Parallel()
			cfg := &ThresholdConfig{
				Defaults: map[string]float64{tt.key: 80},
				Tenants:  map[string]map[string]ScheduledValue{tenant: {}},
			}
			assertRowSet(t, rowSet(mustResolveAt(cfg), tenant), tt.wantRows, tt.key)
		})
	}
}

// --- (5) deprecation signal pins ----------------------------------------------

// noticeFixture produces every notice variant in one config: a defaults-side
// both-spellings conflict, a tenant rename notice (base), a tenant rename
// notice (_critical shape), and a tenant both-spellings conflict.
func noticeFixture() *ThresholdConfig {
	return &ThresholdConfig{
		Defaults: map[string]float64{"mysql_cpu": 70, "mysql_threads_running": 72},
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-n": {
				"mysql_cpu":          {Default: "60"},
				"mysql_cpu_critical": {Default: "90"},
			},
			"tenant-c": {
				"mysql_cpu":             {Default: "60"},
				"mysql_threads_running": {Default: "65"},
			},
		},
	}
}

func TestDeprecationNotices_NameBothKeysAndNeverBlock(t *testing.T) {
	t.Parallel()
	kv := noticeFixture().ValidateTenantKeys()

	if len(kv.Errors) != 0 {
		t.Fatalf("deprecation must ride Notices, never Errors (writes stay unblocked), got Errors: %v", kv.Errors)
	}
	// defaults conflict + tenant-n rename ×2 + tenant-c conflict
	if len(kv.Notices) != 4 {
		t.Fatalf("expected 4 notices, got %d: %v", len(kv.Notices), kv.Notices)
	}
	for _, n := range kv.Notices {
		// #1227 convention: "there is a problem" is not actionable — every
		// notice must name BOTH the deprecated and the replacement key.
		for _, want := range []string{"mysql_cpu", "mysql_threads_running"} {
			if !strings.Contains(n, want) {
				t.Errorf("notice must mention %q, got: %q", want, n)
			}
		}
	}
}

// TestDeprecationNotices_ForbiddenTokens is a standalone wording tripwire:
// the Python CI fatal heuristic is substring-based, and the two tokens below
// would flip a benign advisory into a build-breaking fatal. Pinned separately
// from the content test so a future rewording PR trips THIS test by name.
func TestDeprecationNotices_ForbiddenTokens(t *testing.T) {
	t.Parallel()
	kv := noticeFixture().ValidateTenantKeys()
	if len(kv.Notices) == 0 {
		t.Fatal("fixture must produce notices for the wording pins to bite")
	}
	for _, n := range kv.Notices {
		if strings.Contains(n, "skipping") {
			t.Errorf("notice must not contain the %q substring (Python CI fatal heuristic): %q", "skipping", n)
		}
		if strings.HasPrefix(n, "ERROR:") {
			t.Errorf("notice must not start with %q (Python CI fatal heuristic): %q", "ERROR:", n)
		}
	}
}

// --- (6) adversarial pins -------------------------------------------------------

func TestAliasAdversarialPins(t *testing.T) {
	t.Parallel()

	t.Run("typo mysql_cpu_util stays an unknown-key Error", func(t *testing.T) {
		t.Parallel()
		cfg := &ThresholdConfig{
			Defaults: map[string]float64{"mysql_threads_running": 72},
			Tenants: map[string]map[string]ScheduledValue{
				"tenant-t": {"mysql_cpu_util": {Default: "90"}},
			},
		}
		kv := cfg.ValidateTenantKeys()
		if len(kv.Errors) != 1 || !strings.Contains(kv.Errors[0], `"mysql_cpu_util"`) ||
			!strings.Contains(kv.Errors[0], "unknown key") {
			t.Errorf("typo must stay an unknown-key Error, got: %v", kv.Errors)
		}
		if len(kv.Notices) != 0 {
			t.Errorf("typo must NOT be treated as a deprecated alias, got notices: %v", kv.Notices)
		}
	})

	t.Run("mysql_cpu{version=\"\"} still rejected", func(t *testing.T) {
		t.Parallel()
		cfg := &ThresholdConfig{
			Defaults: map[string]float64{"mysql_cpu": 70},
			Tenants: map[string]map[string]ScheduledValue{
				"tenant-t": {`mysql_cpu{version=""}`: {Default: "60"}},
			},
		}
		kv := cfg.ValidateTenantKeys()
		joined := strings.Join(kv.Errors, "\n")
		if !strings.Contains(joined, "empty version label") {
			t.Errorf("empty version label must stay a blocking Error after canonicalization, got: %v", kv.Errors)
		}
	})

	t.Run("dangling mysql_cpu_critical still errors", func(t *testing.T) {
		t.Parallel()
		cfg := &ThresholdConfig{
			Defaults: map[string]float64{},
			Tenants: map[string]map[string]ScheduledValue{
				"tenant-t": {"mysql_cpu_critical": {Default: "90"}},
			},
		}
		kv := cfg.ValidateTenantKeys()
		if len(kv.Errors) != 1 || !strings.Contains(kv.Errors[0], "has no base metric") {
			t.Errorf("base-less _critical must keep its dangling Error, got: %v", kv.Errors)
		}
	})
}

// --- consistency riders: expiry events + profile priority across spellings ------

// TestResolveThresholdExpiries_AliasedKeyUnderRenamedDefaults: once the
// platform defaults migrate to the new spelling, a tenant's old-spelled
// time-boxed override STILL fail-safes back to the default (resolve is
// canonical), so the da_config_event expiry entry must keep emitting too —
// and ValidateTenantKeys must not falsely claim the expires is ignored.
func TestResolveThresholdExpiries_AliasedKeyUnderRenamedDefaults(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_threads_running": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-x": {
				"mysql_cpu": {Default: "95", Expiry: &ExpiryMeta{Expires: "2020-01-01T00:00:00Z", Reason: "incident"}},
			},
		},
	}

	entries := cfg.ResolveThresholdExpiriesAt(aliasTestNow)
	if len(entries) != 1 || entries[0].MetricKey != "mysql_cpu" || !entries[0].Expired {
		t.Fatalf("expected 1 expired entry echoing the authored key %q, got: %+v", "mysql_cpu", entries)
	}

	kv := cfg.ValidateTenantKeys()
	for _, e := range kv.Errors {
		if strings.Contains(e, "expires") {
			t.Errorf("in-scope (via alias) expires must not error, got: %q", e)
		}
	}

	// And the value actually fail-safes to the platform default, dual-emitted.
	assertRowSet(t, rowSet(mustResolveAt(cfg), "tenant-x"),
		[]string{"mysql/cpu/warning=80", "mysql/threads_running/warning=80"}, "expired alias override")
}

// TestApplyProfiles_TenantWinsAcrossSpellings: the four-layer priority
// (tenant override beats profile) must hold when the two layers use different
// spellings of the same threshold — in BOTH directions. Without the
// alias-aware fill-in check, the profile value would land next to the
// tenant's and the canonical-wins dedup would silently invert the priority.
func TestApplyProfiles_TenantWinsAcrossSpellings(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name       string
		profileKey string
		tenantKey  string
	}{
		{"profile new-spelled vs tenant old-spelled", "mysql_threads_running", "mysql_cpu"},
		{"profile old-spelled vs tenant new-spelled", "mysql_cpu", "mysql_threads_running"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			cfg := &ThresholdConfig{
				Defaults: map[string]float64{"mysql_threads_running": 70},
				Profiles: map[string]map[string]ScheduledValue{
					"gold": {tc.profileKey: {Default: "85"}},
				},
				Tenants: map[string]map[string]ScheduledValue{
					"tenant-p": {
						"_profile":   {Default: "gold"},
						tc.tenantKey: {Default: "60"},
					},
				},
			}
			cfg.ApplyProfiles()
			assertRowSet(t, rowSet(mustResolveAt(cfg), "tenant-p"),
				[]string{"mysql/cpu/warning=60", "mysql/threads_running/warning=60"},
				"tenant override must win")
		})
	}
}

// --- dimensional dual-emit (#1231 review F1 = C4/C6/C9) --------------------------

// dimRowSet renders resolved thresholds for one tenant INCLUDING dimensional
// labels, so a twin's label fidelity is asserted, not just its base identity.
func dimRowSet(rows []ResolvedThreshold, tenant string) []string {
	out := []string{}
	for _, r := range rows {
		if r.Tenant != tenant {
			continue
		}
		out = append(out, fmt.Sprintf("%s/%s/%s{%s}=%g",
			r.Component, r.Metric, r.Severity, canonicalLabelKey(r.CustomLabels, r.RegexLabels), r.Value))
	}
	sort.Strings(out)
	return out
}

// TestDualEmitContract_Dimensional extends the dual-emit lifecycle contract to
// the dimensional resolution path (review F1): a dimensional override whose
// base is an alias target must ship the legacy twin with IDENTICAL labels and
// value — otherwise the "both series fire in lockstep during the transition
// window" guarantee holds for base/_critical shapes but silently breaks the
// moment a tenant upgrades a plain override to a dimensional one.
func TestDualEmitContract_Dimensional(t *testing.T) {
	t.Parallel()
	const tenant = "tenant-dim"
	tests := []struct {
		key      string
		value    string
		wantRows []string
	}{
		// canonical spelling → legacy twin rides along, same labels + value
		{`mysql_threads_running{env="prod"}`, "70", []string{
			"mysql/cpu/warning{env=prod}=70",
			"mysql/threads_running/warning{env=prod}=70",
		}},
		// deprecated spelling → canonicalized first, then the same twin pair
		{`mysql_cpu{env="prod"}`, "70", []string{
			"mysql/cpu/warning{env=prod}=70",
			"mysql/threads_running/warning{env=prod}=70",
		}},
		// inline severity suffix propagates to BOTH rows
		{`mysql_threads_running{env="prod"}`, "70:critical", []string{
			"mysql/cpu/critical{env=prod}=70",
			"mysql/threads_running/critical{env=prod}=70",
		}},
		// disable suppresses BOTH rows (no canonical row → no twin)
		{`mysql_threads_running{env="prod"}`, "disable", []string{}},
		// non-alias dimensional key → exactly one row
		{`mysql_connections{env="prod"}`, "70",
			[]string{"mysql/connections/warning{env=prod}=70"}},
		// canonical-prefix lookalike: exact-match reverse lookup, no twin
		{`mysql_threads_running_extra{env="prod"}`, "70",
			[]string{"mysql/threads_running_extra/warning{env=prod}=70"}},
	}
	for _, tt := range tests {
		t.Run(tt.key+"="+tt.value, func(t *testing.T) {
			t.Parallel()
			cfg := &ThresholdConfig{
				Tenants: map[string]map[string]ScheduledValue{
					tenant: {tt.key: {Default: tt.value}},
				},
			}
			got := dimRowSet(mustResolveAt(cfg), tenant)
			if !reflect.DeepEqual(got, tt.wantRows) {
				t.Errorf("%s=%s: dimensional row set mismatch\n got: %v\nwant: %v",
					tt.key, tt.value, got, tt.wantRows)
			}
		})
	}
}

// --- expiry events under same-map spelling conflicts (#1231 review F2 = C1) -------

// TestResolveThresholdExpiries_CanonicalSiblingSuppressesLegacyEvent: when a
// tenant carries BOTH spellings, canonical-wins dedup means the legacy entry's
// value NEVER applied — so its lapsed `expires:` must not emit a
// threshold_expired event. The collector renders that event as "已過期並自動停用"
// (for: 0s, unconditional page); firing it for a value that never governed
// anything pages the tenant with fiction.
func TestResolveThresholdExpiries_CanonicalSiblingSuppressesLegacyEvent(t *testing.T) {
	t.Parallel()
	pastExpiry := &ExpiryMeta{Expires: "2020-01-01T00:00:00Z", Reason: "incident"}
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_threads_running": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-c": {
				"mysql_cpu":             {Default: "95", Expiry: pastExpiry},
				"mysql_threads_running": {Default: "65"},
			},
		},
	}

	entries := cfg.ResolveThresholdExpiriesAt(aliasTestNow)
	if len(entries) != 0 {
		t.Fatalf("dedup loser's expiry must not emit an event (its value never applied), got: %+v", entries)
	}

	// WHY zero events is right: the canonical sibling's 65 was in effect the
	// whole time — nothing reverted when the legacy expires lapsed.
	assertRowSet(t, rowSet(mustResolveAt(cfg), "tenant-c"),
		[]string{"mysql/cpu/warning=65", "mysql/threads_running/warning=65"}, "conflict rows")

	// Scoping pin: the suppression targets the LOSING entry only — the
	// canonical sibling's own time-box stays fully honored alongside it.
	cfg2 := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_threads_running": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-c2": {
				"mysql_cpu":             {Default: "95", Expiry: pastExpiry},
				"mysql_threads_running": {Default: "65", Expiry: pastExpiry},
			},
		},
	}
	entries2 := cfg2.ResolveThresholdExpiriesAt(aliasTestNow)
	if len(entries2) != 1 || entries2[0].MetricKey != "mysql_threads_running" || !entries2[0].Expired {
		t.Fatalf("canonical sibling's own expiry must still emit exactly once, got: %+v", entries2)
	}
}

// --- profile-layer canonicalization (#1231 review F4 = C3) -----------------------

// TestApplyProfiles_DualSpelledProfile_CanonicalWinsDeterministically: a
// profile carrying BOTH spellings was the only order-dependent layer — the
// fill-in iterated the raw profile map, so whichever spelling Go's randomized
// iteration visited first won, flipping the tenant's effective value across
// reloads. The canonical entry must win every time.
func TestApplyProfiles_DualSpelledProfile_CanonicalWinsDeterministically(t *testing.T) {
	t.Parallel()
	newCfg := func() *ThresholdConfig {
		return &ThresholdConfig{
			Defaults: map[string]float64{"mysql_threads_running": 70},
			Profiles: map[string]map[string]ScheduledValue{
				"gold": {
					"mysql_cpu":             {Default: "85"},
					"mysql_threads_running": {Default: "95"},
				},
			},
			Tenants: map[string]map[string]ScheduledValue{
				"tenant-p": {"_profile": {Default: "gold"}},
			},
		}
	}
	// Go randomizes map iteration per range statement, so 40 fresh configs
	// make an order-dependent implementation fail with p ≈ 1-2^-40.
	for i := 0; i < 40; i++ {
		cfg := newCfg()
		cfg.ApplyProfiles()
		assertRowSet(t, rowSet(mustResolveAt(cfg), "tenant-p"),
			[]string{"mysql/cpu/warning=95", "mysql/threads_running/warning=95"},
			fmt.Sprintf("iter %d: canonical profile value must win deterministically", i))
		if _, leaked := cfg.Tenants["tenant-p"]["mysql_cpu"]; leaked {
			t.Fatalf("iter %d: fill-in must never inject the deprecated spelling", i)
		}
	}
}

// TestApplyProfiles_LegacyOnlyProfile_FillsInCanonicalSpelling: a legacy-only
// profile must fill in under the CANONICAL spelling. Injecting the deprecated
// key verbatim hands a clean tenant a spelling it never wrote — a false
// tenant-facing rename NOTICE plus a phantom da_config_deprecated_keys count.
// The migration signal belongs to the profile author's surface (which has no
// channel today — see the F4 report note), never to the tenant.
func TestApplyProfiles_LegacyOnlyProfile_FillsInCanonicalSpelling(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_threads_running": 70},
		Profiles: map[string]map[string]ScheduledValue{
			"gold": {"mysql_cpu": {Default: "85"}},
		},
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-clean": {"_profile": {Default: "gold"}},
		},
	}
	cfg.ApplyProfiles()

	// The value applies under the canonical key, dual-emitted as usual.
	assertRowSet(t, rowSet(mustResolveAt(cfg), "tenant-clean"),
		[]string{"mysql/cpu/warning=85", "mysql/threads_running/warning=85"}, "legacy-only profile")

	if _, injected := cfg.Tenants["tenant-clean"]["mysql_cpu"]; injected {
		t.Error("fill-in must inject the canonical spelling, not the deprecated one")
	}
	if _, ok := cfg.Tenants["tenant-clean"]["mysql_threads_running"]; !ok {
		t.Error("fill-in must land on the canonical key")
	}
	kv := cfg.ValidateTenantKeys()
	if len(kv.Notices) != 0 {
		t.Errorf("profile-sourced legacy spelling must not create tenant notices, got: %v", kv.Notices)
	}
	_, stats := cfg.ResolveAtWithStats(aliasTestNow)
	if n, present := stats.PerTenantDeprecatedKeys["tenant-clean"]; present {
		t.Errorf("gauge must not count profile-sourced spellings against the tenant, got %d", n)
	}
}

// --- stats channel for the da_config_deprecated_keys gauge -----------------------

func TestResolveStats_PerTenantDeprecatedKeys(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults: map[string]float64{"mysql_threads_running": 80},
		Tenants: map[string]map[string]ScheduledValue{
			"tenant-legacy": {
				"mysql_cpu":          {Default: "60"},
				"mysql_cpu_critical": {Default: "90"},
			},
			"tenant-clean": {"mysql_threads_running": {Default: "61"}},
		},
	}
	_, stats := cfg.ResolveAtWithStats(aliasTestNow)
	if got := stats.PerTenantDeprecatedKeys["tenant-legacy"]; got != 2 {
		t.Errorf("tenant-legacy deprecated count = %d, want 2", got)
	}
	if _, present := stats.PerTenantDeprecatedKeys["tenant-clean"]; present {
		t.Errorf("tenant-clean must have NO entry (gauge doesn't emit for migrated tenants), got %v",
			stats.PerTenantDeprecatedKeys)
	}
}
