package main

// served_values_test.go — `da-guard served-values` (#2115).

import (
	"encoding/json"
	"fmt"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"github.com/vencil/threshold-exporter/internal/testutil"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// servedOut is the JSON document as a reader decodes it.
type servedOut struct {
	At          string   `json:"at"`
	ParseFailed []string `json:"parse_failed"`
	Tenants     map[string]struct {
		Values     map[string]any      `json:"values"`
		Severities map[string]string   `json:"severities"`
		Unserved   map[string]any      `json:"unserved"`
		Dropped    map[string][]string `json:"dropped"`
	} `json:"tenants"`
}

// served runs the subcommand over files (paths relative to conf.d) at `at`.
func served(t *testing.T, files map[string]string, at string) (int, servedOut, string, string) {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "conf.d")
	tree := make(map[string]string, len(files))
	for k, v := range files {
		tree["conf.d/"+k] = v
	}
	testutil.WriteTree(t, filepath.Dir(dir), tree)
	args := []string{servedValuesCmd, "--config-dir", dir}
	if at != "" {
		args = append(args, "--at", at)
	}
	code, stdout, stderr := runOnce(t, args...)
	var doc servedOut
	if stdout != "" {
		if err := json.Unmarshal([]byte(stdout), &doc); err != nil {
			t.Fatalf("stdout is not JSON: %v\n%s", err, stdout)
		}
	}
	return code, doc, dir, stderr
}

func mustOK(t *testing.T, code int, stderr string) {
	t.Helper()
	if code != exitOK {
		t.Fatalf("exit = %d, want %d; stderr=%q", code, exitOK, stderr)
	}
}

func wantValue(t *testing.T, doc servedOut, tenant, key string, want float64) {
	t.Helper()
	tv, ok := doc.Tenants[tenant]
	if !ok {
		t.Fatalf("tenant %q missing; tenants=%v", tenant, keysOf(doc.Tenants))
	}
	got, ok := tv.Values[key]
	if !ok {
		t.Fatalf("tenant %q: values[%q] missing; values=%v unserved=%v", tenant, key, tv.Values, tv.Unserved)
	}
	if got != want {
		t.Errorf("tenant %q: values[%q] = %v, want %v", tenant, key, got, want)
	}
}

func keysOf[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

const defaultsOnly = "defaults:\n  mysql_connections: 80\n"

// --- where the value is written -------------------------------------------

func TestServedValues_ValueOnlyInDefaults(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly,
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    _silent_mode: disable\n",
	}, "")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "mysql_connections", 80)
}

func TestServedValues_ValueOnlyInPlatformTenants(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly + "tenants:\n  tenant-a:\n    mysql_connections: 55\n",
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    _silent_mode: disable\n",
		"tenant-b.yaml":  "tenants:\n  tenant-b:\n    _silent_mode: disable\n",
	}, "")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "mysql_connections", 55)
	wantValue(t, doc, "tenant-b", "mysql_connections", 80) // control
}

func TestServedValues_ValueInTenantFile(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml":    defaultsOnly,
		"sub/tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
	}, "")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "mysql_connections", 70)
}

func TestServedValues_ProfileFillsIn(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly + "  redis_memory: 70\n",
		"_profiles.yaml": "profiles:\n  gold:\n    redis_memory: 99\n    mysql_connections: 11\n",
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    _profile: gold\n    mysql_connections: 60\n",
	}, "")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "redis_memory", 99)      // from the profile
	wantValue(t, doc, "tenant-a", "mysql_connections", 60) // the tenant's own beats the profile
}

func TestServedValues_DisabledGoesToUnserved(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly + "  redis_memory: 70\n" +
			"tenants:\n  tenant-b:\n    redis_memory: disable\n",
		"tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: disable\n",
		"tenant-b.yaml": "tenants:\n  tenant-b:\n    _silent_mode: disable\n",
	}, "")
	mustOK(t, code, stderr)
	for tenant, key := range map[string]string{"tenant-a": "mysql_connections", "tenant-b": "redis_memory"} {
		tv := doc.Tenants[tenant]
		if _, ok := tv.Values[key]; ok {
			t.Errorf("%s: disabled %q is in values: %v", tenant, key, tv.Values[key])
		}
		if got := tv.Unserved[key]; got != "disable" {
			t.Errorf("%s: unserved[%q] = %v, want \"disable\"", tenant, key, got)
		}
	}
	wantValue(t, doc, "tenant-a", "redis_memory", 70) // control
}

func TestServedValues_ScheduleFollowsAt(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": defaultsOnly,
		"tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections:\n      default: \"70\"\n" +
			"      overrides:\n        - window: \"01:00-09:00\"\n          value: \"1000\"\n",
	}
	code, doc, _, stderr := served(t, files, "2026-07-01T03:00:00Z")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "mysql_connections", 1000)
	if doc.At != "2026-07-01T03:00:00Z" {
		t.Errorf("at = %q", doc.At)
	}
	code, doc, _, stderr = served(t, files, "2026-07-01T12:00:00Z")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "mysql_connections", 70)
}

