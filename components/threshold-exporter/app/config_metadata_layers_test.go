package main

// /metrics half of #2370: a tenant's `_metadata` is the per-key merge of the
// root platform files' `tenants.<id>._metadata` (merge order) and the tenant
// file's own, the tenant file winning a key both write.
//
// Read off a real scrape (tenant_metadata_info's owner / tier labels,
// tenant_expected_exporter's db_type) plus ResolveMetadata for the fields
// that are not labels (environment, domain), which da-guard's served values
// report from the same resolve.

import (
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/prometheus/client_golang/prometheus"
)

// metadataReading is one tenant's metadata as the exporter serves it.
type metadataReading struct {
	Owner, Tier, DBType string // scraped labels
	Environment, Domain string // ResolveMetadata
}

// scrapeMetadata gathers the collector of m in a pedantic registry and
// returns every tenant's reading.
func scrapeMetadata(t *testing.T, m *ConfigManager) map[string]metadataReading {
	t.Helper()
	reg := prometheus.NewPedanticRegistry()
	if err := reg.Register(NewThresholdCollector(m)); err != nil {
		t.Fatalf("register: %v", err)
	}
	mfs, err := reg.Gather()
	if err != nil {
		t.Fatalf("Gather: %v", err)
	}
	out := map[string]metadataReading{}
	for _, mf := range mfs {
		name := mf.GetName()
		if name != "tenant_metadata_info" && name != "tenant_expected_exporter" {
			continue
		}
		for _, metric := range mf.GetMetric() {
			labels := map[string]string{}
			for _, l := range metric.GetLabel() {
				labels[l.GetName()] = l.GetValue()
			}
			r := out[labels["tenant"]]
			if name == "tenant_metadata_info" {
				r.Owner, r.Tier = labels["owner"], labels["tier"]
			} else {
				r.DBType = labels["db_type"]
			}
			out[labels["tenant"]] = r
		}
	}
	for _, md := range m.GetConfig().ResolveMetadata() {
		r := out[md.Tenant]
		r.Environment, r.Domain = md.Environment, md.Domain
		out[md.Tenant] = r
	}
	return out
}

// controlTenantFile is a second tenant whose metadata is written only in its
// own file; no tree below gives it a platform entry, so its reading must
// stay exactly this whatever the tree does to tx.
const controlTenantFile = "tenants:\n  ty:\n    _metadata:\n      owner: ty-team\n      db_type: postgresql\n"

var controlTenantReading = metadataReading{Owner: "ty-team", DBType: "postgresql"}

func metadataLayerTrees() []struct {
	name  string
	files map[string]string
	want  metadataReading
} {
	return []struct {
		name  string
		files map[string]string
		want  metadataReading
	}{
		{
			name: "platform layer only",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n      environment: production\n",
				"tx.yaml":        "tenants:\n  tx: {}\n",
			},
			want: metadataReading{Owner: "plat-team", Environment: "production"},
		},
		{
			name: "tenant file only",
			files: map[string]string{
				"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: tx-team\n      tier: gold\n",
			},
			want: metadataReading{Owner: "tx-team", Tier: "gold"},
		},
		{
			name: "different keys in both layers are merged",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      tier: gold\n",
			},
			want: metadataReading{Owner: "plat-team", Tier: "gold"},
		},
		{
			name: "same key in both layers: the tenant file wins",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n      tier: silver\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      owner: tx-team\n",
			},
			want: metadataReading{Owner: "tx-team", Tier: "silver"},
		},
		{
			name: "tenant file writes only db_type, platform writes environment domain owner",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      environment: production\n      domain: finance\n      owner: plat-team\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata:\n      db_type: mariadb\n",
			},
			want: metadataReading{Owner: "plat-team", DBType: "mariadb", Environment: "production", Domain: "finance"},
		},
		{
			name: "two platform files: the later file wins a key both write",
			files: map[string]string{
				"_a.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: a-team\n      tier: silver\n",
				"_b.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: b-team\n      domain: finance\n",
				"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      db_type: mariadb\n",
			},
			want: metadataReading{Owner: "b-team", Tier: "silver", Domain: "finance", DBType: "mariadb"},
		},
		{
			name: "tenant file writes a null _metadata: nothing below survives",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata: null\n",
			},
			want: metadataReading{},
		},
	}
}

func TestMetadataLayersMergePerKeyOnMetrics(t *testing.T) {
	t.Parallel()
	for _, tc := range metadataLayerTrees() {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			files := map[string]string{"ty.yaml": controlTenantFile}
			for k, v := range tc.files {
				files[k] = v
			}
			writeOverlayTree(t, dir, files)
			m, _ := newOverlayManager(t, dir)
			got := scrapeMetadata(t, m)
			if got["tx"] != tc.want {
				t.Errorf("tx metadata on /metrics = %+v, want %+v", got["tx"], tc.want)
			}
			if got["ty"] != controlTenantReading {
				t.Errorf("ty (own file only) metadata on /metrics = %+v, want %+v", got["ty"], controlTenantReading)
			}
		})
	}
}

// TestMetadataLayersSurviveATenantOnlyReload: the incremental reload rebuilds
// an edited tenant from every declaring file (reclaimTenantFrom); its
// `_metadata` must come out as a fresh Load merges it.
func TestMetadataLayersSurviveATenantOnlyReload(t *testing.T) {
	t.Parallel()
	dir := t.TempDir()
	writeOverlayTree(t, dir, map[string]string{
		"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n      environment: production\n",
		"ty.yaml":        controlTenantFile,
	})
	tenantFile := filepath.Join(dir, "tx.yaml")
	writeTestYAML(t, tenantFile, "tenants:\n  tx:\n    _metadata:\n      db_type: mariadb\n    redis_x: \"1\"\n")

	m, _ := newOverlayManager(t, dir)
	requireFlatWatchPath(t, m)

	writeTestYAML(t, tenantFile, "tenants:\n  tx:\n    _metadata:\n      db_type: postgresql\n    redis_x: \"2\"\n")
	if err := watchReload(m); err != nil {
		t.Fatalf("reload: %v", err)
	}
	reloaded := scrapeMetadata(t, m)
	if !strings.Contains(m.GetConfig().Tenants["tx"]["redis_x"].Default, "2") {
		t.Fatalf("premise: the tenant edit did not land, so this run proves nothing")
	}
	want := metadataReading{Owner: "plat-team", DBType: "postgresql", Environment: "production"}
	if reloaded["tx"] != want {
		t.Errorf("tx metadata after a tenant-only reload = %+v, want %+v", reloaded["tx"], want)
	}
	ref, _ := newOverlayManager(t, dir)
	if full := scrapeMetadata(t, ref); !reflect.DeepEqual(reloaded, full) {
		t.Errorf("tenant-only reload diverged from a fresh Load\nreload=%+v\nfull=  %+v", reloaded, full)
	}
}
