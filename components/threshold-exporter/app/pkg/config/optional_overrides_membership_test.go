package config

// Membership pins for the third slot (#1189 / TRK-337). PR-A wired the FIELD
// through every merge path without changing behaviour; this is the pass where
// ValidateTenantKeys starts consulting it, so a key the platform declares
// without a value becomes something a tenant can actually set.
//
// The membership universe is now TWO sets, and the interesting content of this
// file is where they are deliberately NOT unioned:
//
//	flat        accepted   — the point of the change, and the one gap: nothing
//	                         emits for it yet, so a tenant who sets one gets no
//	                         row and no signal at all. Held shut by
//	                         tests/shared/test_optional_overrides_tripwire.py
//	                         until the emission loop lands, because no
//	                         supported producer can put a key on the list
//	dimensional accepted   — resolveDimensionalRows never consults defaults, so
//	                         the row emits the moment it is written
//	_critical   REFUSED    — resolveCriticalRows keys off defaults[base] and
//	                         drops the row otherwise; accepting would mean
//	                         "write succeeds, nothing emits, log-only signal",
//	                         and unlike the flat shape this one is writable on
//	                         a live config TODAY
//	expires:    REFUSED    — nothing to fail-safe back to; on lapse the
//	                         threshold would have no value at all
//
// So the rule this file pins is not the tidy "never accept what the resolver
// will silently drop" — the flat row breaks that. It is: never accept it where
// a tenant can actually get there. The two refusals guard shapes that are
// reachable now; the tripwire guards the one that is not, mechanically rather
// than by promise. A future pass that widens membership "for consistency" has
// to delete a test that says why not.

import (
	"sort"
	"strings"
	"testing"
	"time"
)

// aliasUnderTest returns the one deprecated spelling currently inside its
// transition window, taken from the alias table instead of hard-coded.
//
// Two reasons, both load-bearing. It keeps this file under the #1231 pygrep
// gate that forbids re-introducing the retired key as a live value: the seven
// files that DO spell it literally each bought a per-file entry on that hook's
// exclude list, which blinds the gate to everything else in them forever. And
// when the window closes and the table empties, these cases retire with it
// rather than pinning a spelling the platform has already withdrawn.
func aliasUnderTest(t *testing.T) (legacy, canonical string) {
	t.Helper()
	legacies := make([]string, 0, len(deprecatedKeyAliases))
	for l := range deprecatedKeyAliases {
		legacies = append(legacies, l)
	}
	if len(legacies) == 0 {
		t.Skip("no deprecated alias is inside its transition window")
	}
	sort.Strings(legacies) // deterministic if the table ever holds more than one
	return legacies[0], deprecatedKeyAliases[legacies[0]]
}

// declaredCfg builds a config whose platform surface is exactly one valued key
// and one declared-without-value key, with a single tenant's overrides.
func declaredCfg(declared []string, overrides map[string]ScheduledValue) *ThresholdConfig {
	return &ThresholdConfig{
		Defaults:          map[string]float64{"mysql_connections": 80},
		OptionalOverrides: declared,
		Tenants:           map[string]map[string]ScheduledValue{"t-1": overrides},
	}
}

func TestDeclaredKey_FlatOverrideIsAccepted(t *testing.T) {
	t.Parallel()
	cfg := declaredCfg(
		[]string{"oracle_wait_time_rate"},
		map[string]ScheduledValue{"oracle_wait_time_rate": {Default: "5"}},
	)

	kv := cfg.ValidateTenantKeys()

	if len(kv.Errors) != 0 {
		t.Fatalf("a declared key must be settable, got errors: %v", kv.Errors)
	}
	if len(kv.Notices) != 0 {
		t.Fatalf("no deprecation involved, want 0 notices, got: %v", kv.Notices)
	}
}

