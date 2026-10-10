package config

// #2830: MetadataResolver (the tenant-api metadata readers) reads a tenant's `_metadata` as /metrics does. The oracle is
// LoadDir + ResolveMetadata over the same tree; every tree also states the
// expected value, so a tree where both sides read nothing cannot pass by
// agreeing on empty.

import (
	"os"
	"path/filepath"
	"reflect"
	"testing"
)

const metadataProfiles = "profiles:\n  pfin:\n    _metadata:\n      environment: production\n      domain: finance\n      owner: team-p\n"

func tenantMetadataTrees() []struct {
	name  string
	files map[string]string
	want  ResolvedMetadata
} {
	return []struct {
		name  string
		files map[string]string
		want  ResolvedMetadata
	}{
		{
			name: "profile supplies _metadata the tenant file does not write",
			files: map[string]string{
				"_profiles.yaml": metadataProfiles,
				"tx.yaml":        "tenants:\n  tx:\n    _profile: pfin\n",
			},
			want: ResolvedMetadata{Environment: "production", Domain: "finance", Owner: "team-p"},
		},
		{
			name: "string form",
			files: map[string]string{
				"tx.yaml": "tenants:\n  tx:\n    _metadata: \"environment: production\\ndomain: finance\\nowner: team-s\\n\"\n",
			},
			want: ResolvedMetadata{Environment: "production", Domain: "finance", Owner: "team-s"},
		},
		{
			name: "non-string scalar values are read as their text",
			files: map[string]string{
				"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      environment: 123\n      domain: finance\n      owner: 456\n      tier: true\n      tags: [1, b]\n",
			},
			want: ResolvedMetadata{Environment: "123", Domain: "finance", Owner: "456", Tier: "true", Tags: []string{"1", "b"}},
		},
		{
			name: "plain mapping",
			files: map[string]string{
				"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      environment: production\n      domain: finance\n      owner: team-c\n",
			},
			want: ResolvedMetadata{Environment: "production", Domain: "finance", Owner: "team-c"},
		},
		{
			name: "tenant file and profile both write: the tenant file's value is used whole",
			files: map[string]string{
				"_profiles.yaml": metadataProfiles,
				"tx.yaml":        "tenants:\n  tx:\n    _profile: pfin\n    _metadata:\n      owner: team-t\n",
			},
			want: ResolvedMetadata{Owner: "team-t"},
		},
		{
			name: "platform entry writes: the profile's is not used",
			files: map[string]string{
				"_profiles.yaml": metadataProfiles,
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n",
				"tx.yaml":        "tenants:\n  tx:\n    _profile: pfin\n",
			},
			want: ResolvedMetadata{Owner: "plat-team"},
		},
		{
			name: "profile elected by a platform entry",
			files: map[string]string{
				"_profiles.yaml": metadataProfiles,
				"_platform.yaml": "tenants:\n  tx:\n    _profile: pfin\n",
				"tx.yaml":        "tenants:\n  tx: {}\n",
			},
			want: ResolvedMetadata{Environment: "production", Domain: "finance", Owner: "team-p"},
		},
		{
			name: "unknown profile supplies nothing",
			files: map[string]string{
				"_profiles.yaml": metadataProfiles,
				"tx.yaml":        "tenants:\n  tx:\n    _profile: nope\n",
			},
			want: ResolvedMetadata{},
		},
		{
			name: "string form over a platform mapping merges per key",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n      environment: staging\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata: \"environment: production\\n\"\n",
			},
			want: ResolvedMetadata{Environment: "production", Owner: "plat-team"},
		},
		{
			name: "a value that does not decode leaves every field empty",
			files: map[string]string{
				"tx.yaml": "tenants:\n  tx:\n    _metadata:\n      environment: {a: b}\n      domain: finance\n",
			},
			want: ResolvedMetadata{},
		},
		{
			name: "null _metadata over a platform entry: nothing below survives",
			files: map[string]string{
				"_platform.yaml": "tenants:\n  tx:\n    _metadata:\n      owner: plat-team\n",
				"tx.yaml":        "tenants:\n  tx:\n    _metadata: null\n",
			},
			want: ResolvedMetadata{},
		},
		{
			name: "carrier declares _metadata in optional_overrides: the profile does not fill it",
			files: map[string]string{
				"_defaults.yaml": "defaults:\n  k: 1\noptional_overrides: [_metadata]\n",
				"_profiles.yaml": metadataProfiles,
				"tx.yaml":        "tenants:\n  tx:\n    _profile: pfin\n",
			},
			want: ResolvedMetadata{},
		},
		{
			name: "two files define the profile: the later file's _metadata",
			files: map[string]string{
				"_a.yaml": metadataProfiles,
				"_b.yaml": "profiles:\n  pfin:\n    _metadata:\n      owner: team-b\n",
				"tx.yaml": "tenants:\n  tx:\n    _profile: pfin\n",
			},
			want: ResolvedMetadata{Owner: "team-b"},
		},
		{
			name: "tenant file's empty _profile over a platform entry's: no profile",
			files: map[string]string{
				"_profiles.yaml": metadataProfiles,
				"_platform.yaml": "tenants:\n  tx:\n    _profile: pfin\n",
				"tx.yaml":        "tenants:\n  tx:\n    _profile: \"\"\n",
			},
			want: ResolvedMetadata{},
		},
	}
}

