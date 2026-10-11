package config

import (
	"reflect"
	"strings"
	"testing"
)

// #2859: after the typed decode a string `_metadata` that reads as YAML is
// the same value as a mapping, so ParseConfigFile records which entries
// wrote the string form; the exporter keeps reading both.
func TestParseConfigFileRecordsStringMetadata(t *testing.T) {
	data := []byte("profiles:\n" +
		"  gold:\n    _metadata: \"tier: gold\"\n" +
		"  plain:\n    _metadata: {tier: silver}\n" +
		"tenants:\n" +
		"  t-str:\n    _metadata: \"environment: production\"\n" +
		"  t-block:\n    _metadata: |\n      environment: production\n" +
		"  t-map:\n    _metadata:\n      environment: production\n" +
		"  t-default:\n    _metadata: {default: x, environment: production}\n" +
		"  t-null:\n    _metadata:\n" +
		"  t-int:\n    _metadata: 5\n" +
		"  t-anchor:\n    _metadata: &m \"owner: a\"\n" +
		"  t-alias:\n    _metadata: *m\n" +
		"  t-none:\n    cpu: \"80\"\n")
	cfg, err := ParseConfigFile(data)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"tenants.t-str", "tenants.t-block", "tenants.t-int", "tenants.t-anchor", "tenants.t-alias", "profiles.gold"}
	if got := cfg.StringMetadataEntries(); !reflect.DeepEqual(got, want) {
		t.Fatalf("StringMetadataEntries = %q, want %q", got, want)
	}
	// Still read: the string form serves its fields as before.
	for _, tenant := range []string{"t-str", "t-block", "t-map"} {
		meta, ok, decoded := MetadataResolver{}.ResolveFile(tenant, data)
		if !ok || !decoded || meta.Environment != "production" {
			t.Errorf("%s: metadata = %+v (ok %v, decoded %v); want environment production", tenant, meta, ok, decoded)
		}
	}
}

func TestParseConfigFileEmptyAndNonMappingDocuments(t *testing.T) {
	for _, body := range []string{"", "~\n", "# only a comment\n", "tenants: ~\n", "tenants:\n  t1: ~\n"} {
		cfg, err := ParseConfigFile([]byte(body))
		if err != nil {
			t.Errorf("%q: err = %v, want none", body, err)
			continue
		}
		if got := cfg.StringMetadataEntries(); got != nil {
			t.Errorf("%q: StringMetadataEntries = %q, want none", body, got)
		}
	}
}

func TestTenantWritesStringMetadata(t *testing.T) {
	data := []byte("tenants:\n  a:\n    _metadata: \"owner: x\"\n  b:\n    _metadata: {owner: y}\n")
	for tenant, want := range map[string]bool{"a": true, "b": false, "c": false} {
		if got := TenantWritesStringMetadata(data, tenant); got != want {
			t.Errorf("tenant %s: got %v, want %v", tenant, got, want)
		}
	}
	if TenantWritesStringMetadata([]byte("tenants: [\n"), "a") {
		t.Error("unparseable bytes: got true, want false")
	}
	// A profile's string form is not a tenant's.
	if TenantWritesStringMetadata([]byte("profiles:\n  a:\n    _metadata: \"owner: x\"\n"), "a") {
		t.Error("profiles.a: got true, want false")
	}
}

// #2859 review: a `_metadata` brought in by a merge key (`<<`) is decoded
// like a written one, so it is listed like one — with yaml.v3's precedence:
// a written key wins over a merged one, and the earlier merge source wins.
func TestStringMetadataThroughMergeKeys(t *testing.T) {
	data := []byte("anchors:\n" +
		"  - &str {_metadata: \"owner: a\"}\n" +
		"  - &map {_metadata: {owner: b}}\n" +
		"tenants:\n" +
		"  t-merged:\n    <<: *str\n" +
		"  t-inline:\n    <<: {_metadata: \"environment: production\"}\n" +
		"  t-overridden:\n    <<: *str\n    _metadata: {owner: c}\n" +
		"  t-written-str:\n    <<: *map\n    _metadata: \"owner: d\"\n" +
		"  t-first-wins-str:\n    <<: [*str, *map]\n" +
		"  t-first-wins-map:\n    <<: [*map, *str]\n" +
		"  t-quoted-key:\n    \"<<\": \"owner: e\"\n" +
		"  <<: {t-from-merge: {_metadata: \"owner: f\"}}\n")
	cfg, err := ParseConfigFile(data)
	if err != nil {
		t.Fatal(err)
	}
	want := []string{"tenants.t-merged", "tenants.t-inline", "tenants.t-written-str", "tenants.t-first-wins-str", "tenants.t-from-merge"}
	if got := cfg.StringMetadataEntries(); !reflect.DeepEqual(got, want) {
		t.Fatalf("StringMetadataEntries = %q, want %q", got, want)
	}
	// The typed decode agrees on every listed and unlisted entry.
	for tenant, str := range map[string]bool{"t-merged": true, "t-inline": true, "t-overridden": false, "t-written-str": true, "t-first-wins-str": true, "t-first-wins-map": false, "t-from-merge": true} {
		d := cfg.Tenants[tenant]["_metadata"].Default
		if isMapping := strings.HasSuffix(d, "\n"); isMapping == str {
			t.Errorf("%s: typed decode Default = %q; listed as string form = %v", tenant, d, str)
		}
	}
	if !TenantWritesStringMetadata(data, "t-inline") || TenantWritesStringMetadata(data, "t-overridden") {
		t.Error("TenantWritesStringMetadata does not follow merge keys")
	}
}
