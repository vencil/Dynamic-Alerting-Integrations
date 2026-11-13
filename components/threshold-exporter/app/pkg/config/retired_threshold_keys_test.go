package config

// retired_threshold_keys_test.go — the lookup over the registry's
// `retired_keys:` (#3829), on fixture tables (the committed registry
// retires no key yet), and the generated table pinned to the registry.

import (
	"reflect"
	"sort"
	"strings"
	"testing"
)

var retiredFixtureTable = []retiredThresholdKey{
	{"fixture_old_conns", "fixture_new_conns", 3829, "fixture hint"},
	{"fixture_gone", "", 3829, "nothing replaces it"},
}

func TestRetiredThresholdIn_MatchesTheHeadExactly(t *testing.T) {
	t.Parallel()
	for key, want := range map[string]bool{
		"fixture_old_conns":                        true,
		`fixture_old_conns{db="a"}`:                true,
		"fixture_old_conns_critical":               true, // derived, as aliasHead derives it
		`fixture_old_conns_critical{d="a"}`:        true,
		"fixture_old_conns_critical_x":             false,
		"fixture_old_conns_x":                      false,
		"fixture_old":                              false,
		"fixture_new_conns":                        false,
		`fixture_new_conns{fixture_old_conns="x"}`: false,
	} {
		if _, got := retiredThresholdIn(retiredFixtureTable, key); got != want {
			t.Errorf("%q: retired=%v, want %v", key, got, want)
		}
	}
}

// #3829 review: closing an alias window moves `<old>` here; `<old>_critical`,
// which the alias canonicalized as aliasHead does, must be retired with it —
// naming `<replaced_by>_critical`, or nothing when nothing replaces the base.
// An entry for the `_critical` key itself wins.
func TestRetiredThresholdIn_CarriesABaseToItsCriticalTier(t *testing.T) {
	t.Parallel()
	r, ok := retiredThresholdIn(retiredFixtureTable, "fixture_old_conns_critical")
	if !ok || r.key != "fixture_old_conns_critical" || r.replacedBy != "fixture_new_conns_critical" {
		t.Fatalf("derived %+v %v", r, ok)
	}
	if r, ok := retiredThresholdIn(retiredFixtureTable, "fixture_gone_critical"); !ok || r.replacedBy != "" {
		t.Fatalf("derived without replacement %+v %v", r, ok)
	}
	own := append([]retiredThresholdKey{{"fixture_old_conns_critical", "other_key", 7, "own"}}, retiredFixtureTable...)
	if r, _ := retiredThresholdIn(own, "fixture_old_conns_critical"); r.replacedBy != "other_key" || r.tracking != 7 {
		t.Fatalf("own entry lost to the derived one: %+v", r)
	}
	// Every alias shape canonicalKeyFor maps is a shape this lookup retires
	// once the alias's old spelling moves here, naming the alias's target.
	for legacy, canon := range deprecatedKeyAliases {
		table := []retiredThresholdKey{{legacy, canon, 1, "h"}}
		for _, k := range []string{legacy, legacy + criticalSuffix, legacy + `{a="b"}`, legacy + criticalSuffix + `{a="b"}`} {
			c, aliased := canonicalKeyFor(k)
			r, ok := retiredThresholdIn(table, k)
			if !aliased || !ok {
				t.Errorf("%q: aliased=%v retired=%v", k, aliased, ok)
				continue
			}
			if head, _ := splitKeyHead(c); r.replacedBy != head {
				t.Errorf("%q: replaced by %q, the alias maps it to %q", k, r.replacedBy, head)
			}
		}
	}
}

func TestRetiredThresholdText_NamesTheReplacement(t *testing.T) {
	t.Parallel()
	r, _ := retiredThresholdIn(retiredFixtureTable, "fixture_old_conns")
	if got := r.text(); !strings.Contains(got, "`fixture_new_conns`") || !strings.Contains(got, "#3829") ||
		!strings.Contains(got, "fixture hint") {
		t.Fatalf("text %q", got)
	}
	r, _ = retiredThresholdIn(retiredFixtureTable, "fixture_gone")
	if got := r.text(); !strings.Contains(got, "no replacement") || !strings.Contains(got, "nothing replaces it") {
		t.Fatalf("text %q", got)
	}
}

// A retired threshold key is unsettable through a batch op: while it is on
// disk the key verdict refuses the tenant's writes.
func TestBatchUnsettable_AdmitsEveryRetiredThresholdKey(t *testing.T) {
	t.Parallel()
	reserved := []string{"_routing"}
	for key, want := range map[string]bool{
		"_routing":                   true,
		"fixture_old_conns":          true,
		`fixture_old_conns{db="a"}`:  true,
		"fixture_gone":               true,
		"fixture_new_conns":          false,
		"fixture_old_conns_critical": true,
		"fixture_new_conns_critical": false,
	} {
		if got := batchUnsettableIn(reserved, retiredFixtureTable, key); got != want {
			t.Errorf("%q: unsettable=%v, want %v", key, got, want)
		}
	}
	got := batchUnsettableKeysOf(reserved, retiredFixtureTable)
	want := []string{"_routing", "fixture_gone", "fixture_old_conns"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("keys %v, want %v", got, want)
	}
	// The shipped answer is the same rule over the shipped tables.
	if !reflect.DeepEqual(BatchUnsettableKeys(), batchUnsettableKeysOf(batchUnsettableKeys, retiredThresholdKeys)) {
		t.Fatal("BatchUnsettableKeys is not batchUnsettableKeysOf the shipped tables")
	}
}

func TestParseRetiredFixture(t *testing.T) {
	t.Parallel()
	got := parseRetiredFixture("a_old:a_new:12,b_old::34")
	want := []retiredThresholdKey{{"a_old", "a_new", 12, "test fixture entry"}, {"b_old", "", 34, "test fixture entry"}}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %+v", got)
	}
	if parseRetiredFixture("") != nil {
		t.Fatal("empty fixture adds entries")
	}
	for _, bad := range []string{"a_old:a_new", "a_old:a_new:x", ":a_new:1", "a_old:a_new:0"} {
		func() {
			defer func() {
				if recover() == nil {
					t.Errorf("%q: no panic", bad)
				}
			}()
			parseRetiredFixture(bad)
		}()
	}
}

// TestRetiredThresholdKeysPinnedToRegistry: the generated table equals the
// registry's `retired_keys:` (check_threshold_registry.py --ci checks the
// file's bytes; this checks what the package reads, a hand edit included).
func TestRetiredThresholdKeysPinnedToRegistry(t *testing.T) {
	t.Parallel()
	doc := loadRegistryDoc(t)
	var want []retiredThresholdKey
	for k, e := range doc.RetiredKeys {
		r := retiredThresholdKey{key: k, tracking: e.Tracking, hint: e.Hint}
		if e.ReplacedBy != nil {
			r.replacedBy = *e.ReplacedBy
		}
		want = append(want, r)
	}
	got := append([]retiredThresholdKey(nil), retiredThresholdKeys...)
	for _, s := range [][]retiredThresholdKey{want, got} {
		sort.Slice(s, func(i, j int) bool { return s[i].key < s[j].key })
	}
	if len(want) == 0 && len(got) == 0 {
		return
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("retiredThresholdKeys (generated) != registry retired_keys\n  Go      : %+v\n  registry: %+v\n"+
			"fix: python3 scripts/tools/lint/check_threshold_registry.py --regen", got, want)
	}
}
