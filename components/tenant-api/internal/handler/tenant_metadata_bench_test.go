package handler

// #2830: the cost of reading every tenant's `_metadata` on the LIST /
// search snapshot over a tree with many profiles. 1000 tenants, 20
// profiles of 50 keys each (every profile writes `_metadata`), a platform
// entry for every second tenant, and a 50-key root `_defaults.yaml`.
//
//	go test ./internal/handler -run '^$' -bench MetadataProfiles -benchmem -count=3

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

func writeMetadataBenchTree(b *testing.B) string {
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
