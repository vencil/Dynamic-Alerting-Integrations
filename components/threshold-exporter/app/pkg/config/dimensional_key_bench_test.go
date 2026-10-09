package config

import (
	"fmt"
	"strings"
	"testing"
)

// dimensionalTenantsFile is one file declaring n tenants, each writing four
// dimensional keys — in the canonical spelling, or with the labels in
// another order and single-quoted (the decode's re-spelling path, #2031).
func dimensionalTenantsFile(n int, respelled bool) []byte {
	var b strings.Builder
	b.WriteString("tenants:\n")
	for i := 0; i < n; i++ {
		fmt.Fprintf(&b, "  tenant-%04d:\n", i)
		for j := 0; j < 4; j++ {
			if respelled {
				fmt.Fprintf(&b, "    \"redis_queue_length{queue='q%d', priority='high'}\": %d\n", j, 10+i%50)
			} else {
				fmt.Fprintf(&b, "    'redis_queue_length{priority=\"high\", queue=\"q%d\"}': %d\n", j, 10+i%50)
			}
		}
	}
	return []byte(b.String())
}

func benchParseDimensional(b *testing.B, respelled bool) {
	data := dimensionalTenantsFile(1000, respelled)
	b.ReportAllocs()
	b.ResetTimer()
	for i := 0; i < b.N; i++ {
		if _, err := ParseConfigFile(data); err != nil {
			b.Fatal(err)
		}
	}
}

// The decode of 1000 tenants × 4 dimensional keys already in the canonical
// spelling (normalizeKeys' fast path) and with every key re-spelled (its
// slow path): the cost of #2031's normalization on a tree that needs it.
func BenchmarkParseConfigFile_1000Tenants_DimensionalCanonical(b *testing.B) {
	benchParseDimensional(b, false)
}

func BenchmarkParseConfigFile_1000Tenants_DimensionalRespelled(b *testing.B) {
	benchParseDimensional(b, true)
}
