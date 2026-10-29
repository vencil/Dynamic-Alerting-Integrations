package main

// Sweep B-1 of #1521 (PR #1569): the key-shape × resolver-entry-condition
// matrix.
//
// ⛔ WHY A MATRIX AND NOT MORE CASES. Rounds 1-5 each fixed the shapes a
// reviewer happened to name, and round 5's own fix was still wrong in both
// directions. The reason is structural: `keyBypassesTheDeclaredSurface` was
// written by reading the resolvers and remembering what they key off, and a
// remembered predicate drifts from the real one exactly the way the two
// conf.d scanners drifted (PR #1343; family #1911). So this file stops
// enumerating cases and enumerates the
// SPACE: every shape `applySubtreeDefaults` can write, crossed against the
// promise the overlay makes about all of them.
//
// The promise, stated once: an inherited key is written into a tenant's map
// ONLY if something downstream reads it; otherwise it is named in the
// divergence report. The one licensed third outcome is a key whose consumer
// WARNs on its own (`_critical` with no base default, an unparseable
// dimensional key) — loud through a different channel, so the gate does not
// repeat it. A `_` key other than a routing key is not carried by a subtree
// at all (#3341): never written, and named by da-guard's
// subtree_default_reserved_key error instead (verdictNotCarried). A routing
// key is not written and is left to the route generator's checks, as da-guard
// leaves it (verdictRouteGenerator, #3379).
//
// ⛔ WHAT THIS CATCHES THAT A PER-SHAPE TEST CANNOT. `verdictSilent` is not a
// case anyone writes on purpose; it is what a NEW bypass arm produces when its
// consumer has a condition the arm does not mirror. Adding `_foo_` to the
// bypass list and no consumer fails this test with the shape named.

