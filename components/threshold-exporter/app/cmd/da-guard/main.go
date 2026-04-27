// da-guard is the CLI wrapper around internal/guard.CheckDefaultsImpact.
//
// It's the v2.8.0 Phase .c C-12 PR-4 deliverable: the first PR that
// turns the guard library (PR-1 schema, PR-2 routing, PR-3
// cardinality) into a customer-runnable binary. Customers run it
// from a pre-commit hook, a CI workflow, or a local "is my conf.d/
// safe?" sanity check before opening a PR.
//
// Scope simplification vs. planning §C-12 ("trigger when
// _defaults.yaml changes, validate impact"):
//
//	The planning row described a *delta-aware* tool that takes a
//	defaults change as input and predicts impact. PR-4 ships a
//	*current-working-tree* validator instead: it reads conf.d/
//	from disk and validates whatever's there. The two are
//	equivalent in every realistic CI / pre-commit flow because by
//	the time the tool runs, the proposed change is already on
//	disk (PR head commit; staged hunks; local edit). The
//	disk-state model also handles the "_defaults.yaml deleted
//	entirely" case (deletion is just one valid post-edit state
//	among many) without special-casing.
//
//	Speculative simulation — "would this change break things?"
//	without writing the change first — is a richer feature and
//	already partially served by C-7b /simulate for one tenant
//	at a time. Out of PR-4 scope; revisit in C-9 PR-3 if the
//	batch translator path needs it.
//
// Exit codes (stable contract for CI YAML / hook scripts):
//
//	0  clean run, no errors
//	1  guard found one or more SeverityError findings
//	2  caller error (bad flags, missing/invalid path, IO failure)
//
// Warnings never affect exit code (`--warn-as-error` flips this if
// a customer wants strict mode).
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strings"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// Version is overridden at build time via `-ldflags "-X main.Version=..."`.
// Default fallback so unbuilt `go run .` invocations don't crash on
// `--version`.
var Version = "dev"

// programName is the binary name we print in usage / errors. Kept
// in a var so tests can swap it without disturbing os.Args[0].
var programName = "da-guard"

// exit codes — referenced from tests too.
const (
	exitOK        = 0
	exitFindings  = 1
	exitCallerErr = 2
)

// flags is the parsed configuration for one run.
type flags struct {
	configDir            string
	scopeDir             string
	requiredFields       string
	cardinalityLimit     int
	cardinalityWarnRatio float64
	format               string
	output               string
	warnAsError          bool
	showVersion          bool
	help                 bool
}

func parseFlags(args []string, errOut io.Writer) (*flags, error) {
	fs := flag.NewFlagSet(programName, flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &flags{}

	fs.StringVar(&f.configDir, "config-dir", "",
		"Path to the conf.d/ root. Required. Defaults chains anchor here.")
	fs.StringVar(&f.scopeDir, "scope", "",
		"Subdirectory under --config-dir to limit validation. Empty = whole tree. "+
			"Typical use: pass dirname of the changed _defaults.yaml in a CI hook.")
	fs.StringVar(&f.requiredFields, "required-fields", "",
		"Comma-separated dotted paths every tenant's effective config must have "+
			"(e.g. 'thresholds.cpu,routing.receiver.type'). Empty disables the schema check.")
	fs.IntVar(&f.cardinalityLimit, "cardinality-limit", 0,
		"Per-tenant predicted-metric-count ceiling. 0 disables. "+
			"Mirror DefaultMaxMetricsPerTenant=500 to match runtime truncation.")
	fs.Float64Var(&f.cardinalityWarnRatio, "cardinality-warn-ratio", 0.0,
		"Warn-tier ratio of --cardinality-limit (0 < r < 1). 0 = library default (0.8).")
	fs.StringVar(&f.format, "format", "md",
		"Output format: 'md' (Markdown for PR comments) or 'json' (machine-readable).")
	fs.StringVar(&f.output, "output", "",
		"Write report to this file instead of stdout. Parent directory must exist.")
	fs.BoolVar(&f.warnAsError, "warn-as-error", false,
		"Treat warnings as errors for exit-code purposes (still rendered as 'warning' in the report).")
	fs.BoolVar(&f.showVersion, "version", false,
		"Print version and exit.")
	fs.BoolVar(&f.help, "help", false, "Print usage and exit.")
	// `-h` short alias
	fs.BoolVar(&f.help, "h", false, "Alias for --help.")

	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s [flags]\n", programName)
		fmt.Fprintf(errOut, "Validate a conf.d/ tree against the C-12 Dangling Defaults Guard.\n\n")
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  clean\n  1  guard found errors\n  2  caller error\n")
	}

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return f, nil
}