func TestServedValues_ExpiredOverrideServesDefault(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": defaultsOnly,
		"tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections:\n      default: \"95\"\n" +
			"      expires: \"2026-06-01T00:00:00Z\"\n      reason: incident\n",
	}
	code, doc, _, stderr := served(t, files, "2026-07-01T00:00:00Z")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "mysql_connections", 80)
	code, doc, _, stderr = served(t, files, "2026-05-01T00:00:00Z")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "mysql_connections", 95)
}

func TestServedValues_MetadataInherited(t *testing.T) {
	t.Parallel()
	code, doc, dir, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly,
		"_profiles.yaml": "profiles:\n  gold:\n    _metadata:\n      owner: team-gold\n      tier: t1\n",
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    _profile: gold\n",
		"tenant-b.yaml":  "tenants:\n  tenant-b:\n    _metadata:\n      owner: team-b\n",
	}, "")
	mustOK(t, code, stderr)
	meta := doc.Tenants["tenant-a"].Values["_metadata"].(map[string]any)
	if meta["owner"] != "team-gold" || meta["tier"] != "t1" {
		t.Errorf("tenant-a _metadata = %v, want owner team-gold tier t1 (from the profile)", meta)
	}
	if got := doc.Tenants["tenant-b"].Values["_metadata"].(map[string]any)["owner"]; got != "team-b" {
		t.Errorf("tenant-b owner = %v, want team-b", got)
	}
	// And it is what the exporter's resolver reads.
	cfg, _, err := config.LoadDir(dir, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, m := range cfg.ResolveMetadata() {
		if got := doc.Tenants[m.Tenant].Values["_metadata"].(map[string]any)["owner"]; got != m.Owner {
			t.Errorf("%s: owner %v, ResolveMetadata says %q", m.Tenant, got, m.Owner)
		}
	}
}

// --- failures -------------------------------------------------------------

func TestServedValues_ParseFailedFile_ExitsThreeAndNamesIt(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct{ file, body string }{
		"tenant file":                  {"tenant-b.yaml", "tenants:\n  tenant-b: [1]\n"},
		"root defaults (typed decode)": {"_defaults.yaml", "defaults:\n  mysql_connections: abc\n"},
		"root profiles (typed decode)": {"_profiles.yaml", "profiles: [1, 2]\n"},
		"nested defaults (syntax)":     {"sub/_defaults.yaml", "defaults: [oops\n"},
	} {
		t.Run(name, func(t *testing.T) {
			files := map[string]string{
				"_defaults.yaml": defaultsOnly,
				"tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
			}
			files[tc.file] = tc.body
			code, doc, _, stderr := served(t, files, "")
			if code != exitParseFailed {
				t.Fatalf("exit = %d, want %d; stderr=%q", code, exitParseFailed, stderr)
			}
			if !reflect.DeepEqual(doc.ParseFailed, []string{tc.file}) {
				t.Errorf("parse_failed = %v, want [%s]", doc.ParseFailed, tc.file)
			}
			if _, ok := doc.Tenants["tenant-a"]; !ok {
				t.Errorf("the JSON must still carry the tenants that loaded")
			}
		})
	}
}

func TestServedValues_CleanTree_ParseFailedIsEmptyList(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": defaultsOnly,
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
	})
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", filepath.Join(tmp, "conf.d"))
	mustOK(t, code, stderr)
	// A reader tells "nothing failed" from "not reported" by the key being
	// there: [] and never null or absent.
	if !strings.Contains(stdout, `"parse_failed": []`) {
		t.Errorf("parse_failed must be an empty list on a clean tree:\n%s", stdout)
	}
}

func TestServedValues_DuplicateTenant_ExitsTwo(t *testing.T) {
	t.Parallel()
	code, _, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly,
		"a.yaml":         "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
		"b.yaml":         "tenants:\n  tenant-a:\n    mysql_connections: 71\n",
	}, "")
	if code != exitCallerErr {
		t.Fatalf("exit = %d, want %d; stderr=%q", code, exitCallerErr, stderr)
	}
	if !strings.Contains(stderr, "duplicate tenant") {
		t.Errorf("stderr should say why: %q", stderr)
	}
}

func TestServedValues_CallerErrors(t *testing.T) {
	t.Parallel()
	for name, args := range map[string][]string{
		"no config-dir": {servedValuesCmd},
		"bad at":        {servedValuesCmd, "--config-dir", t.TempDir(), "--at", "yesterday"},
		"stray arg":     {servedValuesCmd, "--config-dir", t.TempDir(), "extra"},
		"empty tree":    {servedValuesCmd, "--config-dir", t.TempDir()},
	} {
		if code, _, stderr := runOnce(t, args...); code != exitCallerErr {
			t.Errorf("%s: exit = %d, want %d; stderr=%q", name, code, exitCallerErr, stderr)
		}
	}
}