import (
	"bytes"
	"fmt"
	"log"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

type reachVerdict int

const (
	// verdictDelivered: written into the tenant map AND changes resolver output.
	verdictDelivered reachVerdict = iota
	// verdictRefused: not written, and NAMED in the divergence report.
	verdictRefused
	// verdictWarned: written, changes nothing, but the consumer logs its own WARN.
	verdictWarned
	// verdictInert: written, changes nothing, nobody warns — licensed ONLY where
	// the value is semantically identical to the key's absence.
	verdictInert
	// verdictNotCarried: a key a subtree does not carry (#3341) — not
	// written, whatever the value, and named by da-guard's
	// subtree_default_reserved_key (config.ScopedTenants.SubtreeReserved).
	verdictNotCarried
	// verdictRouteGenerator: a routing key (config.IsRoutingKey) — not
	// written, and left to the route generator's checks (`--validate
	// --strict`), as da-guard leaves it (#3379): neither the exporter's
	// undeliverable set nor subtree_default_reserved_key names it.
	verdictRouteGenerator
	// verdictSilent: written, changes nothing, nobody warns, not the inert case.
	// Never licensed. Present so the table can say so out loud.
	verdictSilent
)

func (v reachVerdict) String() string {
	switch v {
	case verdictDelivered:
		return "delivered"
	case verdictRefused:
		return "refused+named"
	case verdictWarned:
		return "written, consumer WARNs"
	case verdictInert:
		return "written, inert (== absent)"
	case verdictNotCarried:
		return "not carried, named by subtree_default_reserved_key"
	case verdictRouteGenerator:
		return "not written, a routing key the route generator judges"
	default:
		return "SILENT (written, no reader, no report)"
	}
}

// reachMatrixRoot declares one platform default with a `_critical`-able base,
// a second default, and one state filter — the smallest root that lets every
// shape below be exercised against a REAL entry condition rather than an
// empty one.
//
// ⚠️ The two alias rows live in `config_subtree_alias_test.go`, not here.
// Their fixtures need the RETIRED spelling as a live value, and the #1231
// pre-commit hook forbids that outside one named file — keeping the exemption
// to one file is worth more than keeping the table in one place.
const reachMatrixRoot = "defaults:\n  mysql_connections: 80\n  redis_evicted_keys: 10\n" +
	"state_filters:\n  maintenance:\n    severity: warning\n"

func TestEveryInheritedKeyShapeIsDeliveredOrNamed(t *testing.T) {
	// Not parallel: swaps the process-global log sink (same reason
	// main_test.go's configDir cases are sequential).
	cases := []struct {
		name string
		line string // one `defaults:` entry in the SUBTREE _defaults.yaml
		key  string
		want reachVerdict
		// root overrides reachMatrixRoot for this case. Needed only where the
		// shared root cannot express the fixture: the repo has exactly ONE
		// deprecated alias, so a legacy key cannot be a platform default and a
		// declared-only key in the same tree.
		root    string
		because string
	}{
		{"a root default", "  redis_evicted_keys: 60\n", "redis_evicted_keys", verdictDelivered, "",
			"resolveBaseRows walks cfg.Defaults, which carries this key"},
		{"a key no plane declares", "  no_such_metric: 60\n", "no_such_metric", verdictRefused, "",
			"in neither cfg.Defaults nor cfg.OptionalOverrides — nothing iterates it"},
		{"a dimensional override", "  redis_evicted_keys{db=\"1\"}: 5\n", "redis_evicted_keys{db=\"1\"}", verdictDelivered, "",
			"resolveDimensionalRows is tenant-only and needs no declaration"},
		{"a dimensional key that cannot be parsed", "  redis_evicted_keys{oops: 5\n", "redis_evicted_keys{oops", verdictWarned, "",
			"resolveDimensionalRows logs `failed to parse dimensional key` itself"},
		{"a critical tier over a root default", "  mysql_connections_critical: 200\n", "mysql_connections_critical", verdictDelivered, "",
			"resolveCriticalRows finds mysql_connections in defaults"},
		{"a critical tier with no base", "  postgres_locks_critical: 200\n", "postgres_locks_critical", verdictRefused, "",
			"resolveCriticalRows serves a critical row only over a base cfg.Defaults values; the overlay asks that same condition (criticalBaseValued, #3378) — before, it handed the key down and the row was dropped with a WARN no report named"},
		{"a critical tier over a declared-only base", "  postgres_locks_critical: 200\n", "postgres_locks_critical", verdictRefused,
			"defaults:\n  mysql_connections: 80\noptional_overrides: [postgres_locks]\nstate_filters:\n  maintenance:\n    severity: warning\n",
			"`optional_overrides:` declares the base without a value, and resolveCriticalRows needs the value (#3378)"},
		// #3341: until then these were delivered (`_state_maintenance`,
		// `_severity_dedup`), inert (`_silent_mode`) or refused as
		// unreachable; a subtree now hands none of them down.
		{"a state filter the platform declares", "  _state_maintenance: disable\n", "_state_maintenance", verdictNotCarried, "",
			"a subtree carries no reserved key (#3341); set it in the tenant's entry"},
		{"a state filter the platform never declared", "  _state_no_such_filter: disable\n", "_state_no_such_filter", verdictNotCarried, "",
			"a subtree carries no reserved key (#3341)"},
		{"severity dedup turned off", "  _severity_dedup: disable\n", "_severity_dedup", verdictNotCarried, "",
			"a subtree carries no reserved key (#3341)"},
		{"silent mode turned off", "  _silent_mode: disable\n", "_silent_mode", verdictNotCarried, "",
			"a subtree carries no reserved key (#3341)"},
		{"junk under the silent prefix", "  _silent_bogus: disable\n", "_silent_bogus", verdictNotCarried, "",
			"a subtree carries no `_` key but a routing key (#3341)"},
		{"junk under the routing prefix", "  _routing_bogus: 5\n", "_routing_bogus", verdictRouteGenerator, "",
			"ResolveRouting reads the exact key _routing, and `_routing` is a valid reserved PREFIX so nothing warns"},

		// ⛔ THE OVERLAPPING SHAPES. Each of these matches TWO arms of
		// `keyBypassesTheDeclaredSurface`, and a `switch` takes the first. The
		// first version of that predicate put the two unconditional arms first,
		// so every shape here reached a `return true` without ever consulting
		// the consumer — the arm-order blind spot of the very table that was
		// supposed to be exhaustive. All four were measured silent then:
		// written, no reader, no report, no WARN. Since #3341 only the
		// routing rows of this block reach the predicate at all.
		{"a critical tier under an undeclared state filter", "  _state_bogus_critical: 5\n", "_state_bogus_critical", verdictNotCarried, "",
			"a subtree carries no reserved key (#3341); before, resolveCriticalRows' own `_state_` skip made it refused"},
		{"a dimensional key under an undeclared state filter", "  _state_bogus{env=\"x\"}: 5\n", "_state_bogus{env=\"x\"}", verdictNotCarried, "",
			"a subtree carries no reserved key (#3341)"},
		{"a dimensional key under the routing prefix", "  _routing_bogus{env=\"x\"}: 5\n", "_routing_bogus{env=\"x\"}", verdictRouteGenerator, "",
			"resolveDimensionalRows skips `_routing`-prefixed keys itself"},
		{"a dimensional key under the silent prefix", "  _silent_bogus{env=\"x\"}: 5\n", "_silent_bogus{env=\"x\"}", verdictNotCarried, "",
			"a subtree carries no `_` key but a routing key (#3341)"},
		{"a critical tier under the silent prefix", "  _silent_bogus_critical: 5\n", "_silent_bogus_critical", verdictNotCarried, "",
			"a subtree carries no `_` key but a routing key (#3341)"},
		{"a junk value on the dedup key", "  _severity_dedup: 5\n", "_severity_dedup", verdictNotCarried, "",
			"a subtree carries no reserved key (#3341); before, ResolveSeverityDedup WARNed on it"},
		{"a state filter carrying a time window", "  _state_maintenance:\n    default: disable\n    overrides:\n      - window: \"00:00-23:59\"\n        value: enable\n", "_state_maintenance", verdictNotCarried, "",
			"a subtree carries no reserved key, windowed or not (#3341)"},
		{"a critical tier under the routing prefix", "  _routing_bogus_critical: 5\n", "_routing_bogus_critical", verdictRouteGenerator, "",
			"resolveCriticalRows reads no `_` key (criticalRowBase, #3045), so the overlay asks no critical condition of it (#3378) and refuses it as any undeclared routing key, which the route generator reports (#3379)"},
		{"a critical tier on the dedup key", "  _severity_dedup_critical: 5\n", "_severity_dedup_critical", verdictNotCarried, "",
			"a subtree carries no `_` key but a routing key (#3341); before, resolveCriticalRows read it and WARNed"},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			root := tc.root
			if root == "" {
				root = reachMatrixRoot
			}
			got, ev := classifyInheritedKey(t, root, tc.line, tc.key)
			if got != tc.want {
				t.Fatalf("%s\nkey %q: got %v, want %v (%s)\n%s",
					tc.name, tc.key, got, tc.want, tc.because, ev)
			}
		})
	}
}

