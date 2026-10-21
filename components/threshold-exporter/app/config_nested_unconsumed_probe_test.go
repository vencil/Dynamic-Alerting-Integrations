package main

// config_nested_unconsumed_probe_test.go — #2439, revised by ADR-036 step 6
// (#3206): a nested `_` file is judged by parse-tree's verdict.
//
// The flat plane's nested probe (pkg/config reportUnparseableNestedPlatformFile)
// used to decode every nested `_*.yaml` into `any`; #2439 narrowed it to the
// YAML parser for a non-carrier (a `!!null x` the route generator's PyYAML
// read fine was not a parse failure). Since step 6 the route generator reads
// every nested `_` file through `da-guard parse-tree`, which refuses such a
// tag (decode_error) and a repeated key (duplicate_key), and refuses the
// whole tree on it; the probe asks parse-tree first, so those files are a
// parse failure again — counted (with the kind), ERROR-logged and in
// LoadDir's parseFailed (da-guard exit 3) — like a syntax error. A file
// parse-tree reads is not one, whatever the file.

import (
	"path"
	"strings"
	"testing"

	promtest "github.com/prometheus/client_golang/prometheus/testutil"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func TestNestedPlatformFileProbe_OnlyTheCarrierIsJudgedByItsDecode(t *testing.T) {
	t.Parallel()
	const (
		tagMismatch = "note: !!null x\n"   // parses; the decode into `any` refuses the tag
		syntaxError = "note: [1,\n"        // the parser refuses it
		dupKey      = "note: 1\nnote: 2\n" // parses; the decode into `any` refuses the repeat
		healthy     = "defaults:\n  mysql_connections: 60\n"
	)
	const readable = "note: 1\n" // parse-tree reads it
	cases := []struct {
		rel, body, label string
		kind             string // "" = not a parse failure; else counted, ERROR-logged, in LoadDir's parseFailed, with this kind
	}{
		// parse-tree's refusals, whatever the nested `_` file (#3206).
		{"team/_domain_policy.yaml", tagMismatch, "tag", "decode_error"},
		{"team/_routing_profiles.yaml", tagMismatch, "tag", "decode_error"},
		{"team/_profiles.yaml", tagMismatch, "tag", "decode_error"},
		{"team/_notes.yaml", tagMismatch, "tag", "decode_error"},
		{"team/_notes.yaml", dupKey, "dupkey", "duplicate_key"},
		{"team/_defaults.yaml", healthy + tagMismatch, "tag", "decode_error"},
		{"team/_defaults.yaml", healthy + dupKey, "dupkey", "duplicate_key"},
		{"team/_domain_policy.yaml", syntaxError, "syntax", "syntax_error"},
		{"team/_routing_profiles.yaml", syntaxError, "syntax", "syntax_error"},
		{"team/_profiles.yaml", syntaxError, "syntax", "syntax_error"},
		{"team/_notes.yaml", syntaxError, "syntax", "syntax_error"},
		{"team/_defaults.yaml", syntaxError, "syntax", "syntax_error"},
		// Controls: what parse-tree reads is not a parse failure.
		{"team/_notes.yaml", readable, "readable", ""},
		{"team/_profiles.yaml", readable, "readable", ""},
		{"team/_defaults.yaml", readable, "readable", ""},
	}
	for _, tc := range cases {
		tc := tc
		t.Run(tc.rel+"/"+tc.label, func(t *testing.T) {
			t.Parallel()
			root := writeColdMergeFixture(t, map[string]string{
				"_defaults.yaml":   "defaults:\n  mysql_connections: 80\n",
				"t-root.yaml":      "tenants:\n  t-root: {}\n",
				"team/t-team.yaml": "tenants:\n  t-team: {}\n",
				tc.rel:             tc.body,
			})
			base := path.Base(tc.rel)

			fresh, _ := freshMetrics(t)
			logger, buf := newTestLogger()
			mgr := NewConfigManager(root)
			defer mgr.Close()
			mgr.SetMetrics(fresh)
			mgr.SetLogger(logger)
			if err := mgr.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			count := promtest.ToFloat64(fresh.parseFailures.WithLabelValues(base))
			loud := strings.Contains(buf.String(), "ERROR: skip unparseable")

			_, dropped, err := config.LoadDir(root, nil)
			if err != nil {
				t.Fatalf("LoadDir: %v", err)
			}
			inDropped := false
			for _, k := range dropped {
				if k == tc.rel {
					inDropped = true
				}
			}

			if tc.kind != "" {
				refused := promtest.ToFloat64(fresh.parseRefused.WithLabelValues(base, tc.kind))
				if count == 0 || refused != count || !loud || !inDropped ||
					!strings.Contains(buf.String(), "(kind "+tc.kind+")") {
					t.Errorf("want a parse failure of kind %s: parse_failure{%s} = %v, parse_refused{%s} = %v, ERROR logged = %v, in LoadDir parseFailed = %v (%v); log:\n%s",
						tc.kind, base, count, tc.kind, refused, loud, inDropped, dropped, buf.String())
				}
				return
			}
			if count != 0 || loud || inDropped || len(dropped) != 0 {
				t.Errorf("a file parse-tree reads must not be a parse failure: parse_failure{%s} = %v, ERROR logged = %v, LoadDir parseFailed = %v; log:\n%s",
					base, count, loud, dropped, buf.String())
			}
			// The tree is otherwise served: the subtree tenant keeps the
			// root default, so the file did not take its subtree down.
			if got, ok := seriesFor(t, mgr, "t-team", "connections"); !ok || got != 80 {
				t.Errorf("t-team emits %v (present=%v), want the root default 80", got, ok)
			}
		})
	}
}

// TestNestedPlatformFileProbe_ReadableFileKeepsItsWarnings (#2439 review
// F2): the two WARNs that read a nested non-carrier's top-level keys fire for
// a file parse-tree reads. One it refuses (here a `!!null x` beside them) is
// a parse failure since ADR-036 step 6 (#3206): ERROR, counted with its kind,
// and no WARN about content that is not read.
func TestNestedPlatformFileProbe_ReadableFileKeepsItsWarnings(t *testing.T) {
	t.Parallel()
	const body = "tenants:\n  t-team:\n    mysql_connections: \"1\"\nmax_metrics_per_tenant: 5\n"
	for _, tc := range []struct {
		name, extra string
		refused     bool
	}{
		{"readable", "", false},
		{"refused", "note: !!null x\n", true},
	} {
		tc := tc
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			root := writeColdMergeFixture(t, map[string]string{
				"_defaults.yaml":      "defaults:\n  mysql_connections: 80\n",
				"team/t-team.yaml":    "tenants:\n  t-team: {}\n",
				"team/_platform.yaml": body + tc.extra,
			})
			fresh, _ := freshMetrics(t)
			logger, buf := newTestLogger()
			mgr := NewConfigManager(root)
			defer mgr.Close()
			mgr.SetMetrics(fresh)
			mgr.SetLogger(logger)
			if err := mgr.Load(); err != nil {
				t.Fatalf("Load: %v", err)
			}
			log := buf.String()
			count := promtest.ToFloat64(fresh.parseFailures.WithLabelValues("_platform.yaml"))
			refused := promtest.ToFloat64(fresh.parseRefused.WithLabelValues("_platform.yaml", "decode_error"))
			warns := []string{
				`WARN: tenants: block in nested platform file`,
				`(tenants "t-team")`,
				`WARN: max_metrics_per_tenant found in`,
			}
			if tc.refused {
				if count != 1 || refused != 1 || !strings.Contains(log, "ERROR: skip unparseable") {
					t.Errorf("parse_failure = %v, parse_refused{decode_error} = %v, want 1 and 1 with an ERROR; log:\n%s", count, refused, log)
				}
				for _, w := range warns {
					if strings.Contains(log, w) {
						t.Errorf("a refused file still WARNs %q; log:\n%s", w, log)
					}
				}
				return
			}
			if count != 0 || strings.Contains(log, "ERROR: skip unparseable") {
				t.Errorf("parse_failure{_platform.yaml} = %v, want 0 and no ERROR; log:\n%s", count, log)
			}
			for _, w := range warns {
				if !strings.Contains(log, w) {
					t.Errorf("log lacks %q; log:\n%s", w, log)
				}
			}
		})
	}
}
