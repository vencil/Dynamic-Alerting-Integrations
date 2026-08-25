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
// Randomised sequences do not have to guess: 9 of 300 seeds diverged.
//
// The oracle is the only one that cannot drift: load the same directory from
// scratch and compare what a scrape would see.

import (
	"fmt"
	"os"
	"path/filepath"
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

			// Deterministic pseudo-randomness: `rand` is banned in this
			// package's fixtures, and a seed that reproduces by number is
			// worth more than entropy anyway.
			next := func(state *int, n int) int {
				*state = (*state*1103515245 + 12345) & 0x7fffffff
				return *state % n
			}
			state := seed*2654435761 + 1

			files := []string{"a.yaml", "b.yaml", "c.yaml"}
			// Round 0: a starting tree, then three reload rounds each doing an
			// arbitrary mix of add / edit / delete across those files.
			for _, f := range files[:1+next(&state, len(files)-1)] {
				writeTestYAML(t, filepath.Join(dir, f), tenantFileFor(f, next(&state, 90)+10))
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
						writeTestYAML(t, filepath.Join(dir, absent[next(&state, len(absent))]),
							tenantFileFor(absent[0], next(&state, 90)+10))
						edit := present[next(&state, len(present))]
						writeTestYAML(t, filepath.Join(dir, edit), tenantFileFor(edit, next(&state, 90)+10))
						touched = true
					}
				}
				for _, f := range files {
					switch next(&state, 4) {
					case 0: // leave it alone
					case 1, 2: // write (add or edit)
						writeTestYAML(t, filepath.Join(dir, f), tenantFileFor(f, next(&state, 90)+10))
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
			}

			reference := NewConfigManager(dir)
			if err := reference.Load(); err != nil {
				t.Fatalf("reference Load: %v", err)
			}
			if got, want := scrapeView(m.GetConfig()), scrapeView(reference.GetConfig()); got != want {
				t.Fatalf("the fast path published something a restart does not reproduce\n"+
					"--- incremental ---\n%s\n--- full load ---\n%s\n"+
					"tree now:\n%s", got, want, listTree(t, dir))
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
		body += fmt.Sprintf("  t-shared:\n    mysql_connections: \"%d\"\n", value+2)
	}
	return body
}

// scrapeView renders what a Prometheus scrape would see, order-independently.
func scrapeView(cfg *ThresholdConfig) string {
	rows := make([]string, 0)
	for _, r := range cfg.Resolve() {
		rows = append(rows, fmt.Sprintf("%s|%s_%s|%v|%s", r.Tenant, r.Component, r.Metric, r.Value, r.Severity))
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