// --- the guard: values are ResolveAt's rows, nothing else --------------------

// consistencyTree exercises every row source ResolveAt has: defaults, the
// platform `tenants:` block, profiles, subtree defaults, schedules in and
// out of their window, expiry, `_critical`, `:severity`, dimensional keys,
// declared keys, a #1231 alias, custom alerts, and a disable.
var consistencyTree = map[string]string{
	"_defaults.yaml": `defaults:
  mysql_connections: 80
  mysql_threads_running: 30
  redis_memory: 70
  container_cpu: 75
  container_memory: 85
optional_overrides:
  - kafka_lag
state_filters:
  maintenance:
    reasons: []
    default_state: disable
  crashloop:
    reasons: ["CrashLoopBackOff"]
tenants:
  tenant-b:
    redis_memory: 55
`,
	"_profiles.yaml": `profiles:
  gold:
    redis_memory: 99
    container_cpu: disable
    _metadata:
      owner: team-gold
`,
	"tenant-a.yaml": `tenants:
  tenant-a:
    mysql_connections: "70:critical"
    mysql_cpu: 44
    redis_memory: disable
    kafka_lag: 1000
    _silent_mode: warning
    _severity_dedup: disable
    _state_crashloop: disable
    redis_queue_length{queue="tasks"}: "5"
    container_memory:
      default: "disable"
      overrides:
        - window: "01:00-09:00"
          value: "88"
    container_cpu:
      default: "95"
      expires: "2026-06-01T00:00:00Z"
    _custom_alerts:
      - {recipe: threshold, name: q_high, metric: qd, op: ">", window: 5m, threshold: "100:warning"}
    _routing:
      receiver:
        type: webhook
        url: https://example.invalid/hook
`,
	"tenant-b.yaml": `tenants:
  tenant-b:
    _profile: gold
    _state_maintenance: enable
    mysql_connections_critical: 95
`,
	"sub/_defaults.yaml": "defaults:\n  mysql_connections: 60\n",
	"sub/tenant-c.yaml":  "tenants:\n  tenant-c:\n    container_memory: 50\n",
}

// TestServedValues_MatchesResolveAt is the guard against the subcommand
// growing semantics of its own: over one tree, at two instants, every
// threshold entry in `values` is a row ResolveAt produced (with its value and
// severity), every row ResolveAt produced is an entry, and every reserved
// entry is what the exporter's resolver for it reads.
func TestServedValues_MatchesResolveAt(t *testing.T) {
	t.Parallel()
	for _, at := range []string{"2026-07-01T03:00:00Z", "2026-07-01T12:00:00Z", "2026-05-01T12:00:00Z"} {
		t.Run(at, func(t *testing.T) {
			code, doc, dir, stderr := served(t, consistencyTree, at)
			mustOK(t, code, stderr)
			now, _ := time.Parse(time.RFC3339, at)
			cfg, _, err := config.LoadDir(dir, nil)
			if err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(keysOf(doc.Tenants), keysOf(cfg.Tenants)) {
				t.Fatalf("tenants %v, LoadDir has %v", keysOf(doc.Tenants), keysOf(cfg.Tenants))
			}

			// Rows, per tenant: (value, severity) for thresholds, and the
			// custom-alert rows on their own.
			rowVS := map[string][]string{}
			rowCustom := map[string][]string{}
			for _, r := range cfg.ResolveAt(now) {
				vs := fmt.Sprintf("%v|%s", r.Value, r.Severity)
				if r.Component == "custom" {
					rowCustom[r.Tenant] = append(rowCustom[r.Tenant],
						fmt.Sprintf("%s|%s|%s|%s|%s", r.Metric, vs, r.CustomLabels["recipe_id"], r.CustomLabels["name"], r.CustomLabels["mode"]))
					continue
				}
				rowVS[r.Tenant] = append(rowVS[r.Tenant], vs)
			}

			for tenant, tv := range doc.Tenants {
				var gotVS []string
				for key, sev := range tv.Severities {
					v, ok := tv.Values[key].(float64)
					if !ok {
						t.Errorf("%s: severities has %q but values[%q] = %v", tenant, key, key, tv.Values[key])
						continue
					}
					vs := fmt.Sprintf("%v|%s", v, sev)
					gotVS = append(gotVS, vs)
					// A #1231 alias target is served twice: the row and its
					// legacy twin.
					if _, twin := config.LegacySpellingFor(key); twin {
						gotVS = append(gotVS, vs)
					}
				}
				sort.Strings(gotVS)
				want := append([]string(nil), rowVS[tenant]...)
				sort.Strings(want)
				if !reflect.DeepEqual(gotVS, want) {
					t.Errorf("%s: threshold values %v\n  ResolveAt rows  %v", tenant, gotVS, want)
				}

				var gotCustom []string
				if list, ok := tv.Values[customAlertsKey].([]any); ok {
					for _, e := range list {
						m := e.(map[string]any)
						l := m["labels"].(map[string]any)
						gotCustom = append(gotCustom, fmt.Sprintf("%s|%v|%s|%s|%s|%s",
							m["metric"], m["value"], m["severity"], l["recipe_id"], l["name"], l["mode"]))
					}
				}
				sort.Strings(gotCustom)
				wantCustom := append([]string(nil), rowCustom[tenant]...)
				sort.Strings(wantCustom)
				if !reflect.DeepEqual(gotCustom, wantCustom) {
					t.Errorf("%s: _custom_alerts %v\n  ResolveAt rows %v", tenant, gotCustom, wantCustom)
				}
			}

			// Reserved keys: the exporter's own resolvers.
			for _, m := range cfg.ResolveMetadata() {
				got := doc.Tenants[m.Tenant].Values["_metadata"].(map[string]any)
				if got["owner"] != m.Owner || got["tier"] != m.Tier || got["runbook_url"] != m.RunbookURL {
					t.Errorf("%s: _metadata %v, ResolveMetadata %+v", m.Tenant, got, m)
				}
			}
			dedup := map[string]string{}
			for _, d := range cfg.ResolveSeverityDedup() {
				dedup[d.Tenant] = d.Mode
			}
			ops := cfg.OperationalStatesAt(now)
			byTenant := ops.ByTenant(cfg)
			on := map[string]bool{}
			for _, sf := range ops.StateFilters {
				on[sf.Tenant+"/"+sf.FilterName] = true
			}
			routed := map[string]bool{}
			for _, r := range cfg.ResolveRouting() {
				routed[r.Tenant] = true
			}
			for tenant, tv := range doc.Tenants {
				wantDedup := dedup[tenant]
				if wantDedup == "" {
					wantDedup = "disable"
				}
				if tv.Values["_severity_dedup"] != wantDedup {
					t.Errorf("%s: _severity_dedup %v, resolver %q", tenant, tv.Values["_severity_dedup"], wantDedup)
				}
				var silent []string
				for _, s := range tv.Values["_silent_mode"].([]any) {
					silent = append(silent, s.(string))
				}
				if want := byTenant[tenant].SilentTargets; !reflect.DeepEqual(nonNilStrings(silent), want) {
					t.Errorf("%s: _silent_mode %v, resolver %v", tenant, silent, want)
				}
				for name := range cfg.StateFilters {
					if tv.Values["_state_"+name] != on[tenant+"/"+name] {
						t.Errorf("%s: _state_%s %v, resolver %v", tenant, name, tv.Values["_state_"+name], on[tenant+"/"+name])
					}
				}
				if _, ok := tv.Values["_routing"]; ok != routed[tenant] {
					t.Errorf("%s: _routing present=%v, ResolveRouting has it=%v", tenant, ok, routed[tenant])
				}
			}
		})
	}
}

