package config

// #3703: a KeyCheck's two renderings. Message is what KeyValidation carries
// and must stay byte for byte what it was before the record existed (the
// exporter's log and tenant-api read it); Reason is what da-guard hands the
// route generator and must carry nothing the author wrote. The fixtures put
// a canary in the metric name (an unregistered one, where the kind allows),
// a label name and a label value. Tenant ids are fixture names (CLAUDE.md #9).

import (
	"strings"
	"testing"
)

// keyCheckCanary is the text every fixture key carries; no Reason may.
const keyCheckCanary = "canary"

// keyCheckFixture is one kind's tenant map and the Message that kind gave
// before KeyCheck existed (the literal the old fmt.Sprintf produced).
type keyCheckFixture struct {
	keys    map[string]string
	key     string // the key the check is on, as written
	head    string
	replace string // ReplacementHead
	message string
}

var keyCheckFixtures = map[KeyCheckKind]keyCheckFixture{
	KeyCheckVersionRegex: {
		keys: map[string]string{`canary_metric_a{canary_lbl="CaNaRy-v",version=~"CANARY-re.*"}`: "1"},
		key:  `canary_metric_a{canary_lbl="CaNaRy-v",version=~"CANARY-re.*"}`, head: "canary_metric_a",
		message: `WARN: tenant=t-kc: regex version matcher "CANARY-re.*" in key ` +
			`"canary_metric_a{canary_lbl=\"CaNaRy-v\",version=~\"CANARY-re.*\"}" — ADR-024 Phase 1 ` +
			`expects an exact version="..." selector`,
	},
	KeyCheckVersionEmpty: {
		keys: map[string]string{`canary_metric_b{canary_lbl="CaNaRy-v",version=""}`: "1"},
		key:  `canary_metric_b{canary_lbl="CaNaRy-v",version=""}`, head: "canary_metric_b",
		message: `WARN: tenant=t-kc: empty version label in key ` +
			`"canary_metric_b{canary_lbl=\"CaNaRy-v\",version=\"\"}" (ADR-024 OQ-6 forbids empty — ` +
			`it collides with the unversioned baseline)`,
	},
	KeyCheckVersionDefault: {
		keys: map[string]string{`canary_metric_c{canary_lbl="CaNaRy-v",version="default"}`: "1"},
		key:  `canary_metric_c{canary_lbl="CaNaRy-v",version="default"}`, head: "canary_metric_c",
		message: `WARN: tenant=t-kc: literal version="default" in key ` +
			`"canary_metric_c{canary_lbl=\"CaNaRy-v\",version=\"default\"}" is reserved for the ` +
			`normalize-layer fallback (ADR-024 OQ-6)`,
	},
	KeyCheckVersionCharset: {
		keys: map[string]string{`canary_metric_d{canary_lbl="x",version="CANARY https://u:PW@hooks.example/x"}`: "1"},
		key:  `canary_metric_d{canary_lbl="x",version="CANARY https://u:PW@hooks.example/x"}`, head: "canary_metric_d",
		message: `WARN: tenant=t-kc: version "CANARY https://u:PW@hooks.example/x" in key ` +
			`"canary_metric_d{canary_lbl=\"x\",version=\"CANARY https://u:PW@hooks.example/x\"}" violates ` +
			`^[a-z0-9][a-z0-9._-]*$ (ADR-024 OQ-6; pilot-calibratable)`,
	},
	KeyCheckRenamedShadowed: {
		keys: map[string]string{`mysql_cpu{canary_lbl="CaNaRy-s"}`: "1",
			`mysql_threads_running{canary_lbl="CaNaRy-s"}`: "2"},
		key: `mysql_cpu{canary_lbl="CaNaRy-s"}`, head: "mysql_cpu", replace: "mysql_threads_running",
		message: `NOTICE: tenant=t-kc: deprecated key "mysql_cpu{canary_lbl=\"CaNaRy-s\"}" is ignored ` +
			`because its replacement "mysql_threads_running{canary_lbl=\"CaNaRy-s\"}" is also set — remove ` +
			`"mysql_cpu{canary_lbl=\"CaNaRy-s\"}" (#1231 rename)`,
	},
	KeyCheckRenamedCriticalDimensional: {
		keys: map[string]string{`mysql_cpu_critical{canary_lbl="CaNaRy-c"}`: "1"},
		key:  `mysql_cpu_critical{canary_lbl="CaNaRy-c"}`, head: "mysql_cpu_critical",
		replace: "mysql_threads_running_critical",
		message: `NOTICE: tenant=t-kc: key "mysql_cpu_critical{canary_lbl=\"CaNaRy-c\"}" was renamed to ` +
			`"mysql_threads_running_critical{canary_lbl=\"CaNaRy-c\"}" (#1231), but a ` + "`_critical`" +
			` tier takes no dimensions and neither spelling is served (#3556); write it as ` +
			`mysql_threads_running{canary_lbl="CaNaRy-c"}: "N:critical", its labels declared for ` +
			`mysql_threads_running under the root ` + "`dimensions:`",
	},
	KeyCheckRenamed: {
		keys: map[string]string{`mysql_cpu{canary_lbl="CaNaRy-r"}`: "1"},
		key:  `mysql_cpu{canary_lbl="CaNaRy-r"}`, head: "mysql_cpu", replace: "mysql_threads_running",
		message: `NOTICE: tenant=t-kc: key "mysql_cpu{canary_lbl=\"CaNaRy-r\"}" was renamed to ` +
			`"mysql_threads_running{canary_lbl=\"CaNaRy-r\"}" (#1231) — the old name still resolves during ` +
			`the 2-release transition window; please update this override to ` +
			`"mysql_threads_running{canary_lbl=\"CaNaRy-r\"}"`,
	},
}