// run is the testable entry point. Returns an exit code; main()
// passes it to os.Exit. errOut receives caller-facing diagnostics
// (usage errors, exception messages); the report itself goes to
// either stdout (if --output empty) or the named file.
func run(args []string, stdout, errOut io.Writer) int {
	// Force log output to errOut so the report stream stays clean
	// when --output is empty (i.e. report goes to stdout).
	log.SetOutput(errOut)
	log.SetFlags(0)

	f, err := parseFlags(args, errOut)
	if err != nil {
		// flag.ContinueOnError already printed the message; just
		// return the code.
		return exitCallerErr
	}
	if f.help {
		// parseFlags already printed usage on -h; just exit clean.
		return exitOK
	}
	if f.showVersion {
		fmt.Fprintf(stdout, "%s %s\n", programName, Version)
		return exitOK
	}
	if f.configDir == "" {
		fmt.Fprintf(errOut, "%s: --config-dir is required\n", programName)
		return exitCallerErr
	}
	if f.format != "md" && f.format != "json" {
		fmt.Fprintf(errOut, "%s: --format must be 'md' or 'json' (got %q)\n", programName, f.format)
		return exitCallerErr
	}
	if f.cardinalityWarnRatio < 0 || f.cardinalityWarnRatio >= 1 {
		fmt.Fprintf(errOut, "%s: --cardinality-warn-ratio must be in [0, 1) (got %v)\n",
			programName, f.cardinalityWarnRatio)
		return exitCallerErr
	}

	scoped, err := config.ScopeEffective(f.configDir, f.scopeDir)
	if err != nil {
		fmt.Fprintf(errOut, "%s: %v\n", programName, err)
		return exitCallerErr
	}

	// No tenants in scope: this is "vacuously safe". Print a friendly
	// message in the chosen format and exit clean. We deliberately
	// don't return exitCallerErr here because GitHub Actions wrappers
	// run the guard on every _defaults.yaml change — and a defaults
	// file under a directory with no tenants yet (e.g. brand-new
	// domain skeleton) is a real, valid scenario.
	if len(scoped.Tenants) == 0 {
		if err := writeEmptyReport(stdout, errOut, f); err != nil {
			fmt.Fprintf(errOut, "%s: %v\n", programName, err)
			return exitCallerErr
		}
		return exitOK
	}

	input := buildCheckInput(scoped, f)
	report, err := guard.CheckDefaultsImpact(input)
	if err != nil {
		fmt.Fprintf(errOut, "%s: guard run: %v\n", programName, err)
		return exitCallerErr
	}

	if err := writeReport(stdout, errOut, f, scoped, report); err != nil {
		fmt.Fprintf(errOut, "%s: %v\n", programName, err)
		return exitCallerErr
	}

	if report.Summary.Errors > 0 {
		return exitFindings
	}
	if f.warnAsError && report.Summary.Warnings > 0 {
		return exitFindings
	}
	return exitOK
}