// TestServedValues_ConsistencyTreeIsNotVacuous pins that the tree above does
// reach the shapes the guard is meant to cover, so a later edit to it cannot
// quietly drop one.
func TestServedValues_ConsistencyTreeIsNotVacuous(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, consistencyTree, "2026-07-01T03:00:00Z")
	mustOK(t, code, stderr)
	a, b, c := doc.Tenants["tenant-a"], doc.Tenants["tenant-b"], doc.Tenants["tenant-c"]
	checks := []struct {
		what string
		ok   bool
	}{
		{":severity suffix", a.Severities["mysql_connections"] == "critical" && a.Values["mysql_connections"] == 70.0},
		{"alias served under the canonical name", a.Values["mysql_threads_running"] == 44.0},
		{"declared key", a.Values["kafka_lag"] == 1000.0},
		{"dimensional key", a.Values[`redis_queue_length{queue="tasks"}`] == 5.0},
		{"schedule in window", a.Values["container_memory"] == 88.0},
		{"expired override serves the default", a.Values["container_cpu"] == 75.0},
		{"custom alerts", a.Values[customAlertsKey] != nil},
		{"routing", a.Values["_routing"] != nil},
		{"disabled → unserved", a.Unserved["redis_memory"] == "disable"},
		{"platform tenants: beats profile", b.Values["redis_memory"] == 55.0},
		{"profile disable → unserved", b.Unserved["container_cpu"] == "disable"},
		{"_critical", b.Values["mysql_connections_critical"] == 95.0 && b.Severities["mysql_connections_critical"] == "critical"},
		{"subtree defaults", c.Values["mysql_connections"] == 60.0},
	}
	for _, ch := range checks {
		if !ch.ok {
			t.Errorf("consistency tree no longer reaches: %s\n a=%v\n b=%v\n c=%v", ch.what, a, b, c)
		}
	}
}

// --- trees /metrics cannot serve, and values JSON has no number for ---------

