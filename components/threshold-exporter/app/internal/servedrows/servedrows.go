// Package servedrows hands pkg/config's keyed resolve to da-guard
// served-values (#2115) without making it part of pkg/config's public API.
//
// pkg/config's resolver can tell, for each threshold row it produces, which
// tenant-config key the row serves (an unexported sink on the resolve path).
// da-guard needs that answer; nothing outside this module should. An
// `internal` package is importable only from inside the module, so pkg/config
// stores the keyed resolve here at init and cmd/da-guard reads it.
//
// This package cannot name pkg/config's types (pkg/config imports it), so the
// value is untyped. Its dynamic type, set by pkg/config/served_rows.go, is
//
//	func(cfg *config.ThresholdConfig, now time.Time,
//	     sink func(key string, row config.ResolvedThreshold)) []config.ResolvedThreshold
//
// and the reader asserts that type; a mismatch is a build-time-visible test
// failure in cmd/da-guard, never a silent fallback.
package servedrows

// ResolveAt is the keyed resolve; see the package comment for its type.
var ResolveAt any