// buildCheckInput assembles a guard.CheckInput from the scoped
// resolution. It's where the YAML-shape → guard-input mapping lives:
//
//   - EffectiveConfigs[id]      ← ec.EffectiveConfig
//   - RoutingByTenant[id]       ← ec.EffectiveConfig["_routing"] (when nested map)
//   - TenantOverrides[id]       ← ec.TenantOverridesRaw (PR-5)
//   - NewDefaultsByTenant[id]   ← ec.MergedDefaults      (PR-5)
//
// The PR-5 wiring (TenantOverrides + NewDefaultsByTenant) enables
// the redundant-override warn-tier without changing the cardinality /
// schema / routing error tiers. Fields used to be nil-left in PR-4
// because pkg/config.EffectiveConfig didn't expose the pre-merge
// shapes; PR-5 adds those (json:"-" so tenant-api's API contract
// stays unchanged) and we now thread them through.
//
// We deliberately use NewDefaultsByTenant rather than NewDefaults: a
// scope spanning multiple cascading _defaults.yaml levels means
// different tenants inherit different merged-defaults views, and
// the guard's per-tenant resolution honors that. See
// guard.CheckInput documentation for the resolution rule.
func buildCheckInput(scoped *config.ScopedTenants, f *flags) guard.CheckInput {
	effective := make(map[string]map[string]any, len(scoped.Tenants))
	routing := make(map[string]map[string]any)
	tenantOverrides := make(map[string]map[string]any)
	newDefaultsByTenant := make(map[string]map[string]any)
	for _, ec := range scoped.Tenants {
		effective[ec.TenantID] = ec.EffectiveConfig
		// _routing is stored as a nested map inside the merged
		// effective config — see config_inheritance.go and the
		// db-b.yaml example. The guard's RoutingByTenant just
		// wants that nested block. Tenants without routing are
		// simply absent from the map (no finding emitted, per
		// guard/types.go documentation).
		if r, ok := ec.EffectiveConfig["_routing"].(map[string]any); ok {
			routing[ec.TenantID] = r
		}
		// PR-5: redundant-override warn-tier inputs. We populate
		// both fields as soon as the resolver hands them to us; an
		// absent tenant.yaml block (TenantOverridesRaw == nil) or
		// a defaults-less tree (MergedDefaults == nil) leaves the
		// per-tenant entry out of the map, which the guard treats
		// as "skip this tenant for redundant-override".
		if ec.TenantOverridesRaw != nil {
			tenantOverrides[ec.TenantID] = ec.TenantOverridesRaw
		}
		if ec.MergedDefaults != nil {
			newDefaultsByTenant[ec.TenantID] = ec.MergedDefaults
		}
	}

	required := splitNonEmpty(f.requiredFields)

	return guard.CheckInput{
		EffectiveConfigs:     effective,
		RequiredFields:       required,
		RoutingByTenant:      routing,
		TenantOverrides:      tenantOverrides,
		NewDefaultsByTenant:  newDefaultsByTenant,
		CardinalityLimit:     f.cardinalityLimit,
		CardinalityWarnRatio: f.cardinalityWarnRatio,
	}
}