// classifyInheritedKey builds a two-level tree carrying `line` in the subtree
// defaults file, and reports which of the five outcomes the key produced.
// `ev` is the evidence string, printed only on failure.
func classifyInheritedKey(t *testing.T, root, line, key string) (reachVerdict, string) {
	t.Helper()

	var logBuf bytes.Buffer
	origOut, origFlags := log.Writer(), log.Flags()
	log.SetOutput(&logBuf)
	log.SetFlags(0)
	t.Cleanup(func() { log.SetOutput(origOut); log.SetFlags(origFlags) })

	with, written, reported, named := loadReachTree(t, root, line, key)
	without, _, _, _ := loadReachTree(t, root, "", key)
	observable := resolverFingerprint(with) != resolverFingerprint(without)

	// A WARN naming this key, from a consumer rather than from the divergence
	// audit. The audit's own line is an ERROR, so a substring test on WARN
	// lines cannot be satisfied by the report we are trying to distinguish from.
	// ⛔ NOT EVERY WARN SPELLS THE KEY THE WAY THE CONFIG DOES.
	// `ResolveSeverityDedup` logs `unknown severity_dedup value …` — no
	// leading underscore — so a literal match on `_severity_dedup` reported
	// `warned=false` while that exact WARN sat in the captured log, and the
	// classifier called an inert-and-warned shape SILENT. Matching the
	// underscore-stripped spelling too costs nothing here (a WARN naming
	// `severity_dedup` is about that key either way) and removes a
	// false SILENT verdict, which is the one verdict this file treats as a
	// defect. (#1569 blind review.)
	warned := false
	for _, ln := range strings.Split(logBuf.String(), "\n") {
		if !strings.Contains(ln, "WARN") {
			continue
		}
		if strings.Contains(ln, key) || strings.Contains(ln, strings.TrimPrefix(key, "_")) {
			warned = true
			break
		}
	}

	ev := fmt.Sprintf("evidence: written=%v observable=%v reported=%v warned=%v named=%v\n--- log ---\n%s",
		written, observable, reported, warned, named, logBuf.String())

	switch {
	case !written && !reported && named:
		return verdictNotCarried, ev
	case !written && !reported && !named && config.IsRoutingKey(key):
		return verdictRouteGenerator, ev
	case written && observable:
		return verdictDelivered, ev
	case !written && reported:
		return verdictRefused, ev
	case written && warned:
		return verdictWarned, ev
	case written && key == "_silent_mode":
		// The single licensed inert shape, named explicitly so a second one
		// cannot slip in under the same branch.
		return verdictInert, ev
	default:
		return verdictSilent, ev
	}
}