// ⭐ The pin the whole design rests on. ValidateTenantKeys compares the
// tenant's CANONICALIZED key against canonicalizeDefaults' output, so the
// declared list has to be canonicalized by the same table — otherwise a key
// listed under its retired spelling never matches, the platform declares
// something permanently un-settable, and nothing says so. That is the exact
// silent drift this change exists to end, reappearing inside its own fix.
//
// Uses the one live alias (mysql_cpu → mysql_threads_running), not a synthetic
// one: a test that invents its own alias stops covering the real table.
func TestDeclaredKey_ListedUnderRetiredSpellingStillMatches(t *testing.T) {
	t.Parallel()
	legacy, canonical := aliasUnderTest(t)
	cfg := declaredCfg(
		[]string{legacy}, // platform listed the OLD spelling
		map[string]ScheduledValue{canonical: {Default: "60"}},
	)

	kv := cfg.ValidateTenantKeys()

	if len(kv.Errors) != 0 {
		t.Fatalf("declared list must be canonicalized before comparison; "+
			"a retired spelling on the list silently un-declares the key: %v", kv.Errors)
	}
}

func TestDeclaredKey_TenantUsesRetiredSpellingOfDeclaredKey(t *testing.T) {
	t.Parallel()
	legacy, canonical := aliasUnderTest(t)
	overrides := map[string]ScheduledValue{}
	overrides[legacy] = ScheduledValue{Default: "60"}
	cfg := declaredCfg(
		[]string{canonical}, // platform listed the canonical name
		overrides,
	)

	kv := cfg.ValidateTenantKeys()

	if len(kv.Errors) != 0 {
		t.Fatalf("the rename window applies to declared keys too, got: %v", kv.Errors)
	}
	if len(kv.Notices) != 1 {
		t.Fatalf("want exactly 1 rename notice, got %d: %v", len(kv.Notices), kv.Notices)
	}
	for _, want := range []string{legacy, canonical} {
		if !strings.Contains(kv.Notices[0], want) {
			t.Errorf("notice should name %q, got %q", want, kv.Notices[0])
		}
	}
}

// resolveDimensionalRows is tenant-only — it never looks at defaults — so a
// dimensional override on a declared base resolves and emits immediately.
// Accepting it promises nothing the resolver won't deliver.
func TestDeclaredKey_DimensionalOverrideIsAccepted(t *testing.T) {
	t.Parallel()
	cfg := declaredCfg(
		[]string{"oracle_wait_time_rate"},
		map[string]ScheduledValue{`oracle_wait_time_rate{db="ORCL"}`: {Default: "5"}},
	)

	kv := cfg.ValidateTenantKeys()

	if len(kv.Errors) != 0 {
		t.Fatalf("dimensional override on a declared base must be accepted: %v", kv.Errors)
	}
}

// ⛔ The deliberate non-union. resolveCriticalRows (resolve.go) requires
// defaults[base] and drops the row with a scrape-time log line otherwise; a
// declared base is absent from defaults by definition. Accepting this key
// would produce exactly the failure the surrounding block comment forbids.
func TestDeclaredKey_CriticalTwinIsStillRefused(t *testing.T) {
	t.Parallel()
	cfg := declaredCfg(
		[]string{"oracle_wait_time_rate"},
		map[string]ScheduledValue{"oracle_wait_time_rate_critical": {Default: "9"}},
	)

	kv := cfg.ValidateTenantKeys() // #1231 c2: dangling _critical is a blocking Error

	if len(kv.Errors) != 1 {
		t.Fatalf("expected exactly 1 error, got %d: %v", len(kv.Errors), kv.Errors)
	}
	e := kv.Errors[0]
	// Name both keys AND the reason: "refused" is not actionable, "the base is
	// declared without a value, so the row would never emit" is.
	for _, want := range []string{
		"oracle_wait_time_rate_critical",
		"oracle_wait_time_rate",
		"optional_overrides",
		"resolveCriticalRows",
	} {
		if !strings.Contains(e, want) {
			t.Errorf("error should mention %q, got %q", want, e)
		}
	}
}

