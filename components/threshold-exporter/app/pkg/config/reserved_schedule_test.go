package config

// reserved_schedule_test.go — #3269: a reserved key written with a schedule
// (`overrides:` / `expires:` / `reason:`) is read as its `default` alone on
// every plane — the typed decode, the subtree overlay, tenant-api's merge,
// the walker's reported config — and refused by the write gate, with a
// ratchet that compares the value as written.
//
// Seams: none — in-memory documents and t.TempDir() trees.

import (
	"fmt"
	"io"
	"log"
	"reflect"
	"sort"
	"strings"
	"testing"
	"time"

	"gopkg.in/yaml.v3"
)

// rsDoc is a tenant config document whose tenant t1 writes key: value, with
// everything the reserved keys below need declared beside it.
func rsDoc(key, value string) string {
	return "defaults:\n  mysql_connections: 80\n" +
		"state_filters:\n" +
		"  crashloop:\n    reasons: [CrashLoopBackOff]\n    severity: critical\n" +
		"  maintenance:\n    reasons: []\n    severity: info\n    default_state: disable\n" +
		"profiles:\n  p1:\n    mysql_connections: \"11\"\n  p2:\n    mysql_connections: \"22\"\n" +
		"tenants:\n  t1:\n    " + key + ": " + value + "\n"
}

// rsKeys is, per reserved key, a default and a value a window would switch
// it to — each a value its reader acts on, so a window that took effect
// would change what is served.
var rsKeys = []struct{ key, def, win string }{
	{"_silent_mode", "disable", "all"},
	{"_severity_dedup", "enable", "disable"},
	{"_state_crashloop", "enable", "disable"},
	{"_state_maintenance", "disable", "enable"},
	{"_profile", "p1", "p2"},
	{"_routing_profile", "rp1", "rp2"},
}

// rsForms are the schedule forms: a window, an expiry, a reason alone.
var rsForms = []struct {
	name string
	val  func(def, win string) string
}{
	{"overrides", func(def, win string) string {
		return fmt.Sprintf(`{default: %q, overrides: [{window: "03:00-04:00", value: %q}]}`, def, win)
	}},
	{"expires", func(def, _ string) string {
		return fmt.Sprintf(`{default: %q, expires: "2020-01-01T00:00:00Z"}`, def)
	}},
	{"reason", func(def, _ string) string { return fmt.Sprintf(`{default: %q, reason: "r"}`, def) }},
}

// rsInWindow is an instant inside the 03:00-04:00 window.
var rsInWindow = time.Date(2026, 10, 22, 3, 30, 0, 0, time.UTC)

// rsServed renders everything the exporter serves from c at now: threshold
// rows (after profile expansion), silent modes, state filters, severity
// dedup — sorted, so two configs serving the same compare equal.
func rsServed(t *testing.T, c ThresholdConfig, now time.Time) string {
	t.Helper()
	c.applyProfiles(nil)
	var out []string
	for _, r := range c.ResolveAt(now) {
		out = append(out, fmt.Sprintf("row %s %s %s %v", r.Tenant, r.Metric, r.Severity, r.Value))
	}
	for _, s := range c.resolveSilentModesAt(now, nil) {
		out = append(out, fmt.Sprintf("silent %s %s %v", s.Tenant, s.TargetSeverity, s.Expired))
	}
	for _, s := range c.resolveStateFiltersAt(now, nil) {
		out = append(out, fmt.Sprintf("state %s %s", s.Tenant, s.FilterName))
	}
	for _, d := range c.ResolveSeverityDedup() {
		out = append(out, fmt.Sprintf("dedup %s %s", d.Tenant, d.Mode))
	}
	sort.Strings(out)
	return strings.Join(out, "\n")
}

func mustParse(t *testing.T, doc string) ThresholdConfig {
	t.Helper()
	c, err := ParseConfigFile([]byte(doc))
	if err != nil {
		t.Fatalf("ParseConfigFile: %v\n%s", err, doc)
	}
	return c
}

