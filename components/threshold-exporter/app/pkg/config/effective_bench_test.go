package config_test

// Cold-load cost of the walker plane's two library readers (#2019):
// ResolveEffective (one tenant, tenant-api /effective) and ScopeEffective
// (every tenant, da-guard). Each call is one cold ScanDirTree plus the merge,
// so these measure what the platform per-tenant layer adds to a request.
//
// Two trees of 1000 tenants under a four-level chain (root / 4 domains /
// 5 regions each / 50 tenants per region), identical except that the
// PlatformTenants variant's root `_defaults.yaml` carries a `tenants:`
// block for 100 of them. The plain tree is the common case, and the one
// whose cost must not move.
//
// ⚠️ Named `_Hier1k`, not `_1000`, on purpose: the PR bench gate and
// `make benchmark-report` select `_1000(_|$)`, and adding to that set is a
// separate decision from adding a measurement.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func writeBenchFile(b *testing.B, path, body string) {
	b.Helper()
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		b.Fatal(err)
	}
	if err := os.WriteFile(path, []byte(body), 0o600); err != nil {
		b.Fatal(err)
	}
}

// buildEffectiveBenchTree writes the tree and returns its root and one
// tenant id that the platform block names (when withPlatform).
func buildEffectiveBenchTree(b *testing.B, withPlatform bool) (string, string) {
	b.Helper()
	root := b.TempDir()
	keys := []string{"mysql_connections", "mysql_threads_running", "container_cpu", "container_memory",
		"oracle_sessions_active", "redis_memory", "es_index_store_size_bytes", "pg_connections"}
	defaults := func(v int) string {
		var sb strings.Builder
		sb.WriteString("defaults:\n")
		for i, k := range keys {
			fmt.Fprintf(&sb, "  %s: %d\n", k, v+i)
		}
		return sb.String()
	}
	rootDefaults := defaults(80)
	if withPlatform {
		var sb strings.Builder
		sb.WriteString("tenants:\n")
		for n := 0; n < 1000; n += 10 {
			fmt.Fprintf(&sb, "  t-%04d:\n    mysql_connections: \"60\"\n    _silent_mode: warning\n    redis_memory: \"70\"\n", n)
		}
		rootDefaults += sb.String()
	}
	writeBenchFile(b, filepath.Join(root, "_defaults.yaml"), rootDefaults)
	writeBenchFile(b, filepath.Join(root, "_profiles.yaml"), "profiles:\n  small:\n    mysql_connections: \"50\"\n")
	n := 0
	for d := 0; d < 4; d++ {
		dom := filepath.Join(root, fmt.Sprintf("dom%d", d))
		writeBenchFile(b, filepath.Join(dom, "_defaults.yaml"), defaults(70+d))
		for r := 0; r < 5; r++ {
			reg := filepath.Join(dom, fmt.Sprintf("reg%d", r))
			writeBenchFile(b, filepath.Join(reg, "_defaults.yaml"), defaults(60+r))
			for t := 0; t < 50; t++ {
				id := fmt.Sprintf("t-%04d", n)
				writeBenchFile(b, filepath.Join(reg, id+".yaml"),
					fmt.Sprintf("tenants:\n  %s:\n    mysql_connections: \"%d\"\n    container_cpu: \"%d\"\n", id, 40+t, 50+t))
				n++
			}
		}
	}
	return root, "t-0500"
}

func benchResolveEffective(b *testing.B, withPlatform bool) {
	root, tid := buildEffectiveBenchTree(b, withPlatform)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := config.ResolveEffective(root, tid); err != nil {
			b.Fatal(err)
		}
	}
}

func benchScopeEffective(b *testing.B, withPlatform bool) {
	root, _ := buildEffectiveBenchTree(b, withPlatform)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		s, err := config.ScopeEffective(root, root)
		if err != nil {
			b.Fatal(err)
		}
		if len(s.Tenants) != 1000 {
			b.Fatalf("scope has %d tenants, want 1000", len(s.Tenants))
		}
	}
}

func BenchmarkResolveEffective_Hier1k(b *testing.B)                 { benchResolveEffective(b, false) }
func BenchmarkResolveEffective_Hier1k_PlatformTenants(b *testing.B) { benchResolveEffective(b, true) }
func BenchmarkScopeEffective_Hier1k(b *testing.B)                   { benchScopeEffective(b, false) }
func BenchmarkScopeEffective_Hier1k_PlatformTenants(b *testing.B)   { benchScopeEffective(b, true) }