// splitNonEmpty splits "a, b , ,c" into ["a","b","c"] — empty
// segments dropped so a trailing comma in CI YAML doesn't
// spuriously fail.
func splitNonEmpty(s string) []string {
	if s == "" {
		return nil
	}
	parts := strings.Split(s, ",")
	out := parts[:0]
	for _, p := range parts {
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

// writeReport renders the guard report in the chosen format and
// writes it to either f.output or stdout. The "rendered to file"
// path also drops a one-line summary on errOut so a CI log
// surfaces "errors=N, warnings=M" without the user having to cat
// the file separately.
func writeReport(stdout, errOut io.Writer, f *flags, scoped *config.ScopedTenants, report *guard.GuardReport) error {
	var body string
	switch f.format {
	case "md":
		body = renderMarkdown(scoped, report)
	case "json":
		b, err := json.MarshalIndent(struct {
			ConfigDir   string             `json:"config_dir"`
			Scope       string             `json:"scope,omitempty"`
			SourceFiles []string           `json:"source_files"`
			Report      *guard.GuardReport `json:"report"`
		}{
			ConfigDir:   f.configDir,
			Scope:       f.scopeDir,
			SourceFiles: scoped.SourceFiles,
			Report:      report,
		}, "", "  ")
		if err != nil {
			return fmt.Errorf("encode JSON report: %w", err)
		}
		body = string(b) + "\n"
	}

	if f.output == "" {
		_, err := io.WriteString(stdout, body)
		return err
	}
	abs, err := filepath.Abs(f.output)
	if err != nil {
		return fmt.Errorf("resolve --output %q: %w", f.output, err)
	}
	if err := os.WriteFile(abs, []byte(body), 0o644); err != nil {
		return fmt.Errorf("write --output %q: %w", abs, err)
	}
	fmt.Fprintf(errOut, "%s: wrote report to %s (errors=%d, warnings=%d)\n",
		programName, abs, report.Summary.Errors, report.Summary.Warnings)
	return nil
}

// renderMarkdown wraps GuardReport.Markdown() with a small caller
// preamble (config dir, scanned files) so the PR-comment reader
// has the context they need without scrolling back to the workflow
// definition.
func renderMarkdown(scoped *config.ScopedTenants, report *guard.GuardReport) string {
	var b strings.Builder
	b.WriteString(report.Markdown())
	if len(scoped.SourceFiles) > 0 {
		b.WriteString("\n<details><summary>Scanned files</summary>\n\n")
		for _, f := range scoped.SourceFiles {
			b.WriteString("- `" + f + "`\n")
		}
		b.WriteString("\n</details>\n")
	}
	return b.String()
}

// writeEmptyReport handles the "no tenants in scope" path. Mirrors
// writeReport's error contract — IO failures (e.g. unwritable
// --output path) surface as caller errors rather than silent
// success, otherwise a CI runner can't tell "scope was empty and we
// wrote nothing" from "scope was empty and the disk was full".
//
// Emits a clean Markdown / JSON shell so downstream consumers (PR
// comment poster, dashboards, log scrapers) don't have to
// special-case empty input.
func writeEmptyReport(stdout, errOut io.Writer, f *flags) error {
	var body string
	switch f.format {
	case "md":
		body = "## Dangling Defaults Guard\n\n" +
			"### Summary\n\n" +
			"- Tenants in scope: **0**\n" +
			"- Errors: **0**\n" +
			"- Warnings: **0**\n" +
			"- Tenants passing (zero errors): **0**\n\n" +
			"_No tenants under the requested scope; defaults change is vacuously safe._\n"
	case "json":
		b, err := json.MarshalIndent(map[string]any{
			"config_dir":   f.configDir,
			"scope":        f.scopeDir,
			"source_files": []string{},
			"report": map[string]any{
				"findings": []any{},
				"summary": map[string]int{
					"total_tenants":       0,
					"errors":              0,
					"warnings":            0,
					"passed_tenant_count": 0,
				},
			},
		}, "", "  ")
		if err != nil {
			// MarshalIndent on a map literal of strings/ints can't
			// realistically fail — but propagating the error keeps
			// the contract honest for future callers who add
			// less-trivial fields.
			return fmt.Errorf("encode empty JSON report: %w", err)
		}
		body = string(b) + "\n"
	}

	if f.output == "" {
		_, err := io.WriteString(stdout, body)
		return err
	}
	abs, err := filepath.Abs(f.output)
	if err != nil {
		return fmt.Errorf("resolve --output %q: %w", f.output, err)
	}
	if err := os.WriteFile(abs, []byte(body), 0o644); err != nil {
		return fmt.Errorf("write --output %q: %w", abs, err)
	}
	fmt.Fprintf(errOut, "%s: wrote empty-scope report to %s\n", programName, abs)
	return nil
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}
