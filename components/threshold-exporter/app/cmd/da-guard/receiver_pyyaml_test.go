package main

import (
	"strings"
	"testing"
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
