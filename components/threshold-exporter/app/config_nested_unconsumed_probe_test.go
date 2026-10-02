package main

// config_nested_unconsumed_probe_test.go — #2439: a nested `_` file the
// exporter never consumes is judged by the YAML PARSER alone.
//
// The flat plane's nested probe (pkg/config reportUnparseableNestedPlatformFile)
// used to decode every nested `_*.yaml` into `any`, so a value with a tag
// yaml.v3 refuses (`!!null x`, which PyYAML and the route generator read
// fine) raised da_config_parse_failure_total — the platform alert's input —
// and landed in LoadDir's parseFailed (da-guard exit 3, served-values'
// parse_failed) for a file no plane of the exporter reads. Below the root
// only the defaults carrier is read (the subtree chain decodes it into
// `any`), so that one keeps the decode verdict; for every other nested `_`
// file only syntax damage counts.

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
	cases := []struct {
		rel, body, label string
		failed           bool // counted, ERROR-logged and in LoadDir's parseFailed
	}{
		{"team/_domain_policy.yaml", tagMismatch, "tag", false},
		{"team/_routing_profiles.yaml", tagMismatch, "tag", false},
		{"team/_profiles.yaml", tagMismatch, "tag", false},
		{"team/_notes.yaml", tagMismatch, "tag", false},
		{"team/_notes.yaml", dupKey, "dupkey", false},
		// The carrier is read: its decode failure drops the subtree's
		// defaults for real, so it stays a parse failure.
		{"team/_defaults.yaml", healthy + tagMismatch, "tag", true},
		{"team/_defaults.yaml", healthy + dupKey, "dupkey", true},
		// Syntax damage stays loud whatever the file.
		{"team/_domain_policy.yaml", syntaxError, "syntax", true},
		{"team/_routing_profiles.yaml", syntaxError, "syntax", true},
		{"team/_profiles.yaml", syntaxError, "syntax", true},
		{"team/_notes.yaml", syntaxError, "syntax", true},
		{"team/_defaults.yaml", syntaxError, "syntax", true},
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

			if tc.failed {
				if count == 0 || !loud || !inDropped {
					t.Errorf("want a parse failure: parse_failure{%s} = %v, ERROR logged = %v, in LoadDir parseFailed = %v (%v); log:\n%s",
						base, count, loud, inDropped, dropped, buf.String())
				}
				return
			}
			if count != 0 || loud || inDropped || len(dropped) != 0 {
				t.Errorf("a nested file the exporter never reads must not be a parse failure: parse_failure{%s} = %v, ERROR logged = %v, LoadDir parseFailed = %v; log:\n%s",
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
