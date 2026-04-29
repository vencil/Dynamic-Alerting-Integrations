package parser

// VictoriaMetrics-only function allowlist (PR-2).
//
// PR-1 hard-coded the allowlist in this Go file. PR-2 promotes the
// list to an external `vm_only_functions.yaml` (embedded at build
// time) so that:
//
//   1. Updating the allowlist no longer requires touching Go source —
//      a YAML edit + freshness CI gate is enough.
//   2. The freshness gate (test_vm_only_functions_freshness.go) reads
//      the same YAML and diffs it against the metricsql release in
//      go.mod, failing CI when an upstream version-bump introduces
//      new function names that haven't been triaged here.
//
// The YAML stays embedded (via go:embed) rather than loaded from disk
// at runtime so the binary remains self-contained — customer
// deployments don't need to ship the YAML alongside the executable.
//
// CAUTION when adding entries: list only function-call names that
// actually appear in `metricsql.FuncExpr.Name` or
// `metricsql.AggrFuncExpr.Name`. Keywords (`with`, modifier verbs
// like `offset`/`@`) are parsed as separate Expr shapes and never
// reach the function-name visitor; adding them there is dead code.

import (
	_ "embed"
	"fmt"
	"sort"
	"sync"

	"gopkg.in/yaml.v3"
)

//go:embed vm_only_functions.yaml
var vmOnlyFunctionsYAML []byte

// vmOnlyAllowlist is the parsed YAML; populated lazily via initOnce
// because go:embed only delivers raw bytes — we still need a YAML
// decode + map flatten before the lookup map is usable.
type vmOnlyAllowlist struct {
	// MetricsqlVersion is the parser release the YAML was curated
	// against (e.g. "v0.87.0"). The freshness gate compares this
	// against the metricsql dep in go.mod; mismatches trigger a CI
	// audit reminder.
	MetricsqlVersion string `yaml:"metricsql_version"`

	// Functions is decoded as a YAML mapping where keys are function
	// names and values are ignored (idiomatic "set" shape in YAML —
	// `name:` with empty/null value). yaml.v3 decodes `name:` as
	// `name: ""` so we use string values; only the keys matter.
	Functions map[string]string `yaml:"functions"`
}

var (
	vmOnlyOnce sync.Once
	vmOnly     map[string]struct{}
	vmOnlyMeta vmOnlyAllowlist
	vmOnlyErr  error
)

// loadVMOnlyAllowlist decodes the embedded YAML once. Errors here are
// programmer bugs (the YAML lives in the same module as this file,
// reviewed together) so we panic on failure to make them impossible
// to ignore. The freshness CI gate catches the realistic regression
// (new VM functions upstream); a malformed YAML caught at startup is
// a localised fix.
func loadVMOnlyAllowlist() {
	vmOnlyOnce.Do(func() {
		var doc vmOnlyAllowlist
		if err := yaml.Unmarshal(vmOnlyFunctionsYAML, &doc); err != nil {
			vmOnlyErr = fmt.Errorf("parser: decode vm_only_functions.yaml: %w", err)
			return
		}
		if len(doc.Functions) == 0 {
			vmOnlyErr = fmt.Errorf("parser: vm_only_functions.yaml has no `functions` entries")
			return
		}
		set := make(map[string]struct{}, len(doc.Functions))
		for name := range doc.Functions {
			set[lowerASCII(name)] = struct{}{}
		}
		vmOnly = set
		vmOnlyMeta = doc
	})
	if vmOnlyErr != nil {
		// Embedded YAML failing to decode is unrecoverable — the
		// binary's classification map is empty and every rule would
		// be mislabelled `prom`. Better to fail loudly than silently.
		panic(vmOnlyErr)
	}
}

// IsVMOnlyFunction reports whether the given function name is known
// to belong only to MetricsQL (not vanilla PromQL).
//
// The check is case-insensitive; metricsql normalises function names
// to lower-case before dispatch, so "Rollup_Rate" and "rollup_rate"
// behave identically and we match accordingly.
func IsVMOnlyFunction(name string) bool {
	loadVMOnlyAllowlist()
	_, ok := vmOnly[lowerASCII(name)]
	return ok
}

// VMOnlyFunctionNames returns a sorted slice of every function name
// in the allowlist. Used by the freshness CI gate and by the
// `da-parser allowlist` introspection subcommand.
func VMOnlyFunctionNames() []string {
	loadVMOnlyAllowlist()
	out := make([]string, 0, len(vmOnly))
	for name := range vmOnly {
		out = append(out, name)
	}
	sort.Strings(out)
	return out
}

// VMOnlyMetricsqlVersion returns the metricsql parser release tag the
// allowlist was curated against. The freshness gate uses this to decide
// whether the allowlist needs a re-audit cycle.
func VMOnlyMetricsqlVersion() string {
	loadVMOnlyAllowlist()
	return vmOnlyMeta.MetricsqlVersion
}

// lowerASCII is a tiny ASCII-only lower-caser used to avoid pulling
// in unicode tables for function-name normalisation. PromQL /
// MetricsQL function names are by spec ASCII identifiers.
func lowerASCII(s string) string {
	out := make([]byte, len(s))
	for i := 0; i < len(s); i++ {
		c := s[i]
		if c >= 'A' && c <= 'Z' {
			c += 'a' - 'A'
		}
		out[i] = c
	}
	return string(out)
}