func TestMetadataResolverMatchesMetrics(t *testing.T) {
	t.Parallel()
	for _, tc := range tenantMetadataTrees() {
		t.Run(tc.name, func(t *testing.T) {
			t.Parallel()
			dir := t.TempDir()
			writeMergeTree(t, dir, tc.files)
			want := tc.want
			want.Tenant = "tx"

			oracle, _, err := LoadDir(dir, nil)
			if err != nil {
				t.Fatalf("LoadDir: %v", err)
			}
			var metrics ResolvedMetadata
			for _, md := range oracle.ResolveMetadata() {
				if md.Tenant == "tx" {
					metrics = md
				}
			}
			if !reflect.DeepEqual(metrics, want) {
				t.Fatalf("/metrics reads %+v, the table says %+v", metrics, want)
			}

			body, err := os.ReadFile(filepath.Join(dir, "tx.yaml"))
			if err != nil {
				t.Fatal(err)
			}
			root, err := LoadRootPlatformChecked(dir)
			if err != nil {
				t.Fatalf("LoadRootPlatformChecked: %v", err)
			}
			got, ok := root.MetadataResolver().ResolveFile("tx", body)
			if !ok {
				t.Fatalf("ResolveFile: tx not read")
			}
			if !reflect.DeepEqual(got, metrics) {
				t.Errorf("MetadataResolver = %+v, /metrics = %+v", got, metrics)
			}
		})
	}
}

// The zero MetadataResolver (no platform files, no profiles) reads the tenant
// file's own `_metadata` alone; a body that does not declare the tenant or
// does not decode is not read.
func TestMetadataResolverWithoutRootAndUnreadBodies(t *testing.T) {
	t.Parallel()
	var root MetadataResolver
	got, ok := root.ResolveFile("tx", []byte("tenants:\n  tx:\n    _profile: pfin\n    _metadata: \"domain: finance\\n\"\n"))
	if want := (ResolvedMetadata{Tenant: "tx", Domain: "finance"}); !ok || !reflect.DeepEqual(got, want) {
		t.Errorf("zero root = %+v, %v; want %+v, true", got, ok, want)
	}
	for name, body := range map[string]string{
		"other tenant only": "tenants:\n  ty:\n    _metadata:\n      domain: finance\n",
		"does not decode":   "tenants: [\n",
	} {
		if got, ok := root.ResolveFile("tx", []byte(body)); ok || !reflect.DeepEqual(got, ResolvedMetadata{Tenant: "tx"}) {
			t.Errorf("%s: = %+v, %v; want empty, false", name, got, ok)
		}
	}
}