// ⛔ PREVENT #656. `expires:` reverts an override TO the platform default; a
// declared key has none, so a lapse leaves NO value and the alert goes quiet.
// Declared keys are the fourth member of the "nothing to fall back to" family
// that resolveCriticalRows and resolveDimensionalRows already refuse.
func TestDeclaredKey_ExpiresIsExplicitlyRefused(t *testing.T) {
	t.Parallel()
	cfg := declaredCfg(
		[]string{"oracle_wait_time_rate"},
		map[string]ScheduledValue{
			"oracle_wait_time_rate": {
				Default: "5",
				Expiry:  &ExpiryMeta{Expires: "2027-01-01T00:00:00Z", Reason: "tuning"},
			},
		},
	)

	kv := cfg.ValidateTenantKeys()

	if len(kv.Errors) != 1 {
		t.Fatalf("expected exactly 1 error, got %d: %v", len(kv.Errors), kv.Errors)
	}
	e := kv.Errors[0]
	for _, want := range []string{"oracle_wait_time_rate", "expires", "optional_overrides", "no default to revert to"} {
		if !strings.Contains(e, want) {
			t.Errorf("error should mention %q, got %q", want, e)
		}
	}
	// A well-formed RFC3339 instant must not be what saves it — the refusal is
	// about the key's tier, not the timestamp's syntax.
	if strings.Contains(e, "RFC3339") {
		t.Errorf("refusal must not read as a format complaint, got %q", e)
	}
}

// ⭐ Cross-function contract. The `expires:` scope predicate exists TWICE:
// ValidateTenantKeys (author-facing refusal) and ResolveThresholdExpiriesAt
// (which emits da_config_event{event="threshold_expired"}). They are two
// hand-copies of one rule and must agree — if membership ever widens in the
// validator alone, a declared key would become writable with an expiry that
// silently never fires an event. This pins them together.
func TestDeclaredKey_ExpiryRefusalAndEventSuppressionAgree(t *testing.T) {
	t.Parallel()
	cfg := declaredCfg(
		[]string{"oracle_wait_time_rate"},
		map[string]ScheduledValue{
			"oracle_wait_time_rate": {
				Default: "5",
				Expiry:  &ExpiryMeta{Expires: "2020-01-01T00:00:00Z"}, // already lapsed
			},
		},
	)

	if len(cfg.ValidateTenantKeys().Errors) == 0 {
		t.Fatal("validator must refuse expires on a declared key")
	}
	if got := cfg.ResolveThresholdExpiriesAt(time.Now()); len(got) != 0 {
		t.Fatalf("no expiry event may be emitted for a key the validator refuses, got %+v", got)
	}
}

// The other direction of the same contract, and the case that first broke it.
// Nothing forbids a key from being in BOTH sets — mergePartialInto only unions
// the list, and no schema or loader cross-checks it against defaults. A key
// that HAS a platform value has something to revert to no matter what else
// names it, so `expires:` must keep working and its event must keep firing.
//
// Checking the declared set first refused this override while telling the
// author "there is no default to revert to" about a key whose default is right
// there — and refused it while ResolveThresholdExpiriesAt (which gates on
// canonDefaults alone) still emitted the event. The refusal and the event have
// to move together in BOTH directions or the pair above is only half pinned.
func TestDeclaredKey_AlsoValuedKeepsExpiryWorkingAndEmitting(t *testing.T) {
	t.Parallel()
	cfg := &ThresholdConfig{
		Defaults:          map[string]float64{"mysql_connections": 80},
		OptionalOverrides: []string{"mysql_connections"}, // in BOTH sets
		Tenants: map[string]map[string]ScheduledValue{"t-1": {
			"mysql_connections": {
				Default: "200",
				Expiry:  &ExpiryMeta{Expires: "2020-01-01T00:00:00Z", Reason: "migration"},
			},
		}},
	}

	if errs := cfg.ValidateTenantKeys().Errors; len(errs) != 0 {
		t.Fatalf("a key that also has a platform value must keep its time-box: %v", errs)
	}
	if got := cfg.ResolveThresholdExpiriesAt(time.Now()); len(got) != 1 {
		t.Fatalf("the expiry event must still fire for a valued key, got %+v", got)
	}
}

