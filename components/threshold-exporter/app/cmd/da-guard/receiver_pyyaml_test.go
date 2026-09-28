package main

import (
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
)

// TestRun_ReceiverReadAsPyYAMLReadsIt (#2295): in every layer the route
// generator merges — the tenant file, the root platform overlay, a routing
// profile, `_routing_defaults` — a receiver string field written as plain
// `on` / `1:30` is what PyYAML reads (a boolean, an integer: the generator
// skips the receiver), so da-guard refuses it; quoted, it is the string the
// generator takes, so da-guard accepts it.
func TestRun_ReceiverReadAsPyYAMLReadsIt(t *testing.T) {
	t.Parallel()
	// recv is a routing body indented at depth `ind`: a main receiver and an
	// override's, plus (routes=true) a routes entry's.
	recv := func(ind int, token string, routes bool) string {
		p := strings.Repeat(" ", ind)
		out := "\n" + p + "receiver:\n" + p + "  type: webhook\n" + p + "  url: https://t.example/h\n" +
			p + "  http_config:\n" + p + "    bearer_token: " + token + "\n" +
			p + "overrides:\n" + p + "- alertname: X\n" +
			p + "  receiver: {type: webhook, url: https://t.example/o, http_config: {bearer_token_file: " + token + "}}\n"
		if routes {
			out += p + "routes:\n" + p + "- match: {severity: critical}\n" +
				p + "  receiver: {type: webhook, url: https://t.example/r, http_config: {bearer_token: " + token + "}}\n"
		}
		return out
	}
	layers := map[string]func(token string) map[string]string{
		"tenant-file": func(tok string) map[string]string {
			return map[string]string{"_defaults.yaml": rsDefaults, "tx.yaml": rsTenant + "    _routing:" + recv(6, tok, true)}
		},
		"platform-overlay": func(tok string) map[string]string {
			return map[string]string{"_defaults.yaml": rsDefaults, "tx.yaml": rsTenant,
				"_platform.yaml": "tenants:\n  tx:\n    _routing:" + recv(6, tok, true)}
		},
		"routing-profile": func(tok string) map[string]string {
			return map[string]string{"_defaults.yaml": rsDefaults, "tx.yaml": rsTenant + "    _routing_profile: team\n",
				"_routing_profiles.yaml": "routing_profiles:\n  team:" + recv(4, tok, true)}
		},
		"routing-defaults": func(tok string) map[string]string {
			// `routes` do not belong in _routing_defaults (a finding of their own).
			return map[string]string{"_defaults.yaml": rsDefaults + "_routing_defaults:" + recv(2, tok, false), "tx.yaml": rsTenant}
		},
	}
	for name, build := range layers {
		for _, tc := range []struct {
			token string
			bad   bool
		}{
			{"on", true}, {"1:30", true}, {`"on"`, false}, {"'1:30'", false}, {`"2024-01-01"`, false},
		} {
			code, got := runRoutingTree(t, build(tc.token), "")
			var bad []string
			for _, f := range got {
				if strings.HasPrefix(f, "error invalid_receiver_field tx ") {
					bad = append(bad, f)
				}
			}
			want := 0
			if tc.bad {
				want = 3
				if name == "routing-defaults" {
					want = 2
				}
			}
			if len(bad) != want || (want == 0) != (code == 0) {
				t.Errorf("%s token %s: exit %d, invalid_receiver_field %v; want %d of them", name, tc.token, code, got, want)
			}
		}
	}
}