// The typed decode keeps a scheduled reserved value's `default` alone: the
// decode equals the one of the same file writing only the default, so every
// reader serves the same — also inside the window — and ScheduleCuts /
// AtMinuteOfDay see no window. The entry is recorded for the load's WARN.
func TestParseConfigFile_ScheduledReservedKeepsDefaultOnly(t *testing.T) {
	t.Parallel()
	for _, k := range rsKeys {
		for _, f := range rsForms {
			t.Run(k.key+"/"+f.name, func(t *testing.T) {
				t.Parallel()
				sched := mustParse(t, rsDoc(k.key, f.val(k.def, k.win)))
				plain := mustParse(t, rsDoc(k.key, fmt.Sprintf("%q", k.def)))

				if !reflect.DeepEqual(sched.Tenants, plain.Tenants) {
					t.Errorf("tenants = %+v, want the default-only decode %+v", sched.Tenants, plain.Tenants)
				}
				if got, want := sched.ScheduledReservedEntries(), []string{"tenants.t1." + k.key}; !reflect.DeepEqual(got, want) {
					t.Errorf("ScheduledReservedEntries = %q, want %q", got, want)
				}
				if plain.ScheduledReservedEntries() != nil {
					t.Errorf("default-only: ScheduledReservedEntries = %q, want none", plain.ScheduledReservedEntries())
				}
				if got, want := rsServed(t, sched, rsInWindow), rsServed(t, plain, rsInWindow); got != want {
					t.Errorf("served inside the window:\n%s\nwant (default only):\n%s", got, want)
				}
				if cuts := sched.ScheduleCuts(); !reflect.DeepEqual(cuts, []int{0}) {
					t.Errorf("ScheduleCuts = %v, want [0] (no reserved window is a cut)", cuts)
				}
				if got, want := rsServed(t, *sched.AtMinuteOfDay(3 * 60), rsInWindow), rsServed(t, plain, rsInWindow); got != want {
					t.Errorf("AtMinuteOfDay(03:00) serves:\n%s\nwant:\n%s", got, want)
				}
			})
		}
	}
}

// A profile's reserved key is the same: kept as its default, named
// `profiles.<name>.<key>`.
func TestParseConfigFile_ScheduledReservedInAProfile(t *testing.T) {
	t.Parallel()
	c := mustParse(t, "profiles:\n  gold:\n    _silent_mode: {default: warning, overrides: [{window: \"03:00-04:00\", value: all}]}\n")
	if sv := c.Profiles["gold"]["_silent_mode"]; !reflect.DeepEqual(sv, ScheduledValue{Default: "warning"}) {
		t.Errorf("profile value = %+v, want the default alone", sv)
	}
	if got := c.ScheduledReservedEntries(); !reflect.DeepEqual(got, []string{"profiles.gold._silent_mode"}) {
		t.Errorf("ScheduledReservedEntries = %q", got)
	}
}

