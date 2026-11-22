package main

// /metrics half of #4097: tenant_expected_exporter{tenant, pack} is served
// from `_expected_exporters` through the tenant's layers — read off a real
// scrape of the tree, a tenant-only reload included.

import (
	"path/filepath"
	"reflect"
	"sort"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

// scrapeExpectedExporters gathers the collector of m in a pedantic registry
// (a series emitted twice fails it) and returns each tenant's `pack` labels,
// sorted.
func scrapeExpectedExporters(t *testing.T, m *ConfigManager) map[string][]string {
	t.Helper()
	reg := prometheus.NewPedanticRegistry()
	if err := reg.Register(NewThresholdCollector(m)); err != nil {
		t.Fatalf("register: %v", err)
	}
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}
	out := map[string][]string{}
	for _, mf := range mfs {
		if mf.GetName() != "tenant_expected_exporter" {
			continue
		}
		for _, metric := range mf.GetMetric() {
			labels := map[string]string{}
			for _, l := range metric.GetLabel() {
				labels[l.GetName()] = l.GetValue()
			}
			if len(labels) != 2 {
				t.Errorf("tenant_expected_exporter labels = %v, want exactly tenant and pack", labels)
			}
			out[labels["tenant"]] = append(out[labels["tenant"]], labels["pack"])
		}
	}
	for _, kinds := range out {
		sort.Strings(kinds)
	}
	return out
}

func TestExpectedExportersLayersOnMetrics(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeOverlayTree(t, dir, map[string]string{
		"_profiles.yaml": "profiles:\n  web:\n    _expected_exporters: [nginx, jvm]\n",
		"_platform.yaml": "tenants:\n  tplat:\n    _expected_exporters: [kafka]\n  tover:\n    _expected_exporters: [kafka, redis]\n",
		"tplat.yaml":     "tenants:\n  tplat: {}\n",
		"tover.yaml":     "tenants:\n  tover:\n    _expected_exporters: [mariadb]\n",
		"tprof.yaml":     "tenants:\n  tprof:\n    _profile: web\n",
		"tnone.yaml":     "tenants:\n  tnone:\n    _metadata:\n      owner: o\n",
		"told.yaml":      "tenants:\n  told:\n    _metadata:\n      db_type: mariadb\n",
		"tbad.yaml":      "tenants:\n  tbad:\n    _expected_exporters: [mariadb, nope]\n",
	})
	m, _ := newOverlayManager(t, dir)
	want := map[string][]string{
		"tplat": {"kafka"},
		"tover": {"mariadb"},
		"tprof": {"jvm", "nginx"},
	}
	if got := scrapeExpectedExporters(t, m); !reflect.DeepEqual(got, want) {
		t.Errorf("tenant_expected_exporter on /metrics = %v, want %v", got, want)
	}
}

// The incremental reload rebuilds an edited tenant from every declaring
// file; its `_expected_exporters` must come out as a fresh Load reads it.
func TestExpectedExportersSurviveATenantOnlyReload(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeOverlayTree(t, dir, map[string]string{
		"_platform.yaml": "tenants:\n  tx:\n    _expected_exporters: [kafka]\n",
		"ty.yaml":        "tenants:\n  ty:\n    _expected_exporters: [redis]\n",
	})
	tenantFile := filepath.Join(dir, "tx.yaml")
	writeTestYAML(t, tenantFile, "tenants:\n  tx:\n    redis_x: \"1\"\n")
	m, _ := newOverlayManager(t, dir)
	requireFlatWatchPath(t, m)
	if got := scrapeExpectedExporters(t, m)["tx"]; !reflect.DeepEqual(got, []string{"kafka"}) {
		t.Fatalf("premise: tx reads %v before the edit, want the platform entry's [kafka]", got)
	}

	writeTestYAML(t, tenantFile, "tenants:\n  tx:\n    _expected_exporters: [nginx, mariadb]\n    redis_x: \"2\"\n")
	if err := watchReload(m); err != nil {
		t.Fatalf("reload: %v", err)
	}
	reloaded := scrapeExpectedExporters(t, m)
	want := map[string][]string{"tx": {"mariadb", "nginx"}, "ty": {"redis"}}
	if !reflect.DeepEqual(reloaded, want) {
		t.Errorf("after a tenant-only reload = %v, want %v", reloaded, want)
	}
	ref, _ := newOverlayManager(t, dir)
	if full := scrapeExpectedExporters(t, ref); !reflect.DeepEqual(reloaded, full) {
		t.Errorf("tenant-only reload diverged from a fresh Load\nreload=%v\nfull=  %v", reloaded, full)
	}
}
