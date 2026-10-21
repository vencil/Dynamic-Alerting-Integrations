package config

// What judgeConfFile's content cache hides from the repeated-scan benches
// (#3206): BenchmarkScanDirTree_1000_Cold in package main scans the same
// tree every iteration, so from the second one every verdict is a cache hit.
// A process's real cold start judges each file once; this bench empties the
// cache before every scan, so it keeps that one-time cost visible beside
// BenchmarkScanDirTree_1000_ColdVerdictCached (the cache warm, as on a
// reload of unchanged files).

import (
	"crypto/sha256"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"testing"
)

func verdictBenchTree(b *testing.B) string {
	b.Helper()
	dir := b.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "_defaults.yaml"),
		[]byte("defaults:\n  mysql_connections: 80\n  container_cpu: 80\n"), 0o600); err != nil {
		b.Fatal(err)
	}
	for i := 0; i < 1000; i++ {
		body := fmt.Sprintf("tenants:\n  tenant-%04d:\n    mysql_connections: \"%d\"\n    container_cpu:\n"+
			"      default: \"%d\"\n      overrides:\n        - window: \"22:00-06:00\"\n          value: \"95\"\n"+
			"    _metadata:\n      owner: team-a\n", i, 50+i%100, 70+i%30)
		if err := os.WriteFile(filepath.Join(dir, fmt.Sprintf("tenant-%04d.yaml", i)), []byte(body), 0o600); err != nil {
			b.Fatal(err)
		}
	}
	return dir
}

func benchScanVerdict(b *testing.B, cached bool) {
	dir := verdictBenchTree(b)
	discard := log.New(io.Discard, "", 0)
	if _, err := ScanDirTree(dir, nil, nil, discard); err != nil {
		b.Fatal(err)
	}
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if !cached {
			verdictCache.mu.Lock()
			verdictCache.m = make(map[[sha256.Size]byte]*UnreadableFileError)
			verdictCache.mu.Unlock()
		}
		if _, err := ScanDirTree(dir, nil, nil, discard); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkScanDirTree_1000_ColdVerdictUncached(b *testing.B) { benchScanVerdict(b, false) }
func BenchmarkScanDirTree_1000_ColdVerdictCached(b *testing.B)   { benchScanVerdict(b, true) }