// ⛔ Py↔Go parity pin for the shape that dominates the registry: 16 of the 25
// `tier: optional_overrides` keys end in `_critical`. Go's cascade reaches the
// _critical branch before the declared check and `continue`s out of both arms,
// so such a key never reaches membership widening — and must not, because
// resolveCriticalRows drops the row when the base has no value. The Python
// twin carries the mirrored guard; if either side moves alone, CI and the
// tenant-api write gate disagree about what is writable.
func TestDeclaredKey_CriticalKeyNamedOnTheListIsStillRefused(t *testing.T) {
	t.Parallel()
	cfg := declaredCfg(
		[]string{"jvm_memory_critical"}, // the _critical key itself is declared
		map[string]ScheduledValue{"jvm_memory_critical": {Default: "90"}},
	)

	kv := cfg.ValidateTenantKeys()

	if len(kv.Errors) != 1 {
		t.Fatalf("expected exactly 1 error, got %d: %v", len(kv.Errors), kv.Errors)
	}
	for _, want := range []string{"jvm_memory_critical", "jvm_memory"} {
		if !strings.Contains(kv.Errors[0], want) {
			t.Errorf("error should mention %q, got %q", want, kv.Errors[0])
		}
	}
}

// The declared list is a membership set, not an amnesty: everything about the
// old rejection path has to survive it.
func TestDeclaredKey_ListIsNotABlanketAmnesty(t *testing.T) {
	t.Parallel()
	cases := []struct {
		name     string
		key      string
		wantWord string
	}{
		// Never prefix-match — the package header forbids it for aliases and
		// the same trap applies here: a declared oracle_wait_time_rate must
		// not bless oracle_wait_time_rate_extra.
		{"prefix lookalike", "oracle_wait_time_rate_extra", "unknown key"},
		{"unrelated key", "postgres_nonsense", "unknown key"},
		{"typo reserved key", "_silence_mode", "unknown reserved key"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			cfg := declaredCfg(
				[]string{"oracle_wait_time_rate"},
				map[string]ScheduledValue{tc.key: {Default: "1"}},
			)

			kv := cfg.ValidateTenantKeys()

			if len(kv.Errors) != 1 {
				t.Fatalf("expected exactly 1 error, got %d: %v", len(kv.Errors), kv.Errors)
			}
			if !strings.Contains(kv.Errors[0], tc.wantWord) {
				t.Errorf("want %q in %q", tc.wantWord, kv.Errors[0])
			}
			if !strings.Contains(kv.Errors[0], tc.key) {
				t.Errorf("want the offending key %q named in %q", tc.key, kv.Errors[0])
			}
		})
	}
}

// An empty / absent list must leave every pre-existing verdict untouched —
// this is what makes the change inert until the platform actually declares
// something (no shipped _defaults.yaml does yet).
func TestDeclaredKey_EmptyListPreservesRejection(t *testing.T) {
	t.Parallel()
	overrides := map[string]ScheduledValue{"oracle_wait_time_rate": {Default: "5"}}

	withNil := declaredCfg(nil, overrides).ValidateTenantKeys()
	withEmpty := declaredCfg([]string{}, overrides).ValidateTenantKeys()

	if len(withNil.Errors) != 1 || len(withEmpty.Errors) != 1 {
		t.Fatalf("an undeclared key stays rejected: nil=%v empty=%v", withNil.Errors, withEmpty.Errors)
	}
	if withNil.Errors[0] != withEmpty.Errors[0] {
		t.Errorf("nil and empty list must behave identically:\n nil=%q\n empty=%q",
			withNil.Errors[0], withEmpty.Errors[0])
	}
}