func TestServedValues_TwoKeysOneSeries_ExitsTwoNamingBothKeys(t *testing.T) {
	t.Parallel()
	for name, tenant := range map[string]string{
		// `X: "n:critical"` and `X_critical` both emit severity="critical" for X.
		"severity suffix vs _critical": "    mysql_connections: \"70:critical\"\n    mysql_connections_critical: 95\n",
		// A regex label `q` is exported as `q_re`.
		"regex label vs exact _re label": "    redis_queue_length{q=~\"a\"}: 2\n    redis_queue_length{q_re=\"a\"}: 3\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			code, doc, _, stderr := served(t, map[string]string{
				"_defaults.yaml": defaultsOnly + "max_metrics_per_tenant: 5\n",
				"tenant-a.yaml":  "tenants:\n  tenant-a:\n" + tenant,
			}, "")
			if code != exitCallerErr {
				t.Fatalf("exit = %d, want %d; tenants=%v stderr=%q", code, exitCallerErr, doc.Tenants, stderr)
			}
			if !strings.Contains(stderr, "was collected before with the same name and label values") || !strings.Contains(stderr, "HTTP 500") {
				t.Errorf("stderr should name the collision: %q", stderr)
			}
		})
	}
	code, _, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly,
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: \"70:critical\"\n    mysql_connections_critical: 95\n",
	}, "")
	for _, key := range []string{`"mysql_connections"`, `"mysql_connections_critical"`} {
		if code != exitCallerErr || !strings.Contains(stderr, key) {
			t.Errorf("exit %d, stderr must name %s: %q", code, key, stderr)
		}
	}
}

func TestServedValues_NonFiniteThresholds(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly + "  redis_memory: 70\n  container_cpu: 75\n",
		"tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: NaN\n    redis_memory: \"+Inf\"\n" +
			"    container_cpu: \"-inf:critical\"\n",
	}, "")
	mustOK(t, code, stderr)
	tv := doc.Tenants["tenant-a"]
	for key, want := range map[string]string{"mysql_connections": "NaN", "redis_memory": "+Inf", "container_cpu": "-Inf"} {
		if got := tv.Values[key]; got != want {
			t.Errorf("values[%q] = %#v, want %q", key, got, want)
		}
	}
	if tv.Severities["container_cpu"] != "critical" {
		t.Errorf("severities = %v", tv.Severities)
	}
}

// --- unserved -----------------------------------------------------------------

func TestServedValues_AliasSpellingServedIsNotUnserved(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly + "  mysql_threads_running: 30\n",
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_cpu: 44\n",
	}, "")
	mustOK(t, code, stderr)
	tv := doc.Tenants["tenant-a"]
	wantValue(t, doc, "tenant-a", "mysql_threads_running", 44)
	if len(tv.Unserved) != 0 {
		t.Errorf("the tenant's old spelling is served under the canonical key, yet unserved = %v", tv.Unserved)
	}
}

// Both spellings in one tenant: /metrics serves the canonical key's value, so
// the old spelling's own value is unserved — even though the canonical key it
// maps to is in values. Alone, the old spelling is what is served (above).
func TestServedValues_AliasShadowedByCanonicalIsUnserved(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly + "  mysql_threads_running: 30\n",
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_cpu: \"44\"\n    mysql_threads_running: \"50\"\n",
	}, "")
	mustOK(t, code, stderr)
	tv := doc.Tenants["tenant-a"]
	wantValue(t, doc, "tenant-a", "mysql_threads_running", 50)
	if got, ok := tv.Unserved["mysql_cpu"]; !ok || got != "44" {
		t.Errorf(`unserved["mysql_cpu"] = %#v (present=%v), want "44"; unserved = %v`, got, ok, tv.Unserved)
	}
	if _, ok := tv.Unserved["mysql_threads_running"]; ok {
		t.Errorf("the canonical key is served, yet unserved = %v", tv.Unserved)
	}

	// Control: the old spelling alone is served under the canonical key.
	code, doc, _, stderr = served(t, map[string]string{
		"_defaults.yaml": defaultsOnly + "  mysql_threads_running: 30\n",
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_cpu: 44\n",
	}, "")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "mysql_threads_running", 44)
	if _, ok := doc.Tenants["tenant-a"].Unserved["mysql_cpu"]; ok {
		t.Errorf("mysql_cpu alone is served, yet unserved = %v", doc.Tenants["tenant-a"].Unserved)
	}
}

func TestServedValues_UnservedScheduleKeepsItsWindows(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly + "  container_memory: 85\n",
		"tenant-a.yaml": "tenants:\n  tenant-a:\n    container_memory:\n      default: \"disable\"\n" +
			"      overrides:\n        - window: \"01:00-09:00\"\n          value: \"88\"\n" +
			"    mysql_connections:\n      default: \"disable\"\n      expires: \"2027-01-01T00:00:00Z\"\n      reason: quiet\n",
	}, "2026-07-01T12:00:00Z")
	mustOK(t, code, stderr)
	u := doc.Tenants["tenant-a"].Unserved
	want := map[string]any{
		"container_memory": map[string]any{
			"default":   "disable",
			"overrides": []any{map[string]any{"window": "01:00-09:00", "value": "88"}},
		},
		"mysql_connections": map[string]any{"default": "disable", "expires": "2027-01-01T00:00:00Z", "reason": "quiet"},
	}
	if !reflect.DeepEqual(u, want) {
		t.Errorf("unserved = %#v\nwant %#v", u, want)
	}
}