// Not a schedule: a mapping key's mapping (its `default`, `overrides` and
// `expires` are fields of it, kept whole by keepReservedMappings), a
// `_profile` written `{default: <name>}` (#2433's election form), and a
// threshold's schedule (honoured).
func TestScheduledReserved_NotMisjudged(t *testing.T) {
	t.Parallel()
	cases := []struct{ key, value string }{
		{"_metadata", "{default: x, owner: team-a, tier: tier-1}"},
		{"_metadata", `{default: x, overrides: [{window: "03:00-04:00", value: y}], owner: team-a}`},
		{"_routing", `{default: x, receiver: {type: webhook, url: "https://e.example/h"}}`},
		{"_routing", `{default: x, expires: "2020-01-01T00:00:00Z", receiver: {type: webhook, url: "https://e.example/h"}}`},
		{"_profile", "{default: p1}"},
		{"mysql_connections", `{default: "70", overrides: [{window: "03:00-04:00", value: "1000"}]}`},
	}
	for _, c := range cases {
		t.Run(c.key+" "+c.value, func(t *testing.T) {
			t.Parallel()
			doc := rsDoc(c.key, c.value)
			parsed := mustParse(t, doc)
			if parsed.ScheduledReservedEntries() != nil {
				t.Errorf("ScheduledReservedEntries = %q, want none", parsed.ScheduledReservedEntries())
			}
			// The write gate's decode (tenant-api: a plain yaml.Unmarshal).
			var gate ThresholdConfig
			if err := yaml.Unmarshal([]byte(doc), &gate); err != nil {
				t.Fatal(err)
			}
			for _, f := range gate.ValidateTenantKeys().Values {
				if f.schedule {
					t.Errorf("schedule finding on %s: %q", c.key, f.Message)
				}
			}
		})
	}
	// The mapping is kept whole (the probe that started #3269 once read
	// _metadata's owner as lost): owner is still there.
	md := mustParse(t, rsDoc("_metadata", "{default: x, owner: team-a, tier: tier-1}"))
	if !strings.Contains(md.Tenants["t1"]["_metadata"].Default, "owner: team-a") {
		t.Errorf("_metadata = %q, want the mapping whole", md.Tenants["t1"]["_metadata"].Default)
	}
	// A threshold's window is honoured and is a cut.
	th := mustParse(t, rsDoc("mysql_connections", `{default: "70", overrides: [{window: "03:00-04:00", value: "1000"}]}`))
	if cuts := th.ScheduleCuts(); !reflect.DeepEqual(cuts, []int{0, 180, 240}) {
		t.Errorf("threshold ScheduleCuts = %v, want [0 180 240]", cuts)
	}
}

// gateDecode is the write gate's decode of doc (tenant-api validateShape:
// a plain yaml.Unmarshal — the schedule as written).
func gateDecode(t *testing.T, doc string) *ThresholdConfig {
	t.Helper()
	var c ThresholdConfig
	if err := yaml.Unmarshal([]byte(doc), &c); err != nil {
		t.Fatal(err)
	}
	return &c
}

// The write gate refuses every scheduled reserved value with one finding —
// one message, also for `expires:` (the threshold `expires:` check stays out
// of it) — that names the fields and the default that is read.
func TestValidateTenantKeys_ScheduledReservedIsOneBlockingFinding(t *testing.T) {
	t.Parallel()
	for _, k := range rsKeys {
		for _, f := range rsForms {
			t.Run(k.key+"/"+f.name, func(t *testing.T) {
				t.Parallel()
				kv := gateDecode(t, rsDoc(k.key, f.val(k.def, k.win))).ValidateTenantKeys()
				var mine []string
				for _, e := range kv.Errors {
					if strings.Contains(e, k.key) {
						mine = append(mine, e)
					}
				}
				want := fmt.Sprintf("WARN: tenant=t1: %s writes `%s:`", k.key, f.name)
				if len(mine) != 1 || !strings.HasPrefix(mine[0], want) || !strings.Contains(mine[0], fmt.Sprintf("only its default %q is read", k.def)) {
					t.Fatalf("errors about %s = %q, want one starting %q", k.key, mine, want)
				}
				if len(kv.Values) != 1 || kv.Values[0].Message != mine[0] || !kv.Values[0].schedule {
					t.Errorf("values = %+v, want the error as one schedule finding", kv.Values)
				}
			})
		}
	}
}