func keyCheckConfig(keys map[string]string) *ThresholdConfig {
	m := map[string]ScheduledValue{}
	for k, v := range keys {
		m[k] = ScheduledValue{Default: v}
	}
	return &ThresholdConfig{
		Defaults: map[string]float64{"container_cpu": 80, "mysql_threads_running": 70,
			"mysql_threads_running_critical": 90},
		Tenants: map[string]map[string]ScheduledValue{"t-kc": m},
	}
}

func hasCanary(s string) bool { return strings.Contains(strings.ToLower(s), keyCheckCanary) }

// TestKeyCheckEveryKind: every kind of KeyCheckKinds has a fixture, a
// Reason and a Message, and nothing else is a kind.
func TestKeyCheckEveryKind(t *testing.T) {
	t.Parallel()
	seen := map[KeyCheckKind]bool{}
	for _, k := range KeyCheckKinds() {
		if seen[k] {
			t.Errorf("kind %q listed twice", k)
		}
		seen[k] = true
		if _, ok := keyCheckFixtures[k]; !ok {
			t.Errorf("kind %q has no fixture in keyCheckFixtures", k)
		}
		if _, ok := keyCheckReasons[k]; !ok {
			t.Errorf("kind %q has no reason", k)
		}
	}
	for k := range keyCheckFixtures {
		if !seen[k] {
			t.Errorf("fixture for %q, which KeyCheckKinds does not list", k)
		}
	}
	if len(keyCheckReasons) != len(seen) {
		t.Errorf("keyCheckReasons has %d entries, KeyCheckKinds %d", len(keyCheckReasons), len(seen))
	}
}

// TestKeyCheck_RenderingsPerKind: per kind, the check is found on the
// fixture key; its Message is the pre-record literal and is the entry
// Errors / Notices carries; its Reason and ReplacementHead carry no canary.
func TestKeyCheck_RenderingsPerKind(t *testing.T) {
	t.Parallel()
	for _, kind := range KeyCheckKinds() {
		fx, ok := keyCheckFixtures[kind]
		if !ok {
			continue // TestKeyCheckEveryKind fails it
		}
		t.Run(string(kind), func(t *testing.T) {
			t.Parallel()
			v := keyCheckConfig(fx.keys).ValidateTenantKeysByTenant().Tenants["t-kc"]
			var got []KeyCheck
			for _, c := range v.KeyChecks {
				if c.Kind == kind {
					got = append(got, c)
				}
			}
			if len(got) != 1 || len(v.KeyChecks) != 1 {
				t.Fatalf("key checks %+v, want one %s", v.KeyChecks, kind)
			}
			c := got[0]
			if c.Tenant != "t-kc" || c.Key != fx.key || c.Head != fx.head || c.ReplacementHead != fx.replace {
				t.Errorf("record %+v, want key %q head %q replacement %q", c, fx.key, fx.head, fx.replace)
			}
			if c.Message() != fx.message {
				t.Errorf("Message()\n got %s\nwant %s", c.Message(), fx.message)
			}
			channel := v.Errors
			if kind.IsRenamed() {
				channel = v.Notices
			}
			if len(channel) != 1 || channel[0] != fx.message {
				t.Errorf("Errors %q / Notices %q, want only %q in the %s channel", v.Errors, v.Notices,
					fx.message, map[bool]string{true: "Notices", false: "Errors"}[kind.IsRenamed()])
			}
			for _, s := range []string{c.Reason(), c.ReplacementHead, string(c.Kind)} {
				if hasCanary(s) {
					t.Errorf("a field the generator prints as is carries the author's text: %q", s)
				}
			}
			if strings.Contains(c.Reason(), "%") {
				t.Errorf("Reason has a %% verb: %q", c.Reason())
			}
		})
	}
}
