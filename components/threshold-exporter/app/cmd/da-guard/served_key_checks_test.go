package main

// ADR-038 §11: served-values hands the CI readers, per tenant, the two key
// checks they print and no longer judge themselves — ValidateTenantKeys'
// records on a `version` label (ADR-024 OQ-6) and on a retired spelling
// (#1231) — and nothing else of that judgement (whether a key is read is the
// shared key verdict's). #3703: as structured records whose only fields
// carrying the author's text are key and head. Tenant ids are fixture names
// (CLAUDE.md #9).

import (
	"bytes"
	"encoding/json"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func servedKeyChecksFor(t *testing.T, files map[string]string) map[string]servedKeyChecks {
	t.Helper()
	var out, errOut bytes.Buffer
	if c := runServedValues([]string{"--config-dir", writeTree(t, files)}, &out, &errOut); c != exitOK {
		t.Fatalf("served-values exit %d: %s", c, errOut.String())
	}
	var doc struct {
		Tenants map[string]struct {
			KeyChecks *servedKeyChecks `json:"key_checks"`
		} `json:"tenants"`
	}
	if err := json.Unmarshal(out.Bytes(), &doc); err != nil {
		t.Fatal(err)
	}
	got := map[string]servedKeyChecks{}
	for tid, tv := range doc.Tenants {
		if tv.KeyChecks == nil {
			t.Fatalf("tenant %s has no key_checks (always present):\n%s", tid, out.String())
		}
		got[tid] = *tv.KeyChecks
	}
	return got
}

func TestServedValues_KeyChecks(t *testing.T) {
	t.Parallel()
	got := servedKeyChecksFor(t, map[string]string{
		"_defaults.yaml": "defaults:\n  container_cpu: 80\n  mysql_threads_running: 30\n",
		"tx.yaml": "tenants:\n  tx:\n" +
			"    mysql_cpu: \"40\"\n" + // retired spelling
			"    container_cpu{version=\"V2!\"}: \"5\"\n" + // a label the exporter refuses
			"    container_cpu{version=\"v2\"}: \"6\"\n" + // a good label: no check
			"    mysql_conections: \"5\"\n", // unread: the verdict's, not a key check
		"ty.yaml": "tenants:\n  ty:\n    container_cpu: \"70\"\n",
	})
	renamedTo := "mysql_threads_running"
	want := servedKeyChecks{
		{Kind: string(config.KeyCheckVersionCharset), Key: `container_cpu{version="V2!"}`, Head: "container_cpu",
			Reason: config.KeyCheck{Kind: config.KeyCheckVersionCharset}.Reason()},
		{Kind: string(config.KeyCheckRenamed), Key: "mysql_cpu", Head: "mysql_cpu", ReplacementHead: &renamedTo,
			Reason: config.KeyCheck{Kind: config.KeyCheckRenamed}.Reason()},
	}
	if !reflect.DeepEqual(got["tx"], want) {
		t.Errorf("tx key_checks = %+v, want %+v", got["tx"], want)
	}
	// Always present, [] when none: a reader tells "none" from "not given".
	if ty, ok := got["ty"]; !ok || ty == nil || len(ty) != 0 {
		t.Errorf("ty key_checks = %#v, want []", ty)
	}
}

// servedKeyCheckFixtures: per kind, a tenant's keys that cause it, each with
// a canary in the metric name (an unregistered one, where the kind allows),
// a label name and a label value.
var servedKeyCheckFixtures = map[config.KeyCheckKind]string{
	config.KeyCheckVersionRegex:               `'canary_metric_a{canary_lbl="CaNaRy-v",version=~"CANARY-re.*"}': "1"`,
	config.KeyCheckVersionEmpty:               `'canary_metric_b{canary_lbl="CaNaRy-v",version=""}': "1"`,
	config.KeyCheckVersionDefault:             `'canary_metric_c{canary_lbl="CaNaRy-v",version="default"}': "1"`,
	config.KeyCheckVersionCharset:             `'canary_metric_d{canary_lbl="x",version="CANARY https://u:PW@hooks.example/x"}': "1"`,
	config.KeyCheckRenamedShadowed:            "'mysql_cpu{canary_lbl=\"CaNaRy-s\"}': \"1\"\n    'mysql_threads_running{canary_lbl=\"CaNaRy-s\"}': \"2\"",
	config.KeyCheckRenamedCriticalDimensional: `'mysql_cpu_critical{canary_lbl="CaNaRy-c"}': "1"`,
	config.KeyCheckRenamed:                    `'mysql_cpu{canary_lbl="CaNaRy-r"}': "1"`,
}

// TestServedValues_KeyChecksCarryNoAuthorTextButKeyAndHead (#3703): for
// every kind of config.KeyCheckKinds — a kind without a fixture fails — the
// record is in key_checks, and none of its fields but key and head carries
// the canary: they are what the route generator prints as they are.
func TestServedValues_KeyChecksCarryNoAuthorTextButKeyAndHead(t *testing.T) {
	t.Parallel()
	kinds := config.KeyCheckKinds()
	for kind := range servedKeyCheckFixtures {
		if !slices.Contains(kinds, kind) {
			t.Errorf("fixture for %q, which config.KeyCheckKinds does not list", kind)
		}
	}
	for _, kind := range kinds {
		fx, ok := servedKeyCheckFixtures[kind]
		if !ok {
			t.Errorf("kind %q has no fixture in servedKeyCheckFixtures", kind)
			continue
		}
		t.Run(string(kind), func(t *testing.T) {
			t.Parallel()
			got := servedKeyChecksFor(t, map[string]string{
				"_defaults.yaml": "defaults:\n  container_cpu: 80\n  mysql_threads_running: 70\n" +
					"  mysql_threads_running_critical: 90\n",
				"t.yaml": "tenants:\n  t-kc:\n    " + fx + "\n",
			})["t-kc"]
			if len(got) != 1 || got[0].Kind != string(kind) {
				t.Fatalf("key_checks %+v, want one %s", got, kind)
			}
			if !strings.Contains(strings.ToLower(got[0].Key), "canary") {
				t.Errorf("the fixture's key carries no canary: %q", got[0].Key)
			}
			raw, err := json.Marshal(got[0])
			if err != nil {
				t.Fatal(err)
			}
			var fields map[string]any
			if err := json.Unmarshal(raw, &fields); err != nil {
				t.Fatal(err)
			}
			for name, v := range fields {
				if name == "key" || name == "head" {
					continue
				}
				s, _ := json.Marshal(v)
				if strings.Contains(strings.ToLower(string(s)), "canary") {
					t.Errorf("field %s carries the author's text: %s", name, s)
				}
			}
			if (fields["replacement_head"] == nil) == kind.IsRenamed() {
				t.Errorf("replacement_head %v on kind %s", fields["replacement_head"], kind)
			}
		})
	}
}
