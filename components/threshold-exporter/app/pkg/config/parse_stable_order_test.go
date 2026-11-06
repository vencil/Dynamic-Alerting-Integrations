package config

// #3323: a mapping (or sequence) that is not a {default, overrides} value is
// decoded to interface{} and marshalled back to a string. yaml.v3 sorts the
// keys of that map, but its comparator ranks two time.Time keys as equal (same
// reflect kind, neither numeric nor string), so the order of those keys fell
// back to Go's randomised map iteration and served-values printed the same
// tree differently from run to run.
//
// Go randomises map iteration, so with two timestamp keys an unfixed
// re-serialisation picks the other order in roughly one run in seven (42 of
// 300 measured); 200 runs that all agree happen by chance about once in 10^13,
// so agreement is the evidence the order no longer comes from the map.

import (
	"testing"

	"gopkg.in/yaml.v3"
)

const stableOrderRuns = 200

func reserialiseOnce(t *testing.T, doc string) string {
	t.Helper()
	var sv ScheduledValue
	if err := yaml.Unmarshal([]byte(doc), &sv); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	return sv.Default
}

func assertSameEveryRun(t *testing.T, doc, want string) {
	t.Helper()
	for i := 0; i < stableOrderRuns; i++ {
		if got := reserialiseOnce(t, doc); got != want {
			t.Fatalf("run %d: re-serialisation differs\n--- got ---\n%s--- want ---\n%s", i, got, want)
		}
	}
}

// The fixture shape from the issue (tests/golden/fixtures/mixed-mode,
// tenant-keys `_x`): mixed scalar kinds plus two timestamp keys, one of them
// zoned. Timestamp keys are ordered by their YAML text; every other key keeps
// the order yaml.v3 already gave it (bool, number, string, then timestamps).
func TestScheduledValue_MappingWithTimestampKeys_StableOrder(t *testing.T) {
	t.Parallel()
	doc := "true: bool\n" +
		"1: float\n" +
		"\"010\": quoted\n" +
		"2026-12-31T10:20:30.5+08:00: zoned\n" +
		"2026-12-31T00:00:00Z:\n    from_tenant: 2\n"
	want := "true: bool\n" +
		"1: float\n" +
		"\"010\": quoted\n" +
		"2026-12-31T00:00:00Z:\n    from_tenant: 2\n" +
		"2026-12-31T10:20:30.5+08:00: zoned\n"
	assertSameEveryRun(t, doc, want)
}

// Several timestamp keys, and a nested mapping whose own timestamp keys tie:
// the fix has to reach every mapping in the tree, not just the outer one.
func TestScheduledValue_NestedTimestampKeys_StableOrder(t *testing.T) {
	t.Parallel()
	doc := "outer:\n" +
		"    2026-03-01T00:00:00Z: c\n" +
		"    2026-01-01T00:00:00Z: a\n" +
		"    2026-02-01T00:00:00Z: b\n" +
		"2027-01-01T00:00:00Z:\n" +
		"    2026-05-01T00:00:00Z: z\n" +
		"    2026-04-01T00:00:00Z: x\n" +
		"2025-01-01T00:00:00Z: first\n"
	want := "outer:\n" +
		"    2026-01-01T00:00:00Z: a\n" +
		"    2026-02-01T00:00:00Z: b\n" +
		"    2026-03-01T00:00:00Z: c\n" +
		"2025-01-01T00:00:00Z: first\n" +
		"2027-01-01T00:00:00Z:\n" +
		"    2026-04-01T00:00:00Z: x\n" +
		"    2026-05-01T00:00:00Z: z\n"
	assertSameEveryRun(t, doc, want)
}

// The sequence branch (_custom_alerts and other lists) re-serialises the same
// way, so a list item that is a mapping with timestamp keys must be stable too.
func TestScheduledValue_SequenceWithTimestampKeys_StableOrder(t *testing.T) {
	t.Parallel()
	doc := "- 2026-02-01T00:00:00Z: b\n" +
		"  2026-01-01T00:00:00Z: a\n"
	want := "- 2026-01-01T00:00:00Z: a\n" +
		"  2026-02-01T00:00:00Z: b\n"
	assertSameEveryRun(t, doc, want)
}

// Control: mappings yaml.v3 could already order must come out exactly as
// before — the fix changes how timestamp ties are broken and nothing else
// (quoting, indentation, the natural sort of string keys, number order).
func TestScheduledValue_ComparableKeys_OutputUnchanged(t *testing.T) {
	t.Parallel()
	cases := []struct{ name, doc, want string }{
		{
			"string keys use yaml.v3's natural sort",
			// `y` is quoted on the way out: yaml.v3 quotes YAML 1.1 booleans.
			"receiver: b\ngroup_by: [alertname]\nk10: x\nk9: y\n",
			"group_by:\n    - alertname\nk9: \"y\"\nk10: x\nreceiver: b\n",
		},
		{
			"strings that need quotes stay quoted",
			"a: \"010\"\nb: \"yes\"\nc: \"2026-01-01T00:00:00Z\"\nd: \"\"\n",
			"a: \"010\"\nb: \"yes\"\nc: \"2026-01-01T00:00:00Z\"\nd: \"\"\n",
		},
		{
			"numbers before strings, in numeric order",
			"b: s\n10: ten\n2: two\n",
			"2: two\n10: ten\nb: s\n",
		},
		{
			"a single timestamp key has nothing to tie with",
			"z: last\n2026-01-01T00:00:00Z: ts\n",
			"z: last\n2026-01-01T00:00:00Z: ts\n",
		},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			assertSameEveryRun(t, tc.doc, tc.want)
		})
	}
}

// Two timestamp keys can print the same text and still be two map keys:
// `Z` decodes to time.UTC and `+00:00` to a fixed zone, so the time.Time
// values differ while both render as 2026-01-01T00:00:00Z (and the duplicate-
// key check compares the source text, which differs). Key text alone ties
// them; the value's text breaks the tie.
func TestScheduledValue_TimestampKeysWithSameText_StableOrder(t *testing.T) {
	t.Parallel()
	doc := "2026-01-01T00:00:00+00:00: b\n2026-01-01T00:00:00Z: a\n"
	want := "2026-01-01T00:00:00Z: a\n2026-01-01T00:00:00Z: b\n"
	assertSameEveryRun(t, doc, want)
}