// TestRun_ReceiverReadBesideNonStringKeys (#2295 review): a key PyYAML reads
// as a non-string — `on:` (a boolean), `1:` (an integer), `~:` (null), an
// alias to one — beside the receiver, or an alias standing for the tenant
// id, `_routing` or `tenants`, must not make da-guard fall back to the
// yaml.v3 reading, where plain `on` is a string. Plain `on` is refused, as
// the route generator refuses it; quoted "on" is taken, as it takes it (the
// generator accepts these keys, run against each shape).
func TestRun_ReceiverReadBesideNonStringKeys(t *testing.T) {
	t.Parallel()
	recv := func(ind int, tok string) string {
		p := strings.Repeat(" ", ind)
		return "\n" + p + "receiver:\n" + p + "  type: webhook\n" + p + "  url: https://t.example/h\n" +
			p + "  http_config:\n" + p + "    bearer_token: " + tok + "\n"
	}
	const body = "    mysql_connections: \"50\"\n"
	shapes := []struct {
		name, field string
		tenant      func(tok string) string
	}{
		{"routing on: key", "receiver", func(tok string) string { return rsTenant + "    _routing:\n      on: x" + recv(6, tok) }},
		{"routing 1: key", "receiver", func(tok string) string { return rsTenant + "    _routing:\n      1: x" + recv(6, tok) }},
		{"routing ~: key", "receiver", func(tok string) string { return rsTenant + "    _routing:\n      ~: x" + recv(6, tok) }},
		{"routing alias key", "receiver", func(tok string) string {
			return "k: &k on\n" + rsTenant + "    _routing:\n      *k : x" + recv(6, tok)
		}},
		{"override 1: key", "overrides[0].receiver", func(tok string) string {
			return rsTenant + "    _routing:" + rsOKRoute + "      overrides:\n      - alertname: X\n        1: y" + recv(8, tok)
		}},
		{"tenant id alias", "receiver", func(tok string) string {
			return "k: &t tx\ntenants:\n  *t :\n" + body + "    _routing:" + recv(6, tok)
		}},
		{"_routing alias", "receiver", func(tok string) string {
			return "k: &r _routing\n" + rsTenant + "    *r :" + recv(6, tok)
		}},
		{"tenants alias", "receiver", func(tok string) string {
			return "k: &ts tenants\n*ts :\n  tx:\n" + body + "    _routing:" + recv(6, tok)
		}},
	}
	for _, s := range shapes {
		for _, tc := range []struct {
			token string
			bad   bool
		}{{"on", true}, {`"on"`, false}} {
			code, got := runRoutingTree(t, map[string]string{"_defaults.yaml": rsDefaults, "tx.yaml": s.tenant(tc.token)}, "")
			var bad []string
			for _, f := range got {
				if strings.HasPrefix(f, "error invalid_receiver_field tx ") || strings.HasPrefix(f, "error missing_receiver_field tx ") {
					bad = append(bad, f)
				}
			}
			want := []string(nil)
			if tc.bad {
				want = []string{"error invalid_receiver_field tx " + s.field + ".http_config.bearer_token"}
			}
			if strings.Join(bad, "|") != strings.Join(want, "|") || tc.bad == (code == 0) {
				t.Errorf("%s token %s: exit %d, findings %v; want receiver findings %v", s.name, tc.token, code, got, want)
			}
		}
	}
}

// TestPyYAMLOwn_FailsClosedWithoutThePyYAMLReading: a tenant whose `_routing`
// has no PyYAML reading (its file unreadable, the tenant not found in it) is
// not judged as yaml.v3 read it — the receiver is routingpolicy.Unmatched,
// which the receiver check refuses.
func TestPyYAMLOwn_FailsClosedWithoutThePyYAMLReading(t *testing.T) {
	t.Parallel()
	ec := &config.EffectiveConfig{TenantID: "tx", SourceFile: "tx.yaml", TenantOverridesRaw: map[string]any{
		"_routing": map[string]any{"receiver": map[string]any{"type": "webhook", "url": "https://t.example/h"}},
	}}
	for name, py := range map[string]map[string]map[string]any{
		"file unreadable":  {"tx.yaml": nil},
		"tenant not found": {"tx.yaml": {"other": map[string]any{}}},
		"file not read":    {},
	} {
		r, _ := pyyamlOwn(ec, py)["_routing"].(map[string]any)
		if r["receiver"] != routingpolicy.Unmatched {
			t.Errorf("%s: receiver = %#v, want routingpolicy.Unmatched", name, r["receiver"])
		}
	}
	if _, ok := ec.TenantOverridesRaw["_routing"].(map[string]any)["receiver"].(map[string]any); !ok {
		t.Error("pyyamlOwn modified ec.TenantOverridesRaw")
	}
}

// TestRun_PlatformIntKey (#2295 review): a `1:` beside `_routing` or in the
// tenant body of the root platform file does not drop the overlay unjudged
// (the route generator judges it). Repeated keys: generator_duplicates_test.go.
func TestRun_PlatformIntKey(t *testing.T) {
	t.Parallel()
	const (
		bad  = "{type: webhook}"
		good = "{type: webhook, url: 'https://t.example/g'}"
		on   = "\n        type: webhook\n        url: https://t.example/h\n        http_config:\n          bearer_token: on\n"
	)
	for _, tc := range []struct {
		name  string
		files map[string]string
		want  int
	}{
		{"platform 1: beside _routing, plain on", map[string]string{"tx.yaml": rsTenant,
			"_platform.yaml": "tenants:\n  tx:\n    _routing:\n      1: x\n      receiver:" + on}, 1},
		{"platform 1: beside _routing, bad receiver", map[string]string{"tx.yaml": rsTenant,
			"_platform.yaml": "tenants:\n  tx:\n    _routing:\n      1: x\n      receiver: " + bad + "\n"}, 1},
		{"platform 1: in tenant body, bad receiver", map[string]string{"tx.yaml": rsTenant,
			"_platform.yaml": "tenants:\n  tx:\n    1: x\n    _routing:\n      receiver: " + bad + "\n"}, 1},
		{"platform 1: beside _routing, good receiver", map[string]string{"tx.yaml": rsTenant,
			"_platform.yaml": "tenants:\n  tx:\n    _routing:\n      1: x\n      receiver: " + good + "\n"}, 0},
	} {
		tc.files["_defaults.yaml"] = rsDefaults
		if code, got := runRoutingTree(t, tc.files, ""); code != tc.want {
			t.Errorf("%s: exit %d, findings %v; want exit %d", tc.name, code, got, tc.want)
		}
	}
}
