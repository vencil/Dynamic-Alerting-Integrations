package main

// A randomised differential between the incremental fast path and a full load
// of the same tree (#1569 blind review, round 5).
//
// ⛔ WHY RANDOMISED, WHEN THIS PR ALREADY HAS A HAND-WRITTEN EQUIVALENCE TABLE.
// `TestIncrementalLoadLandsWhereAFullLoadWould` enumerates mutations I thought
// of. It did not contain "one reload that BOTH adds a file and edits another",
// and that combination was hiding a defect that silently discarded the edit —
// `append(changed, added...)` aliased `changed`'s backing array and the sort
// after it reordered `changed` in place. Nine rounds of review, five sweeps and
// a hand-written matrix all missed it.
//
// ⛔ AND IT WAS INVISIBLE TO THE BUILDS YOU INVESTIGATE WITH. Under
// `-gcflags=all=-N -l` or `-race` the slice capacity works out differently and
// the alias does not form; so does adding a print statement. A hand-written
// test would have had to guess both the file-set shape AND run optimised.
//
// ⛔ THE FIRST VERSION OF THIS FILE WAS BARELY RANDOM AT ALL, and its own
// comment claimed "9 of 300 seeds diverged" — measured afterwards as **1**.
// The generator used an LCG and sampled `state % 4`; with that multiplier and
// increment both ≡ 1 (mod 4) the low bits are a COUNTER, so all 300 seeds
// collapsed to 4 distinct mutation scripts and only the values varied. A
// reviewer reproduced the stream. It now mixes with splitmix64, whose low bits
// are usable.
//
// ⛔ THE CATCH RATES, RE-MEASURED. Each production fix reverted one at a time,
// counting seeds that diverge:
//
//	append alias reinstated                       56 / 300  (was 1 / 300)
//	ApplyProfiles back inside the rebuild branch  298 / 300
//	reclaim reverted to last-file-wins            270 / 300
//	patch loop reverted to whole-map replace      270 / 300
//	declaration index built without sorting       163 / 300  (was 0 / 300)
//
// The last row is why `tenantFileFor` gives `a.yaml` a key that
// `_defaults.yaml` ALSO supplies: with disjoint keys the union's ordering is
// unfalsifiable, and the sort was unguarded.
//
// The oracle is the only one that cannot drift: load the same directory from
// scratch and compare what a scrape would see.

import (
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
)

