package main

// Lowercase wrappers around `pkg/config` parse helpers.
//
// v2.8.0 PR-4 collapsed the parallel `app/config_parse.go` ↔
// `pkg/config/parse.go` trees. The canonical implementation lives in
// `pkg/config` (it ships capital exports like ParseHHMM / ParseMetricKey
// for library consumers like `cmd/da-guard`). These wrappers exist
// solely so existing `package main` test files (config_test.go,
// config_dimensional_test.go, config_silent_mode_test.go,
// config_resolve_test.go) keep compiling without a mass `parseHHMM →
// config.ParseHHMM` rename across hundreds of call sites.
//
// Behavior pin: every wrapper is `return config.X(args...)`, no extra
// logic. The methods on ScheduledValue (UnmarshalYAML / String /
// ResolveValue) come for free via the type alias in config_types.go.

import (
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

func matchTimeWindow(window string, now time.Time) bool {
	return config.MatchTimeWindow(window, now)
}

func parseHHMM(s string) (int, int, error) {
	return config.ParseHHMM(s)
}

func clampDuration(value, param, tenant string) string {
	return config.ClampDuration(value, param, tenant)
}

func parsePromDuration(s string) (time.Duration, error) {
	return config.ParsePromDuration(s)
}

func formatDuration(d time.Duration) string {
	return config.FormatDuration(d)
}

func isDisabled(lower string) bool {
	return config.IsDisabled(lower)
}

func parseMetricKey(key string) (component, metric string) {
	return config.ParseMetricKey(key)
}

func parseKeyWithLabels(key string) (string, map[string]string, map[string]string) {
	return config.ParseKeyWithLabels(key)
}

func parseLabelsStringWithOp(s string) (exact map[string]string, regex map[string]string) {
	return config.ParseLabelsStringWithOp(s)
}
