package config

import (
	"time"

	"github.com/vencil/threshold-exporter/internal/servedrows"
)

// The keyed resolve for da-guard served-values (#2115): ResolveAt's rows,
// each reported to sink with the tenant-config key it serves. Published
// through the module-internal package servedrows, so it adds nothing to this
// package's public API.
func init() {
	servedrows.ResolveAt = func(c *ThresholdConfig, now time.Time, sink func(key string, row ResolvedThreshold)) []ResolvedThreshold {
		rows, _ := c.resolveAtWithStats(now, sink)
		return rows
	}
}