// --- rows the exporter's collector drops, and collisions only Gather sees ------

func servedTree(t *testing.T, tenant string) (int, servedOut, string) {
	t.Helper()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly,
		"tenant-a.yaml":  "tenants:\n  tenant-a:\n" + tenant,
	}, "")
	return code, doc, stderr
}

// A label client_golang rejects makes the collector drop the row (WARN, not a
// failed scrape): /metrics serves the rest, so the key is dropped, never a
// value.
func TestServedValues_RowsTheCollectorDrops(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		tenant string
		keys   []string
	}{
		"label named like a fixed one": {
			"    mysql_connections{tenant=\"x\"}: 5\n    mysql_connections{severity=\"y\"}: 6\n",
			[]string{`mysql_connections{tenant="x"}`, `mysql_connections{severity="y"}`}},
		// `__` is reserved for Prometheus-internal labels.
		"reserved label name": {
			"    mysql_connections{__x=\"x\"}: 5\n",
			[]string{`mysql_connections{__x="x"}`}},
		"one row with q_re twice": {
			"    redis_queue_length{q_re=\"a\", q=~\"b\"}: 5\n",
			[]string{`redis_queue_length{q_re="a", q=~"b"}`}},
		// Both rows are dropped, so their would-be collision never reaches
		// Gather: /metrics serves 200 and so must this.
		"two dropped rows that would collide": {
			"    mysql_connections{tenant=\"x\"}: 5\n    mysql_connections{ tenant = \"x\" }: 6\n",
			[]string{`mysql_connections{tenant="x"}`, `mysql_connections{ tenant = "x" }`}},
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			code, doc, stderr := servedTree(t, tc.tenant)
			mustOK(t, code, stderr)
			tv := doc.Tenants["tenant-a"]
			for _, key := range tc.keys {
				if _, ok := tv.Values[key]; ok {
					t.Errorf("dropped %q is in values: %v", key, tv.Values)
				}
				if _, ok := tv.Unserved[key]; !ok {
					t.Errorf("dropped %q is not in unserved: %v", key, tv.Unserved)
				}
				if len(tv.Dropped[key]) == 0 {
					t.Errorf("dropped %q is not in dropped: %v", key, tv.Dropped)
				}
			}
			wantValue(t, doc, "tenant-a", "mysql_connections", 80) // the base row is still served
		})
	}
}

// Label order is not identity: `{a_re="x", z="y"}` and `{a=~"x", z="y"}` are
// one series to Prometheus, whatever order the labels were written in.
func TestServedValues_SameSeriesInAnotherLabelOrder_ExitsTwo(t *testing.T) {
	t.Parallel()
	code, doc, stderr := servedTree(t, "    redis_queue_length{a_re=\"x\", z=\"y\"}: 1\n    redis_queue_length{a=~\"x\", z=\"y\"}: 2\n")
	if code != exitCallerErr {
		t.Fatalf("exit = %d, want %d; tenants=%v stderr=%q", code, exitCallerErr, doc.Tenants, stderr)
	}
}

// Same label VALUES under different label NAMES are two series.
func TestServedValues_SameValuesOtherLabelNames_Serves(t *testing.T) {
	t.Parallel()
	code, doc, stderr := servedTree(t, "    redis_queue_length{q=\"a\"}: 1\n    redis_queue_length{r=\"a\"}: 2\n")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", `redis_queue_length{q="a"}`, 1)
	wantValue(t, doc, "tenant-a", `redis_queue_length{r="a"}`, 2)
}

func TestServedValues_DroppedIsAnEmptyObjectWhenNothingIsDropped(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": defaultsOnly,
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: 70\n",
	})
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", filepath.Join(tmp, "conf.d"))
	mustOK(t, code, stderr)
	if !strings.Contains(stdout, `"dropped": {}`) {
		t.Errorf("dropped must be {} when nothing is dropped:\n%s", stdout)
	}
}

// --- strings JSON cannot carry ----------------------------------------------------