// The ratchet compares a schedule as written: the same value is a notice;
// any change to the default, a window or the expiry still blocks; removing
// the schedule leaves nothing to say. SelfRatchet (GET) makes it a notice.
func TestRatchet_ScheduledReserved(t *testing.T) {
	t.Parallel()
	const key = "_silent_mode"
	win := func(w, v string) string {
		return fmt.Sprintf(`{default: disable, overrides: [{window: %q, value: %q}]}`, w, v)
	}
	base := rsDoc(key, win("03:00-04:00", "all"))
	cases := []struct {
		name, body string
		want       string // "error", "notice" or "none"
	}{
		{"unchanged", base, "notice"},
		{"window moved", rsDoc(key, win("05:00-06:00", "all")), "error"},
		{"window value changed", rsDoc(key, win("03:00-04:00", "warning")), "error"},
		{"window added", rsDoc(key, `{default: disable, overrides: [{window: "03:00-04:00", value: all}, {window: "05:00-06:00", value: all}]}`), "error"},
		{"default changed", rsDoc(key, `{default: warning, overrides: [{window: "03:00-04:00", value: all}]}`), "error"},
		{"expires added", rsDoc(key, `{default: disable, overrides: [{window: "03:00-04:00", value: all}], expires: "2099-01-01T00:00:00Z"}`), "error"},
		{"schedule removed", rsDoc(key, "disable"), "none"},
	}
	prev := PriorValues(gateDecode(t, base).Tenants)
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			got := gateDecode(t, c.body).ValidateTenantKeys().Ratchet(prev)
			errs, notices := strings.Join(got.Errors, "\n"), strings.Join(got.Notices, "\n")
			switch c.want {
			case "error":
				if !strings.Contains(errs, "WARN: tenant=t1: _silent_mode writes `overrides:`") || strings.Contains(notices, "_silent_mode writes") {
					t.Errorf("want blocking: errors %q notices %q", got.Errors, got.Notices)
				}
			case "notice":
				if len(got.Errors) != 0 || !strings.Contains(notices, "NOTICE: tenant=t1: _silent_mode writes `overrides:`") {
					t.Errorf("want a notice: errors %q notices %q", got.Errors, got.Notices)
				}
			case "none":
				if strings.Contains(errs+notices, "_silent_mode") {
					t.Errorf("want nothing: errors %q notices %q", got.Errors, got.Notices)
				}
			}
		})
	}
	// A new tenant (no prior value) is strict.
	if got := gateDecode(t, base).ValidateTenantKeys().Ratchet(func(string, string) (ScheduledValue, bool) {
		return ScheduledValue{}, false
	}); len(got.Errors) != 1 {
		t.Errorf("no prior value: errors %q, want blocking", got.Errors)
	}
	// GET: what is on disk is a notice.
	self := gateDecode(t, base).ValidateTenantKeysByTenant().SelfRatchet()
	if v := self.Tenants["t1"]; len(v.Errors) != 0 || len(v.Notices) != 1 || !strings.HasPrefix(v.Notices[0], "NOTICE: tenant=t1: _silent_mode writes `overrides:`") {
		t.Errorf("SelfRatchet = %+v", v)
	}
}

// A value finding of the reading kind still ratchets by the reader's
// reading next to a schedule finding on the same key: an unchanged bad
// default with a moved window keeps only the schedule finding blocking.
func TestRatchet_ScheduleAndReadingFindingsAreJudgedApart(t *testing.T) {
	t.Parallel()
	base := rsDoc("_silent_mode", `{default: purple, overrides: [{window: "03:00-04:00", value: all}]}`)
	body := rsDoc("_silent_mode", `{default: purple, overrides: [{window: "05:00-06:00", value: all}]}`)
	got := gateDecode(t, body).ValidateTenantKeys().Ratchet(PriorValues(gateDecode(t, base).Tenants))
	if len(got.Errors) != 1 || !strings.Contains(got.Errors[0], "writes `overrides:`") ||
		len(got.Notices) != 1 || !strings.Contains(got.Notices[0], `"purple" is not a value the exporter reads`) {
		t.Errorf("errors %q notices %q", got.Errors, got.Notices)
	}
}

// rsTree is a conf.d tree: a root _defaults.yaml declaring the state
// filters, plus files.
func rsTree(t *testing.T, files map[string]string) string {
	t.Helper()
	all := map[string]string{
		"_defaults.yaml": "defaults:\n  mysql_connections: 80\n" +
			"state_filters:\n  crashloop:\n    reasons: [CrashLoopBackOff]\n    severity: critical\n",
	}
	for k, v := range files {
		all[k] = v
	}
	return writeUndeliverableTree(t, all)
}

