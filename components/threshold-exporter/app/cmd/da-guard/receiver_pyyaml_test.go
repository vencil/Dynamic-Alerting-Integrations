package main

// receiver_pyyaml_test.go — #2295 / #2431 / #2960: the receivers as PyYAML
// reads them. Before #2904 the CLI re-read each tenant file the way the route
// generator's PyYAML does and judged the receivers on that reading. Since
// #2904 (ADR-036 §9 step 2) the generator judges them itself — it IS that
// reading — and the CLI judges no routing. Each tree below is the pre-#2904
// fixture, and each test's comment says what it probed then; the run must
// now be undetermined (exit 4), with no finding at all.

import (
	"strings"
	"testing"
)

// assertReceiverTreeNotJudged: the tree holds only routing-plane problems,
// so the run is exit 4 with no finding and with evidence.
func assertReceiverTreeNotJudged(t *testing.T, label string, files map[string]string) {
	t.Helper()
	got := runJudged(t, files, "")
	if got.code != exitUndetermined || len(got.findings) != 0 || len(got.undetermined) == 0 {
		t.Errorf("%s: exit %d, findings %v, undetermined %v; want 4, none, some", label, got.code, got.findings, got.undetermined)
	}
}

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
			assertReceiverTreeNotJudged(t, name+" token "+tc.token, build(tc.token))
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
			assertReceiverTreeNotJudged(t, s.name+" token "+tc.token,
				map[string]string{"_defaults.yaml": rsDefaults, "tx.yaml": s.tenant(tc.token)})
		}
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
		// #2960: the `1:` itself is a key the generator does not read.
		{"platform 1: beside _routing, good receiver", map[string]string{"tx.yaml": rsTenant,
			"_platform.yaml": "tenants:\n  tx:\n    _routing:\n      1: x\n      receiver: " + good + "\n"}, 1},
		{"platform 1: in tenant body, good receiver", map[string]string{"tx.yaml": rsTenant,
			"_platform.yaml": "tenants:\n  tx:\n    1: x\n    _routing:\n      receiver: " + good + "\n"}, 0},
	} {
		tc.files["_defaults.yaml"] = rsDefaults
		_ = tc.want // the pre-#2904 exit, kept as the fixture's record
		assertReceiverTreeNotJudged(t, tc.name, tc.files)
	}
}

// TestRun_IgnoredReceiverKeyNamedAsWritten (#2960 review): a receiver key is
// named as the file writes it — `on`, not its PyYAML value `true` — because
// the check reads the block before the receivers are swapped for PyYAML's.
func TestRun_IgnoredReceiverKeyNamedAsWritten(t *testing.T) {
	t.Parallel()
	tenant := rsTenant + "    _routing:\n      receiver:\n        type: webhook\n        url: https://t.example/h\n        on: x\n"
	assertReceiverTreeNotJudged(t, "receiver key on:", map[string]string{"_defaults.yaml": rsDefaults, "tx.yaml": tenant})
}