// A tenant id or key that is not valid UTF-8 would reach JSON as U+FFFD, so
// two distinct keys could come out as one; it is refused and named instead.
//
// NOT parallel, subtests included: these trees make the collector fail
// NewConstMetric and log.Printf through the process-global logger, which
// run() points at its caller's stderr buffer — under t.Parallel that is some
// other test's buffer, written by two goroutines at once (-race). Serial, with
// an idempotent reset so the lines go to the real stderr.
func TestServedValues_NonUTF8_ExitsTwoNamingIt(t *testing.T) {
	log.SetOutput(os.Stderr)
	log.SetFlags(log.LstdFlags)
	const sf = "state_filters:\n  maintenance:\n    reasons: []\n    default_state: disable\n"
	for name, tc := range map[string]struct {
		files map[string]string
		want  string
	}{
		// `dP8=` is "t\xff"
		"tenant id": {map[string]string{
			"_defaults.yaml": defaultsOnly,
			"tenant-a.yaml":  "tenants:\n  ? !!binary dP8=\n  : {mysql_connections: 5}\n  tenant-b:\n    mysql_connections: 7\n",
		}, `tenant "t\xff"`},
		// mysql_connections{q="\xff"} and mysql_connections{q="\xfe"}: both become {q="\ufffd"} in JSON.
		"tenant key": {map[string]string{
			"_defaults.yaml": defaultsOnly,
			"tenant-a.yaml": "tenants:\n  tenant-a:\n    ? !!binary bXlzcWxfY29ubmVjdGlvbnN7cT0i/yJ9\n    : 5\n" +
				"    ? !!binary bXlzcWxfY29ubmVjdGlvbnN7cT0i/iJ9\n    : 6\n",
		}, `tenant "tenant-a": key "mysql_connections{q=\"\x`}, // \xfe or \xff: whichever the map yields first
		"defaults key": {map[string]string{
			"_defaults.yaml": defaultsOnly + "  ? !!binary bXlzcWxfY29ubmVjdGlvbnN7cT0i/yJ9\n  : 5\n" +
				"  ? !!binary bXlzcWxfY29ubmVjdGlvbnN7cT0i/iJ9\n  : 6\n",
			"tenant-a.yaml": "tenants:\n  tenant-a:\n    mysql_connections: 3\n",
		}, `defaults: key "mysql_connections{q=\"\x`},
		// `eP8=` / `eP4=` are "x\xff" / "x\xfe".
		"state filter name": {map[string]string{
			"_defaults.yaml": defaultsOnly + "state_filters:\n  ? !!binary eP8=\n  : {reasons: [\"a\"]}\n  ? !!binary eP4=\n  : {reasons: [\"b\"]}\n",
			"tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: 3\n",
		}, `state_filters: name "x\x`},
		// Values, not keys: the reserved readings carry them out.
		"_metadata value": {map[string]string{
			"_defaults.yaml": defaultsOnly + sf,
			"tenant-a.yaml":  "tenants:\n  tenant-a:\n    _metadata:\n      runbook_url: !!binary /w==\n      db_type: !!binary /w==\n",
		}, `tenants["tenant-a"].values["_metadata"]["db_type"]`}, // first in key order
		// A map KEY, not a value: `eP8=` / `eP4=` are "x\xff" / "x\xfe".
		"_routing receiver key": {map[string]string{
			"_defaults.yaml": defaultsOnly + sf,
			"tenant-a.yaml": "tenants:\n  tenant-a:\n    _routing:\n      receiver:\n        type: webhook\n        url: http://x\n" +
				"        ? !!binary eP8=\n        : a\n        ? !!binary eP4=\n        : b\n",
		}, `values["_routing"]["receiver"]["x\xfe"] (key)`},
		"_routing value": {map[string]string{
			"_defaults.yaml": defaultsOnly + sf,
			"tenant-a.yaml":  "tenants:\n  tenant-a:\n    _routing:\n      receiver:\n        type: webhook\n        url: !!binary /w==\n",
		}, `tenants["tenant-a"].values["_routing"]`},
		// Two broken files whose names differ only in a non-UTF-8 byte: JSON
		// would render both as "b\ufffd.yaml".
		"parse_failed file name": {map[string]string{
			"_defaults.yaml": defaultsOnly,
			"tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: 3\n",
			"b\xfe.yaml":     "tenants: [\n",
			"b\xff.yaml":     "tenants: [\n",
		}, `parse_failed[0]: "b\xfe.yaml"`},
	} {
		t.Run(name, func(t *testing.T) {
			// Several runs: where two strings are bad, the one named must be
			// the first in key order every time, not whichever map
			// iteration happens to reach first.
			for run := 0; run < 8; run++ {
				code, _, _, stderr := served(t, tc.files, "")
				if code != exitCallerErr {
					t.Fatalf("exit = %d, want %d; stderr=%q", code, exitCallerErr, stderr)
				}
				if !strings.Contains(stderr, tc.want) || !strings.Contains(stderr, "not valid UTF-8") {
					t.Fatalf("run %d: stderr should name it (%s): %q", run, tc.want, stderr)
				}
			}
		})
	}
	// Control: the same shapes spelled in UTF-8 serve.
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly,
		"tenant-a.yaml": "tenants:\n  tenant-ü:\n    mysql_connections: 5\n  tenant-a:\n" +
			"    mysql_connections{q=\"é\"}: 5\n    mysql_connections{q=\"è\"}: 6\n",
	}, "")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-ü", "mysql_connections", 5)
	wantValue(t, doc, "tenant-a", `mysql_connections{q="é"}`, 5)
	wantValue(t, doc, "tenant-a", `mysql_connections{q="è"}`, 6)
}

