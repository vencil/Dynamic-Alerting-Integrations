package main

// #3379: the exporter's undeliverable ERROR and gauge read the set da-guard's
// subtree_default_undeliverable reads (config.FlatBuild.Undeliverable), and a
// subtree `disable` of a key the root never declares is one WARN, not that
// ERROR (config.FlatBuild.DisabledUndeclared).
//
// ⛔ THE PARITY IS THE POINT. Each fixture below is loaded by a real
// ConfigManager (gauge, log, /metrics) and judged by da-guard's own path
// (config.ScopeEffective → guard.CheckDefaultsImpact, the inputs
// cmd/da-guard's buildCheckInput hands it for this finding), and the two
// tenant sets must be equal. Before #3379 the exporter read the unfiltered
// set: `redis_evicted_keys: disable` under a root that never declares it
// counted 1 and logged the ERROR while da-guard reported nothing, and a
// subtree `_routing_bogus: 5` did the same.
//
// The da-guard binary's exit codes for the same shapes are pinned in
// cmd/da-guard (TestUndeliverable_DisableAndRoutingShapes).
//
// Seams: metrics and logger through newAuditedManager; no global swap.

import (
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// disabledUndeclaredAnchor identifies warnSubtreeDisabledUndeclared's line.
const disabledUndeclaredAnchor = "conf.d subtree default disables an undeclared key"

// guardUndeliverableTenants is da-guard's subtree_default_undeliverable
// verdict on dir: the sorted tenants it reports. nil when none.
func guardUndeliverableTenants(t *testing.T, dir string) []string {
	t.Helper()
	scoped, err := config.ScopeEffective(dir, "", fixedNow)
	if err != nil {
		t.Fatalf("ScopeEffective: %v", err)
	}
	effective := make(map[string]map[string]any, len(scoped.Tenants))
	for _, ec := range scoped.Tenants {
		effective[ec.TenantID] = ec.MergedConfig
	}
	rep, err := guard.CheckDefaultsImpact(guard.CheckInput{
		EffectiveConfigs:       effective,
		UndeliverableInherited: scoped.Undeliverable,
	})
	if err != nil {
		t.Fatalf("CheckDefaultsImpact: %v", err)
	}
	seen := map[string]bool{}
	var out []string
	for _, f := range rep.Findings {
		if f.Kind != findingkind.FindingSubtreeDefaultUndeliverable || seen[f.TenantID] {
			continue
		}
		if f.Severity != guard.SeverityError {
			t.Errorf("subtree_default_undeliverable for %s is %v, want error", f.TenantID, f.Severity)
		}
		seen[f.TenantID] = true
		out = append(out, f.TenantID)
	}
	sort.Strings(out)
	return out
}

func TestSubtreeUndeliverable_ExporterAndGuardReadOneSet(t *testing.T) {
	t.Parallel()
	const root = "defaults:\n  mysql_connections: 80\n"
	fin := func(rootBody, sub string) map[string]string {
		return map[string]string{
			"_defaults.yaml":         rootBody,
			"finance/_defaults.yaml": sub,
			"finance/t-fin.yaml":     "tenants:\n  t-fin: {}\n",
			"ops/t-ops.yaml":         "tenants:\n  t-ops: {}\n",
		}
	}
	for _, tc := range []struct {
		name  string
		files map[string]string
		// wantUndeliverable: the tenants the gauge counts, the ERROR names
		// and da-guard reports (one set). nil = none.
		wantUndeliverable []string
		// wantWarned: the tenants the WARN names. nil = no WARN line.
		wantWarned []string
		// noSeries: tenants that must serve no redis_evicted_keys series.
		noSeries []string
	}{
		{
			name:       "A1 disable, root undeclared",
			files:      fin(root, "defaults:\n  redis_evicted_keys: disable\n"),
			wantWarned: []string{"t-fin"},
			noSeries:   []string{"t-fin", "t-ops"},
		},
		{
			// Numeric in one window: a row would be served then, so the
			// root's declaration is the fix.
			name: "A2 schedule disabled in part of the day",
			files: fin(root, "defaults:\n  redis_evicted_keys:\n    default: disable\n"+
				"    overrides:\n      - window: \"01:00-02:00\"\n        value: 50\n"),
			wantUndeliverable: []string{"t-fin"},
		},
		{
			name: "A2b schedule disabled all day",
			files: fin(root, "defaults:\n  redis_evicted_keys:\n    default: disable\n"+
				"    overrides:\n      - window: \"01:00-02:00\"\n        value: disable\n"),
			wantWarned: []string{"t-fin"},
			noSeries:   []string{"t-fin"},
		},
		{
			// The control A1 is measured against: the same /metrics, and
			// nothing to say.
			name:     "A3 root declares, subtree disables",
			files:    fin(root+"  redis_evicted_keys: 500\n", "defaults:\n  redis_evicted_keys: disable\n"),
			noSeries: []string{"t-fin"},
		},
		{
			// finance/ hands down 100 (undeliverable for t-fin); finance/us/
			// switches it off for t-fin-us, which is the WARN's, not the
			// gauge's (it counted 2 before #3379).
			name: "A8 value above, disable below",
			files: map[string]string{
				"_defaults.yaml":            root,
				"finance/_defaults.yaml":    "defaults:\n  redis_evicted_keys: 100\n",
				"finance/t-fin.yaml":        "tenants:\n  t-fin: {}\n",
				"finance/us/_defaults.yaml": "defaults:\n  redis_evicted_keys: disable\n",
				"finance/us/t-fin-us.yaml":  "tenants:\n  t-fin-us: {}\n",
			},
			wantUndeliverable: []string{"t-fin"},
			wantWarned:        []string{"t-fin-us"},
			noSeries:          []string{"t-fin", "t-fin-us"},
		},
		{
			name:              "B1 value, root undeclared",
			files:             fin(root, "defaults:\n  redis_evicted_keys: 100\n"),
			wantUndeliverable: []string{"t-fin"},
			noSeries:          []string{"t-fin"},
		},
		{
			// The route generator's `--validate --strict` reports it.
			name:  "routing key",
			files: fin(root, "defaults:\n  _routing_bogus: 5\n"),
		},
		{
			name:  "routing key disabled",
			files: fin(root, "defaults:\n  _routing_bogus: disable\n"),
		},
		{
			// Not carried (#3341): subtree_default_reserved_key's.
			name:  "unregistered underscore key",
			files: fin(root, "defaults:\n  _myth: 5\n"),
		},
		{
			name:  "silent mode disabled",
			files: fin(root, "defaults:\n  _silent_mode: disable\n"),
		},
		{
			// #3378: a subtree `_critical` over a base the root only
			// declares (no value) serves no critical row.
			name: "critical over a declared-only base",
			files: fin("defaults:\n  mysql_connections: 80\noptional_overrides:\n  - redis_evicted_keys\n",
				"defaults:\n  redis_evicted_keys_critical: 200\n"),
			wantUndeliverable: []string{"t-fin"},
		},
		{
			// The same base switched off as a `_critical`: the WARN's
			// wording covers it (the root declares the base, but values
			// neither the base nor the key).
			name: "critical disabled over a declared-only base",
			files: fin("defaults:\n  mysql_connections: 80\noptional_overrides:\n  - redis_evicted_keys\n",
				"defaults:\n  redis_evicted_keys_critical: disable\n"),
			wantWarned: []string{"t-fin"},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			for name, body := range tc.files {
				p := filepath.Join(dir, filepath.FromSlash(name))
				mkSub(t, dir, filepath.Dir(name))
				writeTestYAML(t, p, body)
			}
			m, fresh, logBuf := newAuditedManager(t, dir)
			if err := m.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			logs := logBuf.String()

			// The exporter's tenant set: the audit's own function over the
			// pair it is handed.
			m.mu.RLock()
			exporter := subtreeUndeliverableTenants(m.hierarchy.tenantSources, m.hierarchy.unreachableInherited)
			m.mu.RUnlock()
			if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); int(got) != len(exporter) {
				t.Errorf("gauge = %v, but the audit's set is %v", got, exporter)
			}
			if !reflect.DeepEqual(exporter, tc.wantUndeliverable) {
				t.Errorf("exporter undeliverable tenants = %v, want %v", exporter, tc.wantUndeliverable)
			}
			if g := guardUndeliverableTenants(t, dir); !reflect.DeepEqual(g, exporter) {
				t.Errorf("ONE SET: da-guard reports %v, the exporter counts %v", g, exporter)
			}

			errLines := logLinesWith(logs, undeliverableAnchor)
			switch {
			case tc.wantUndeliverable == nil && len(errLines) > 0:
				t.Errorf("undeliverable ERROR on a tree with none:\n%s", logs)
			case tc.wantUndeliverable != nil:
				if len(errLines) != 1 || !strings.HasPrefix(errLines[0], "ERROR:") {
					t.Fatalf("want one undeliverable ERROR line, got %q", errLines)
				}
				for _, tid := range tc.wantUndeliverable {
					assertLineSays(t, errLines[0], undeliverableAnchor, tid)
				}
			}

			warnLines := logLinesWith(logs, disabledUndeclaredAnchor)
			switch {
			case tc.wantWarned == nil && len(warnLines) > 0:
				t.Errorf("disabled-undeclared WARN with nothing to warn of:\n%s", logs)
			case tc.wantWarned != nil:
				if len(warnLines) != 1 || !strings.HasPrefix(warnLines[0], "WARN:") {
					t.Fatalf("want one WARN line, got %q", warnLines)
				}
				assertLineSays(t, warnLines[0], disabledUndeclaredAnchor,
					append([]string{"redis_evicted_keys", "changes no output", "misspelled",
						"gives no value to serve", "neither the base nor the key"}, tc.wantWarned...)...)
				for _, tid := range tc.wantUndeliverable {
					if strings.Contains(warnLines[0], tid+" (") {
						t.Errorf("the WARN names undeliverable tenant %s: %s", tid, warnLines[0])
					}
				}
			}

			for _, tid := range tc.noSeries {
				if v, present := seriesFor(t, m, tid, "evicted_keys"); present {
					t.Errorf("%s serves a redis_evicted_keys series (%v)", tid, v)
				}
			}
		})
	}
}

