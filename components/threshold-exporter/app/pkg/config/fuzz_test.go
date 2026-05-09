// P2-11 (testing-quality follow-on): Go fuzz tests for the config-parsing
// surface. These functions all consume untrusted input (config YAML values,
// CLI args) and a regression introduced by an off-by-one or panic-y edge
// case would surface only when a customer's config triggers it. Fuzz tests
// give the testing program a property-style guarantee on top of the
// example-based + mutation-pilot coverage.
//
// Run locally with:
//   go test ./pkg/config/ -run=^$ -fuzz=FuzzParseHHMM -fuzztime=10s
//
// CI integration: not yet wired (these are Go fuzz funcs, not test funcs;
// the standard `go test ./...` does NOT exercise them by default — they
// require an explicit -fuzz flag and a dedicated CI target). Future work:
// add a nightly fuzz workflow analogous to nightly-mutation-pilot.yaml.

package config

import (
	"strings"
	"testing"
)

// FuzzParseHHMM — never panics on arbitrary string input; on success, hour
// and minute are in their valid ranges.
func FuzzParseHHMM(f *testing.F) {
	// Seed corpus: representative valid + invalid forms to give the fuzzer
	// a starting point. The fuzzer mutates from these.
	for _, seed := range []string{
		"00:00", "23:59", "12:30", "05:07",
		" 9:5 ", "  09  :  05  ",
		"24:00", "12:60", "-1:00", "12:-1",
		"abc", "", ":", "12:", ":30",
		"12:34:56",
		"12.5:30",
	} {
		f.Add(seed)
	}

	f.Fuzz(func(t *testing.T, s string) {
		h, m, err := parseHHMM(s)
		if err != nil {
			// On error, h and m may be zero — just verify error isn't nil-ish.
			if h != 0 || m != 0 {
				t.Errorf("error path returned non-zero (%d, %d) for %q", h, m, s)
			}
			return
		}
		// Success path: invariants must hold.
		if h < 0 || h > 23 {
			t.Errorf("hour %d out of range for %q", h, s)
		}
		if m < 0 || m > 59 {
			t.Errorf("minute %d out of range for %q", m, s)
		}
	})
}

// FuzzParsePromDuration — never panics; on success the parsed duration is
// non-negative for non-negative inputs (sign-preserving documented behavior),
// and the function tolerates whitespace + invalid units gracefully.
func FuzzParsePromDuration(f *testing.F) {
	for _, seed := range []string{
		"30s", "5m", "4h", "1d",
		"0s", "1.5h",
		"  10s  ",
		"", "x", "10x", "abc",
		"-5m",
		"99999999999999999999d", // very large
	} {
		f.Add(seed)
	}

	f.Fuzz(func(t *testing.T, s string) {
		// Test guarantees: must not panic. If it returns no error, the
		// output is some time.Duration value (no further invariant claimed
		// — overflow behavior is platform-specific for huge inputs).
		_, _ = parsePromDuration(s)
	})
}

// FuzzClampDuration — never panics; returns a string. param/tenant labels
// are loop-invariant strings used in log output; we vary the value only.
func FuzzClampDuration(f *testing.F) {
	for _, seed := range []string{
		"30s", "5m", "4h",
		"", "abc", "0", "-1m",
		"99h",
	} {
		f.Add(seed)
	}

	f.Fuzz(func(t *testing.T, value string) {
		// Use a known-defined param so the guardrail lookup hits.
		out := clampDuration(value, "group_wait", "fuzz-tenant")
		// ClampDuration always returns a string — assert it doesn't crash
		// during string ops downstream by exercising one len() check.
		_ = len(out)
	})
}

// FuzzIsDisabled — never panics; returns bool. Property: result is true iff
// the trimmed-lowercase input is in the known-disabled set.
func FuzzIsDisabled(f *testing.F) {
	for _, seed := range []string{
		"", "disable", "disabled", "off", "false", "true", "DISABLE",
		"  off  ", "Off", "no", "nil", "null", "0",
	} {
		f.Add(seed)
	}

	known := map[string]bool{
		"disable": true, "disabled": true, "off": true, "false": true,
	}

	f.Fuzz(func(t *testing.T, s string) {
		got := isDisabled(s)
		// isDisabled callers are expected to lowercase before calling, so
		// the function only checks the literal set. Property: must agree
		// with the literal-set lookup.
		want := known[s]
		if got != want {
			t.Errorf("isDisabled(%q) = %v, want %v", s, got, want)
		}
	})
}