func TestTheFastPathAlwaysLandsWhereAFullLoadWould(t *testing.T) {
	t.Parallel()
	const seeds = 300
	for seed := 0; seed < seeds; seed++ {
		seed := seed
		t.Run(fmt.Sprintf("seed=%d", seed), func(t *testing.T) {
			dir := t.TempDir()
			// ⛔ The platform file contributes a KEY of `t-shared`; `a.yaml`
			// contributes a different one. Whole-map-replace loses one of them,
			// per-key union keeps both — and only a tree with this shape can
			// tell those apart.
			writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"),
				"defaults:\n  mysql_connections: 80\n  mysql_slow_queries: 5\n  mysql_threads_running: 60\n"+
					"tenants:\n  t-shared:\n    _profile: gold\n    mysql_slow_queries: \"3\"\n")
			// ⛔ A PROFILE, because the fast path used to skip ApplyProfiles
			// entirely and no generated tree without one can see that.
			writeTestYAML(t, filepath.Join(dir, "_profiles.yaml"),
				"profiles:\n  gold:\n    mysql_threads_running: \"95\"\n")

			// Deterministic, but actually mixed: splitmix64. A seed that
			// reproduces by number is worth more than entropy — a stream whose
			// low bits are a counter is worth nothing.
			state := uint64(seed) + 0x9E3779B97F4A7C15
			next := func(_ *int, n int) int {
				state += 0x9E3779B97F4A7C15
				z := state
				z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9
				z = (z ^ (z >> 27)) * 0x94D049BB133111EB
				z ^= z >> 31
				return int(z % uint64(n))
			}
			var unused int

			files := []string{"a.yaml", "b.yaml", "c.yaml"}
			// Round 0: a starting tree, then three reload rounds each doing an
			// arbitrary mix of add / edit / delete across those files.
			for _, f := range files[:1+next(&unused, len(files)-1)] {
				writeTestYAML(t, filepath.Join(dir, f), tenantFileFor(f, next(&unused, 90)+10))
			}
			m := NewConfigManager(dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}

			for round := 0; round < 3; round++ {
				touched := false
				// ⛔ ONE ROUND IS FORCED TO BOTH ADD AND EDIT. That combination
				// is what `append(changed, added...)` aliasing needs, and a
				// purely random walk produced it too rarely to be a guard —
				// reverting the fix left all 300 seeds green until this was
				// forced. Which file plays which role still varies by seed.
				if round == 1 {
					var absent, present []string
					for _, f := range files {
						if _, err := os.Stat(filepath.Join(dir, f)); err == nil {
							present = append(present, f)
						} else {
							absent = append(absent, f)
						}
					}
					if len(absent) > 0 && len(present) > 0 {
						// ⛔ ONE INDEX, USED TWICE. This picked the filename at
						// random but built the CONTENT for `absent[0]`. It
						// survived only because the old counter-PRNG always
						// left exactly one absent file; with a real stream the
						// two diverge, two files declare the same tenant, and
						// the full-load oracle dies with DuplicateTenantError —
						// the test then fails on its own fixture while
						// reporting a config error, which reads as a product
						// defect. (#1569 blind review.)
						add := absent[next(&unused, len(absent))]
						writeTestYAML(t, filepath.Join(dir, add),
							tenantFileFor(add, next(&unused, 90)+10))
						edit := present[next(&unused, len(present))]
						writeTestYAML(t, filepath.Join(dir, edit), tenantFileFor(edit, next(&unused, 90)+10))
						touched = true
					}
				}
				for _, f := range files {
					switch next(&unused, 4) {
					case 0: // leave it alone
					case 1, 2: // write (add or edit)
						writeTestYAML(t, filepath.Join(dir, f), tenantFileFor(f, next(&unused, 90)+10))
						touched = true
					case 3: // delete if present
						if err := os.Remove(filepath.Join(dir, f)); err == nil {
							touched = true
						}
					}
				}
				if !touched {
					continue
				}
				if err := m.IncrementalLoad(); err != nil {
					t.Fatalf("round %d IncrementalLoad: %v", round, err)
				}

				// ⛔ COMPARED EVERY ROUND. Comparing only after the last one
				// misses a divergence that appears in round 1 and is masked
				// again in round 2 — and a fast path that self-heals is still
				// a fast path that served wrong data in between.
				reference := NewConfigManager(dir)
				if err := reference.Load(); err != nil {
					t.Fatalf("round %d reference Load: %v", round, err)
				}
				if got, want := scrapeView(m.GetConfig()), scrapeView(reference.GetConfig()); got != want {
					t.Fatalf("round %d: the fast path published something a restart does not reproduce\n"+
						"--- incremental ---\n%s\n--- full load ---\n%s\n"+
						"tree now:\n%s", round, got, want, listTree(t, dir))
				}
			}
		})
	}
}

