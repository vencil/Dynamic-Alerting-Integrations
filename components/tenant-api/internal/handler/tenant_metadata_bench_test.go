package handler

// #2830: the cost of reading every tenant's `_metadata` on the LIST /
// search snapshot over a tree with many profiles. 1000 tenants, 20
// profiles of 50 keys each (every profile writes `_metadata`), a platform
// entry for every second tenant, and a 50-key root `_defaults.yaml`.
//
//	go test ./internal/handler -run '^$' -bench MetadataProfiles -benchmem -count=3
//
// TestMetadataBenchTreeReads pins what the benchmarked readers read on it.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	benchMetadataTenants  = 1000
	benchMetadataProfiles = 20
	benchMetadataKeys     = 50
)

func writeMetadataBenchTree(b testing.TB) string {
	b.Helper()
	dir := b.TempDir()
	write := func(name, body string) {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o600); err != nil {
			b.Fatal(err)
		}
	}
	var sb strings.Builder
	sb.WriteString("defaults:\n")
	for k := 0; k < benchMetadataKeys; k++ {
		fmt.Fprintf(&sb, "  k%d: %d\n", k, k)
	}
	write("_defaults.yaml", sb.String())

	sb.Reset()
	sb.WriteString("profiles:\n")
	for p := 0; p < benchMetadataProfiles; p++ {
		fmt.Fprintf(&sb, "  p%d:\n    _metadata:\n      environment: e%d\n      domain: d%d\n", p, p, p)
		for k := 0; k < benchMetadataKeys; k++ {
			fmt.Fprintf(&sb, "    k%d: %d\n", k, k+p)
		}
	}
	write("_profiles.yaml", sb.String())

	sb.Reset()
	sb.WriteString("tenants:\n")
	for t := 0; t < benchMetadataTenants; t += 2 {
		fmt.Fprintf(&sb, "  t%d:\n    _metadata:\n      owner: o%d\n", t, t)
	}
	write("_platform.yaml", sb.String())

	for t := 0; t < benchMetadataTenants; t++ {
		write(fmt.Sprintf("t%d.yaml", t), fmt.Sprintf("tenants:\n  t%d:\n    _profile: p%d\n    k1: 5\n", t, t%benchMetadataProfiles))
	}
	return dir
}

func BenchmarkLoadAllTenants_MetadataProfiles(b *testing.B) {
	dir := writeMetadataBenchTree(b)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		rows, err := loadAllTenants(dir)
		if err != nil || len(rows) != benchMetadataTenants {
			b.Fatal(err, len(rows))
		}
	}
}

func BenchmarkLoadTenantSnapshot_MetadataProfiles(b *testing.B) {
	dir := writeMetadataBenchTree(b)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := loadTenantSnapshot(dir); err != nil {
			b.Fatal(err)
		}
	}
}

// The per-tenant readers build their resolver per call (per request).
func BenchmarkWriteScopeMeta_MetadataProfiles(b *testing.B) {
	dir := writeMetadataBenchTree(b)
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		WriteScopeMeta(dir)("t1")
	}
}

func BenchmarkProposedScopeMeta_MetadataProfiles(b *testing.B) {
	dir := writeMetadataBenchTree(b)
	body := "tenants:\n  t1:\n    _profile: p1\n    k1: 5\n"
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		proposedScopeMeta(dir, body)("t1")
	}
}

func TestMetadataBenchTreeReads(t *testing.T) {
	t.Parallel()
	dir := writeMetadataBenchTree(t)
	rows, err := loadAllTenants(dir)
	if err != nil {
		t.Fatal(err)
	}
	byID := map[string]TenantSummary{}
	for _, r := range rows {
		byID[r.ID] = r
	}
	// t2 has a platform entry writing `_metadata`, so its profile's is not used.
	if r := byID["t2"]; r.Environment != "" || r.Domain != "" || r.Owner != "o2" || r.MetadataIncomplete {
		t.Errorf("t2 on LIST = %+v, want owner o2 alone (platform entry)", r)
	}
	if r := byID["t1"]; r.Environment != "e1" || r.Domain != "d1" || r.Owner != "" {
		t.Errorf("t1 on LIST = %+v, want e1 / d1 (profile p1), no owner", r)
	}
	if env, domain := WriteScopeMeta(dir)("t1"); env != "e1" || domain != "d1" {
		t.Errorf("on-disk read for t1 = (%q, %q), want (e1, d1)", env, domain)
	}
	if env, domain := proposedScopeMeta(dir, "tenants:\n  t1:\n    _profile: p3\n")("t1"); env != "e3" || domain != "d3" {
		t.Errorf("proposed-body read for t1 = (%q, %q), want (e3, d3)", env, domain)
	}
}
