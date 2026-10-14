package main

import (
	"reflect"
	"testing"

	"github.com/vencil/threshold-exporter/internal/parsetree"
)

// TestRenderedBy_Locations: the table at a glance. The oracle is the renders
// (tests/shared/test_parse_tree_rendered_by.py); this pins each row so a
// change to one shows in the diff of this file too.
func TestRenderedBy_Locations(t *testing.T) {
	t.Parallel()
	all := parsetree.Content{}
	for _, k := range renderedByKeys {
		all[k] = nil
	}
	all["max_metrics_per_tenant"] = nil
	all["nobody_reads_this"] = nil
	e, g, n := []string{readerExporter}, []string{readerGenerator}, []string{}
	both := []string{readerExporter, readerGenerator}
	for _, tc := range []struct {
		rel     string
		carrier bool
		want    map[string][]string
	}{
		{"_defaults.yaml", true, map[string][]string{"defaults": e, "state_filters": e, "optional_overrides": e,
			"profiles": e, "tenants": both, "_routing_defaults": g, "_routing_enforced": g, "routing_profiles": n}},
		{"_platform.yaml", false, map[string][]string{"defaults": n, "state_filters": n, "optional_overrides": n,
			"profiles": e, "tenants": both, "_routing_defaults": g, "_routing_enforced": g, "routing_profiles": n}},
		{"_routing_profiles.yaml", false, map[string][]string{"defaults": n, "state_filters": n, "optional_overrides": n,
			"profiles": e, "tenants": both, "_routing_defaults": g, "_routing_enforced": g, "routing_profiles": g}},
		{"team/_defaults.yaml", true, map[string][]string{"defaults": e, "state_filters": n, "optional_overrides": n,
			"profiles": n, "tenants": n, "_routing_defaults": g, "_routing_enforced": n, "routing_profiles": n}},
		{"team/_routing_profiles.yml", false, map[string][]string{"defaults": n, "state_filters": n, "optional_overrides": n,
			"profiles": n, "tenants": n, "_routing_defaults": n, "_routing_enforced": n, "routing_profiles": g}},
		{"team/_notes.yaml", false, map[string][]string{"defaults": n, "state_filters": n, "optional_overrides": n,
			"profiles": n, "tenants": n, "_routing_defaults": n, "_routing_enforced": n, "routing_profiles": n}},
	} {
		if got := renderedBy(tc.rel, tc.carrier, all); !reflect.DeepEqual(got, tc.want) {
			t.Errorf("%s (carrier=%v):\n got  %v\n want %v", tc.rel, tc.carrier, got, tc.want)
		}
	}
	if got := renderedBy("db-a.yaml", false, all); got != nil {
		t.Errorf("a tenant file has no entry, got %v", got)
	}
	if got := renderedBy("_x.yaml", false, parsetree.Content{"tenants": nil}); !reflect.DeepEqual(got, map[string][]string{"tenants": both}) {
		t.Errorf("only the keys the file carries: %v", got)
	}
}