// A subtree `_defaults.yaml` hands no reserved key down (#3341), windowed or
// not: `_silent_mode` / `_severity_dedup` / a windowed `_state_` key whose
// default is `disable` are all left out, as the scalar `disable` is. (Until
// #3341 the overlay applied their `disable` default alone, #3295; this test
// pinned that and now pins the sunset.)
func TestLoadDir_SubtreeScheduledReservedIsNotApplied(t *testing.T) {
	t.Parallel()
	dir := rsTree(t, map[string]string{
		"finance/_defaults.yaml": "defaults:\n" +
			"  _silent_mode: {default: disable, overrides: [{window: \"03:00-04:00\", value: all}]}\n" +
			"  _severity_dedup: {default: disable, expires: \"2020-01-01T00:00:00Z\"}\n" +
			"  _state_crashloop: {default: disable, overrides: [{window: \"03:00-04:00\", value: enable}]}\n",
		"finance/t1.yaml": "tenants:\n  t1:\n    mysql_connections: \"70\"\n",
	})
	cfg, failed, err := LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil || len(failed) != 0 {
		t.Fatalf("LoadDir: %v %v", err, failed)
	}
	got := cfg.Tenants["t1"]
	if want := map[string]ScheduledValue{"mysql_connections": {Default: "70"}}; !reflect.DeepEqual(got, want) {
		t.Errorf("t1 = %+v, want %+v: no subtree reserved key applied (#3341)", got, want)
	}
	if fs := cfg.resolveStateFiltersAt(rsInWindow, nil); len(fs) != 1 || fs[0].Tenant != "t1" {
		t.Errorf("state filters = %+v, want crashloop on for t1 (the root's default)", fs)
	}
	if cuts := cfg.ScheduleCuts(); !reflect.DeepEqual(cuts, []int{0}) {
		t.Errorf("ScheduleCuts = %v, want [0]", cuts)
	}
}

// The flat plane's load (LoadDir: the exporter's, da-guard served-values')
// of a tenant file: no window reaches ScheduleCuts.
func TestLoadDir_TenantScheduledReservedIsNoCut(t *testing.T) {
	t.Parallel()
	dir := rsTree(t, map[string]string{
		"t1.yaml": "tenants:\n  t1:\n    _silent_mode: {default: disable, overrides: [{window: \"03:00-04:00\", value: all}]}\n",
	})
	cfg, failed, err := LoadDir(dir, log.New(io.Discard, "", 0))
	if err != nil || len(failed) != 0 {
		t.Fatalf("LoadDir: %v %v", err, failed)
	}
	if cuts := cfg.ScheduleCuts(); !reflect.DeepEqual(cuts, []int{0}) {
		t.Errorf("ScheduleCuts = %v, want [0]", cuts)
	}
	if n := len(cfg.AtMinuteOfDay(3*60).resolveSilentModesAt(rsInWindow, nil)); n != 0 {
		t.Errorf("the 03:00 copy silences %d severities, want 0 (the exporter never did)", n)
	}
}

// The walker's reported config (/effective, `da-guard effective`) shows a
// scheduled reserved value as its default; a mapping key keeps its mapping.
// merged_hash still hashes the merge as written. A subtree's scheduled
// reserved key is not shown at all (#3341): not_served names its file.
func TestResolveEffective_ScheduledReservedShowsDefault(t *testing.T) {
	t.Parallel()
	dir := rsTree(t, map[string]string{
		"finance/_defaults.yaml": "defaults:\n  _severity_dedup: {default: disable, overrides: [{window: \"03:00-04:00\", value: enable}]}\n",
		"finance/t1.yaml": "tenants:\n  t1:\n" +
			"    _silent_mode: {default: disable, overrides: [{window: \"03:00-04:00\", value: all}]}\n" +
			"    _state_crashloop: {default: enable, expires: \"2020-01-01T00:00:00Z\"}\n" +
			"    _routing: {default: x, receiver: {type: webhook, url: \"https://e.example/h\"}}\n",
	})
	ec, err := ResolveEffective(dir, "t1", fixedNow)
	if err != nil {
		t.Fatal(err)
	}
	view := ec.EffectiveConfig
	for k, want := range map[string]any{"_silent_mode": "disable", "_state_crashloop": "enable"} {
		if !reflect.DeepEqual(view[k], want) {
			t.Errorf("effective_config[%s] = %#v, want %#v", k, view[k], want)
		}
	}
	if got, shown := view["_severity_dedup"]; shown {
		t.Errorf("effective_config[_severity_dedup] = %#v, want absent: a subtree does not carry it (#3341)", got)
	}
	if want := (NotServedKey{Reason: NotServedValueRejected, File: "finance/_defaults.yaml"}); ec.NotServed["_severity_dedup"] != want {
		t.Errorf("not_served[_severity_dedup] = %+v, want %+v", ec.NotServed["_severity_dedup"], want)
	}
	if r, ok := view["_routing"].(map[string]any); !ok || r["default"] != "x" || r["receiver"] == nil {
		t.Errorf("effective_config[_routing] = %#v, want the mapping whole", view["_routing"])
	}
	if _, ok := ec.MergedConfig["_silent_mode"].(map[string]any); !ok {
		t.Errorf("MergedConfig[_silent_mode] = %#v: the merge (merged_hash's input) keeps the value as written", ec.MergedConfig["_silent_mode"])
	}
}