// tenantFileFor gives each file a tenant of its own. `a.yaml` additionally
// contributes ONE key of the shared tenant that `_defaults.yaml` also
// contributes to — the legitimate two-sources-one-tenant shape.
//
// ⛔ THE SHARED TENANT IS WHY THIS CATCHES ANYTHING, and it has to come from a
// PLATFORM file. The first version had two ordinary tenant files declare the
// same tenant, so union-vs-replace was observable — but that tree is invalid:
// a full load rejects it outright with DuplicateTenantError, so all 300 seeds
// failed on the oracle instead of on the property. A platform file's `tenants:`
// block is exempt from that check and is a shape the loader documents.
//
// The first version before THAT gave every file its own tenant only, so no two
// sources ever met and reverting the per-key-union fix left all 300 seeds
// green. Same blind spot as every other fixture in this PR: narrower than
// production.
func tenantFileFor(name string, value int) string {
	own := "t-" + strings.TrimSuffix(name, ".yaml")
	body := fmt.Sprintf("tenants:\n  %s:\n    mysql_connections: \"%d\"\n    mysql_slow_queries: \"%d\"\n",
		own, value, value+1)
	if name == "a.yaml" {
		// ⛔ THE SECOND KEY IS THE ONE `_defaults.yaml` ALSO SUPPLIES, so the
		// two sources COMPETE for it and the merge order decides the winner.
		// Without a contested key the union's ordering is unfalsifiable:
		// deleting the sort from indexTenantDeclarations left all 300 seeds
		// green, because two sources contributing disjoint keys produce the
		// same map in either order. The full-load oracle pins which one wins.
		body += fmt.Sprintf("  t-shared:\n    mysql_connections: \"%d\"\n    mysql_slow_queries: \"%d\"\n",
			value+2, value+4)
	}
	return body
}

// sortedLabelPairs renders a label map order-independently; Go map formatting
// is sorted for string keys today, but relying on that in a differential is
// asking for a false divergence.
func sortedLabelPairs(m map[string]string) string {
	if len(m) == 0 {
		return "-"
	}
	pairs := make([]string, 0, len(m))
	for k, v := range m {
		pairs = append(pairs, k+"="+v)
	}
	sort.Strings(pairs)
	return strings.Join(pairs, ",")
}

// scrapeView renders everything a consumer can observe, order-independently.
//
// ⛔ THE FIRST VERSION COMPARED FOUR FIELDS OF ONE RESOLVER. It dropped
// `CustomLabels` / `RegexLabels` — so a dimensional override landing on the
// wrong label set was invisible — and never called the state-filter, silent-
// mode, dedup, routing or metadata resolvers at all, nor looked at
// `OptionalOverrides`. A differential is only as wide as the face it diffs.
func scrapeView(cfg *ThresholdConfig) string {
	rows := make([]string, 0)
	for _, r := range cfg.Resolve() {
		rows = append(rows, fmt.Sprintf("threshold|%s|%s_%s|%v|%s|custom=%v|regex=%v",
			r.Tenant, r.Component, r.Metric, r.Value, r.Severity,
			sortedLabelPairs(r.CustomLabels), sortedLabelPairs(r.RegexLabels)))
	}
	add := func(tag string, v any) {
		rv := reflect.ValueOf(v)
		for i := 0; i < rv.Len(); i++ {
			rows = append(rows, fmt.Sprintf("%s|%+v", tag, rv.Index(i).Interface()))
		}
	}
	add("state", cfg.ResolveStateFilters())
	add("silent", cfg.ResolveSilentModes())
	add("dedup", cfg.ResolveSeverityDedup())
	add("metadata", cfg.ResolveMetadata())
	add("routing", cfg.ResolveRouting())
	add("declared", cfg.OptionalOverrides)
	for tenant, ov := range cfg.Tenants {
		for k, sv := range ov {
			rows = append(rows, fmt.Sprintf("override|%s|%s|%+v", tenant, k, sv))
		}
	}
	sort.Strings(rows)
	return strings.Join(rows, "\n")
}

func listTree(t *testing.T, dir string) string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	if err != nil {
		return "(unreadable: " + err.Error() + ")"
	}
	var b strings.Builder
	for _, e := range entries {
		body, _ := os.ReadFile(filepath.Join(dir, e.Name()))
		fmt.Fprintf(&b, "--- %s ---\n%s", e.Name(), string(body))
	}
	return b.String()
}