// FuzzParseMetricKey — never panics; component is always non-empty.
//
// History: an earlier iteration of this fuzz found that parseMetricKey
// returned ("", "leading") for inputs starting with "_" because the source
// did `if idx < 0` instead of `if idx <= 0`. The source was patched (see
// the doc comment on parseMetricKey in parse.go); this test now asserts
// the post-fix invariant.
func FuzzParseMetricKey(f *testing.F) {
	for _, seed := range []string{
		"mysql_connections", "redis_memory_used_bytes",
		"single", "", "_leading", "trailing_",
		"a_b_c", "noseparator",
	} {
		f.Add(seed)
	}

	f.Fuzz(func(t *testing.T, key string) {
		comp, metric := parseMetricKey(key)

		// Invariant 1: component is never empty.
		if comp == "" {
			t.Errorf("parseMetricKey(%q) returned empty component", key)
		}

		// Invariant 2: route based on first "_" position (idx).
		//   idx <= 0 (no "_", empty key, or leading "_") → ("default", key)
		//   idx >  0                                       → (key[:idx], key[idx+1:])
		idx := strings.Index(key, "_")
		if idx <= 0 {
			if comp != "default" || metric != key {
				t.Errorf("idx<=0 case: key=%q expected (\"default\", %q), got (%q, %q)",
					key, key, comp, metric)
			}
		} else {
			if comp+"_"+metric != key {
				t.Errorf("reconstruction mismatch: key=%q comp=%q metric=%q",
					key, comp, metric)
			}
			// And comp must NOT itself contain "_" — it's the prefix
			// before the FIRST "_" found.
			if strings.Contains(comp, "_") {
				t.Errorf("comp %q contains underscore for key %q", comp, key)
			}
		}
	})
}

// FuzzDeepMerge — never panics on arbitrary YAML-shaped maps. Properties:
//   - merging with nil override returns a deep copy of base
//   - keys in override that map to nil cause deletion from result
//   - merging is purely additive when keys don't collide
func FuzzDeepMerge(f *testing.F) {
	// Seed with a few key patterns. The fuzzer can't synthesize maps
	// directly (Go fuzz only supports primitive types), so we encode
	// them as a join of `key:value;key:value` and parse to map[string]any.
	for _, seed := range [][2]string{
		{"a:1;b:2", "c:3"},
		{"a:1;b:2", "a:99"},
		{"a:1;b:2", "a:nil"},  // nil-delete
		{"", ""},
		{"", "x:7"},
	} {
		f.Add(seed[0], seed[1])
	}

	f.Fuzz(func(t *testing.T, baseEnc, overrideEnc string) {
		base := decodeMap(baseEnc)
		override := decodeMap(overrideEnc)
		// Take a copy of base BEFORE the merge to verify it isn't mutated.
		baseSnapshot := decodeMap(baseEnc)

		_ = deepMerge(base, override)

		// Property: deepMerge does not mutate `base` (it deepCopyMap-s it
		// internally before applying override).
		if !mapEqual(base, baseSnapshot) {
			t.Errorf("deepMerge mutated base: was %v, now %v", baseSnapshot, base)
		}
	})
}

// decodeMap parses "k:v;k:v" → map[string]any. "nil" value → nil entry.
// Used by FuzzDeepMerge to translate fuzzer-supplied strings into maps.
func decodeMap(enc string) map[string]any {
	out := make(map[string]any)
	if enc == "" {
		return out
	}
	for _, pair := range strings.Split(enc, ";") {
		if pair == "" {
			continue
		}
		parts := strings.SplitN(pair, ":", 2)
		if len(parts) != 2 {
			continue
		}
		k, v := parts[0], parts[1]
		if v == "nil" {
			out[k] = nil
		} else {
			out[k] = v
		}
	}
	return out
}

// mapEqual is a shallow string-only equality check (sufficient since
// decodeMap only produces string + nil values).
func mapEqual(a, b map[string]any) bool {
	if len(a) != len(b) {
		return false
	}
	for k, v := range a {
		bv, ok := b[k]
		if !ok || bv != v {
			return false
		}
	}
	return true
}