func TestWarnSubtreeDisabledUndeclared_CapsTheSample(t *testing.T) {
	t.Parallel()
	disabled := map[string][]string{}
	for i := 0; i < undeliverableLogSampleLimit+3; i++ {
		disabled[string(rune('a'+i))+"-tenant"] = []string{"redis_evicted_keys"}
	}
	var b strings.Builder
	warnSubtreeDisabledUndeclared(printer{&b}, disabled, "/conf.d")
	line := b.String()
	if !strings.Contains(line, "and 3 more") || strings.Count(line, "-tenant (") != undeliverableLogSampleLimit {
		t.Errorf("sample not capped at %d with a remainder: %s", undeliverableLogSampleLimit, line)
	}
	b.Reset()
	warnSubtreeDisabledUndeclared(printer{&b}, nil, "/conf.d")
	if b.Len() != 0 {
		t.Errorf("an empty set logged %q", b.String())
	}
}

type printer struct{ b *strings.Builder }

func (p printer) Print(v ...any) {
	for _, x := range v {
		p.b.WriteString(x.(string))
	}
}

// #3379 review: the WARN is written once per change of DisabledUndeclared,
// through the ERROR's own memory (undeliverableLogState), not once per
// reload. Driven through the watch path's reload (diffAndReload), which
// rebuilds the flat config — and so reaches commitFlatFrom — on a
// tenant-only edit too.
func TestSubtreeDisabledUndeclared_WarnsOncePerChange(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeTestYAML(t, filepath.Join(dir, "_defaults.yaml"), "defaults:\n  mysql_connections: 80\n")
	mkSub(t, dir, "finance")
	sub := filepath.Join(dir, "finance", "_defaults.yaml")
	tenant := filepath.Join(dir, "finance", "t-fin.yaml")
	writeTestYAML(t, sub, "defaults:\n  redis_evicted_keys: disable\n")
	writeTestYAML(t, tenant, "tenants:\n  t-fin: {}\n")

	m, fresh, logBuf := newAuditedManager(t, dir)
	if err := m.Load(); err != nil {
		t.Fatalf("Load: %v", err)
	}
	step := func(name string, edit func(), wantLines int, mustSay ...string) {
		t.Helper()
		logBuf.Reset()
		if edit != nil {
			edit()
		}
		if err := watchReload(m); err != nil {
			t.Fatalf("%s: reload: %v", name, err)
		}
		lines := logLinesWith(logBuf.String(), disabledUndeclaredAnchor)
		if len(lines) != wantLines {
			t.Fatalf("%s: %d WARN line(s), want %d; log:\n%s", name, len(lines), wantLines, logBuf.String())
		}
		if wantLines > 0 {
			assertLineSays(t, lines[0], disabledUndeclaredAnchor, mustSay...)
		}
		if strings.Contains(logBuf.String(), undeliverableAnchor) {
			t.Errorf("%s: undeliverable ERROR on a disable-only tree:\n%s", name, logBuf.String())
		}
		if got := testutil.ToFloat64(fresh.subtreeUndeliverableTenants); got != 0 {
			t.Errorf("%s: gauge = %v, want 0", name, got)
		}
	}
	if n := len(logLinesWith(logBuf.String(), disabledUndeclaredAnchor)); n != 1 {
		t.Fatalf("cold load: %d WARN line(s), want 1; log:\n%s", n, logBuf.String())
	}

	step("idle reload", nil, 0)
	step("tenant-only edit", func() {
		writeTestYAML(t, tenant, "tenants:\n  t-fin:\n    mysql_connections: \"70\"\n")
	}, 0)
	if v, ok := seriesFor(t, m, "t-fin", "connections"); !ok || v != 70 {
		t.Fatalf("precondition: the tenant-only edit did not land (got %v, present=%v)", v, ok)
	}
	step("set changes", func() {
		writeTestYAML(t, sub, "defaults:\n  redis_evicted_keys: disable\n  kafka_lag_seconds: disable\n")
	}, 1, "t-fin", "kafka_lag_seconds", "redis_evicted_keys")
	step("set cleared", func() {
		writeTestYAML(t, sub, "defaults:\n  mysql_connections: 60\n")
	}, 0)
	step("relapse after clearing", func() {
		writeTestYAML(t, sub, "defaults:\n  redis_evicted_keys: disable\n")
	}, 1, "t-fin", "redis_evicted_keys")
}