func loadReachTree(t *testing.T, root, line, key string) (cfg *ThresholdConfig, written, reported, named bool) {
	t.Helper()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), root)
	mkSub(t, dir, "finance")
	writeTestYAML(t, filepath.Join(dir, "finance", "_defaults.yaml"),
		"defaults:\n  mysql_connections: 60\n"+line)
	writeTestYAML(t, filepath.Join(dir, "finance", "t1.yaml"), "tenants:\n  t1: {}\n")

	m := NewConfigManager(dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	cfg = m.GetConfig()
	_, written = cfg.Tenants["t1"][key]

	m.mu.RLock()
	unreachable := m.hierarchy.unreachableInherited
	m.mu.RUnlock()
	for _, keys := range unreachable {
		for _, k := range keys {
			if k == key {
				reported = true
			}
		}
	}
	scoped, err := config.ScopeEffective(dir, "", fixedNow)
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	_, named = scoped.SubtreeReserved["t1"][key]
	return cfg, written, reported, named
}

// resolverFingerprint is every resolver that reads a tenant's override map,
// rendered order-independently.
//
// ⛔ SORTED, BECAUSE THE RESOLVERS ARE NOT. Every one of them walks
// `c.Tenants` (a map) so row order is Go's randomised iteration order. A first
// draft of this helper compared the slices verbatim and reported a control
// tree as "observable" against itself.
func resolverFingerprint(cfg *ThresholdConfig) string {
	now := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	var all []string
	add := func(tag string, rows any) {
		rv := reflect.ValueOf(rows)
		for i := 0; i < rv.Len(); i++ {
			all = append(all, fmt.Sprintf("%s|%+v", tag, rv.Index(i).Interface()))
		}
	}
	add("threshold", cfg.ResolveAt(now))
	add("state", cfg.ResolveStateFiltersAt(now))
	add("silent", cfg.ResolveSilentModesAt(now))
	add("maintenance-expiry", cfg.ResolveMaintenanceExpiriesAt(now))
	add("threshold-expiry", cfg.ResolveThresholdExpiriesAt(now))
	add("dedup", cfg.ResolveSeverityDedup())
	add("metadata", cfg.ResolveMetadata())
	add("routing", cfg.ResolveRouting())
	sort.Strings(all)
	return strings.Join(all, "\n")
}