// A key whose row AND its #1231 legacy twin are both dropped has two reasons,
// one per row.
func TestServedValues_DroppedKeepsEveryRowsReason(t *testing.T) {
	t.Parallel()
	code, doc, stderr := servedTree(t, "    mysql_cpu{__x=\"y\"}: 5\n    mysql_cpu: 9\n")
	mustOK(t, code, stderr)
	got := doc.Tenants["tenant-a"].Dropped[`mysql_threads_running{__x="y"}`]
	if len(got) != 2 {
		t.Errorf("dropped reasons = %q, want 2 (the row and its legacy twin)", got)
	}
}

// --- families other than user_threshold ----------------------------------------------

// Two expired overrides whose da_config_event reasons render alike
// ("container_cpu: a: b") collide in that family: production /metrics answers
// 500 (pinned in the app package), so served-values must refuse too even
// though every user_threshold row is fine.
func TestServedValues_OtherFamilyFailsGather_ExitsTwo(t *testing.T) {
	t.Parallel()
	code, doc, _, stderr := served(t, map[string]string{
		"_defaults.yaml": defaultsOnly + "  container_cpu: 75\n  \"container_cpu: a\": 50\n",
		"tenant-a.yaml": "tenants:\n  tenant-a:\n    container_cpu:\n      default: \"95\"\n      expires: \"2026-06-01T00:00:00Z\"\n      reason: \"a: b\"\n" +
			"    \"container_cpu: a\":\n      default: \"96\"\n      expires: \"2026-06-01T00:00:00Z\"\n      reason: \"b\"\n",
	}, "2026-07-01T00:00:00Z")
	if code != exitCallerErr {
		t.Fatalf("exit = %d, want %d; tenants=%v stderr=%q", code, exitCallerErr, doc.Tenants, stderr)
	}
	if !strings.Contains(stderr, "da_config_event") || !strings.Contains(stderr, "HTTP 500") {
		t.Errorf("stderr should carry client_golang's error: %q", stderr)
	}
}

// A non-UTF-8 name in optional_overrides reaches no output string while no
// tenant sets it, and /metrics serves the tree: so must this, with no U+FFFD.
func TestServedValues_NonUTF8DeclaredKeyNobodySets_Serves(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	testutil.WriteTree(t, tmp, map[string]string{
		"conf.d/_defaults.yaml": defaultsOnly + "optional_overrides:\n  - !!binary eP8=\n",
		"conf.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: 3\n",
	})
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", filepath.Join(tmp, "conf.d"))
	mustOK(t, code, stderr)
	if strings.Contains(stdout, "\ufffd") || strings.Contains(stdout, `\ufffd`) {
		t.Errorf("output carries U+FFFD:\n%s", stdout)
	}
}

// --- threshold expiry events follow --at ----------------------------------------------

// Two time-boxed overrides whose expiry events would render one
// da_config_event series ("container_cpu: a: b"): before they expire nothing
// collides; after, the scrape fails. The expiry is far in the future, so only
// --at (not the wall clock) can put the run after it.
func TestServedValues_ThresholdExpiryFollowsAt(t *testing.T) {
	t.Parallel()
	files := map[string]string{
		"_defaults.yaml": defaultsOnly + "  container_cpu: 75\n  \"container_cpu: a\": 50\n",
		"tenant-a.yaml": "tenants:\n  tenant-a:\n    container_cpu:\n      default: \"95\"\n      expires: \"2099-06-01T00:00:00Z\"\n      reason: \"a: b\"\n" +
			"    \"container_cpu: a\":\n      default: \"96\"\n      expires: \"2099-06-01T00:00:00Z\"\n      reason: \"b\"\n",
	}
	code, doc, _, stderr := served(t, files, "2026-07-01T00:00:00Z")
	mustOK(t, code, stderr)
	wantValue(t, doc, "tenant-a", "container_cpu", 95)
	code, _, _, stderr = served(t, files, "2099-07-01T00:00:00Z")
	if code != exitCallerErr || !strings.Contains(stderr, "da_config_event") {
		t.Fatalf("after expiry: exit = %d, want %d naming da_config_event; stderr=%q", code, exitCallerErr, stderr)
	}
}

// --config-dir is the caller's own argument and is not in the output, so a
// path that is not valid UTF-8 does not refuse the tree.
func TestServedValues_NonUTF8ConfigDir_Serves(t *testing.T) {
	t.Parallel()
	tmp := t.TempDir()
	dir := filepath.Join(tmp, "conf\xff.d")
	testutil.WriteTree(t, tmp, map[string]string{
		"conf\xff.d/_defaults.yaml": defaultsOnly,
		"conf\xff.d/tenant-a.yaml":  "tenants:\n  tenant-a:\n    mysql_connections: 3\n",
	})
	code, stdout, stderr := runOnce(t, servedValuesCmd, "--config-dir", dir)
	mustOK(t, code, stderr)
	if strings.Contains(stdout, `\ufffd`) || strings.Contains(stdout, "\ufffd") {
		t.Errorf("output carries U+FFFD:\n%s", stdout)
	}
}