// tenant-api's merge: the display (what GET resolves) holds the default
// alone; the tenant layer keeps the schedule for ValidateTenantKeys, so GET
// reports it as a notice. The flat-KV fallback is the same.
func TestMergeTenantOverRootPlatform_ScheduledReserved(t *testing.T) {
	t.Parallel()
	dir := rsTree(t, nil)
	root := LoadRootPlatform(dir)
	const val = "{default: disable, overrides: [{window: \"03:00-04:00\", value: all}]}"
	for name, data := range map[string]string{
		"tenants block": "tenants:\n  t1:\n    _silent_mode: " + val + "\n",
		"flat KV":       "_silent_mode: " + val + "\n",
	} {
		t.Run(name, func(t *testing.T) {
			t.Parallel()
			m := MergeTenantOverRootPlatform(root, "t1", []byte(data))
			if sv := m.Tenants["t1"]["_silent_mode"]; !reflect.DeepEqual(sv, ScheduledValue{Default: "disable"}) {
				t.Errorf("display _silent_mode = %+v, want the default alone", sv)
			}
			if cuts := m.ScheduleCuts(); !reflect.DeepEqual(cuts, []int{0}) {
				t.Errorf("ScheduleCuts = %v, want [0]", cuts)
			}
			v := m.ValidateTenantKeysByTenant().SelfRatchet().Tenants["t1"]
			if len(v.Errors) != 0 || !strings.Contains(strings.Join(v.Notices, "\n"), "NOTICE: tenant=t1: _silent_mode writes `overrides:`") {
				t.Errorf("GET validation = %+v, want the schedule as a notice", v)
			}
		})
	}
}

// The expiry is compared field by field: a write that changes only
// `expires:` or only `reason:` is a change and still blocks; the same pair
// is a notice.
func TestRatchet_ScheduledReservedExpiry(t *testing.T) {
	t.Parallel()
	val := func(expires, reason string) string {
		return rsDoc("_silent_mode", fmt.Sprintf(`{default: disable, expires: %q, reason: %q}`, expires, reason))
	}
	prev := PriorValues(gateDecode(t, val("2099-01-01T00:00:00Z", "r1")).Tenants)
	for _, c := range []struct {
		name, body string
		blocks     bool
	}{
		{"unchanged", val("2099-01-01T00:00:00Z", "r1"), false},
		{"expires only", val("2099-02-01T00:00:00Z", "r1"), true},
		{"reason only", val("2099-01-01T00:00:00Z", "r2"), true},
	} {
		t.Run(c.name, func(t *testing.T) {
			t.Parallel()
			got := gateDecode(t, c.body).ValidateTenantKeys().Ratchet(prev)
			if blocked := len(got.Errors) == 1 && strings.Contains(got.Errors[0], "_silent_mode writes `expires:` and `reason:`"); blocked != c.blocks {
				t.Errorf("errors %q notices %q, want blocking=%v", got.Errors, got.Notices, c.blocks)
			}
		})
	}
}
