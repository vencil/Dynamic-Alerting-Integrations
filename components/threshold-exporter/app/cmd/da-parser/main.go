// da-parser is the CLI wrapper around internal/parser.ParsePromRules.
//
// It is the v2.8.0 Phase .c C-8 PR-2 deliverable: turns the parser
// library (PR-1 core + PR-2 strict-PromQL gate + freshness allowlist)
// into a customer-runnable binary. Customers run it from a migration
// pipeline step or a one-shot triage:
//
//   - Convert kube-prometheus PrometheusRule YAML to ParseResult
//     JSON for downstream C-9 Profile Builder consumption.
//   - Verify a corpus is "Prometheus-portable" before committing
//     to MetricsQL (--validate-strict-prom).
//   - Block migrations on any rule using VM-only functions
//     (--fail-on-non-portable).
//
// Subcommands (dispatcher pattern, mirrors da-batchpr):
//
//	import       Parse PrometheusRule YAML(s) → JSON ParseResult.
//	allowlist    Print the embedded VM-only allowlist (introspection
//	             aid for customers writing their own rule audits).
//
// Exit codes (stable contract for CI YAML / hook scripts):
//
//	0  parse succeeded; no failures
//	1  one or more rules failed --fail-on-non-portable (or
//	   --fail-on-ambiguous) gate
//	2  caller error (bad flags, missing/invalid path, IO failure,
//	   malformed YAML)
//
// Strict PromQL validation is on by default — flip with
// `--validate-strict-prom=false` if a customer wants to skip the
// upstream parser cost (e.g. corpus already known VM-only).
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

	"github.com/vencil/threshold-exporter/internal/parser"
)

// Version is overridden at build time via `-ldflags "-X main.Version=..."`.
var Version = "dev"

// programName surfaces in usage / errors. Kept in a var so tests can
// swap it without disturbing os.Args[0].
var programName = "da-parser"

// Exit codes — referenced from tests too.
const (
	exitOK        = 0
	exitGateFail  = 1
	exitCallerErr = 2
)

// Subcommand names — central list keeps `--help` and the dispatcher
// in lock-step.
const (
	cmdImport    = "import"
	cmdAllowlist = "allowlist"
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

// run is the testable entry point. Dispatches the first arg to a
// subcommand's runX(). errOut receives caller-facing diagnostics;
// stdout receives the actual payload (JSON, allowlist, etc.).
func run(args []string, stdout, errOut io.Writer) int {
	log.SetOutput(errOut)
	log.SetFlags(0)

	if len(args) == 0 || isHelpArg(args[0]) {
		printRootUsage(errOut)
		return exitOK
	}
	if isVersionArg(args[0]) {
		fmt.Fprintf(stdout, "%s %s\n", programName, Version)
		return exitOK
	}

	switch args[0] {
	case cmdImport:
		return runImport(args[1:], stdout, errOut)
	case cmdAllowlist:
		return runAllowlist(args[1:], stdout, errOut)
	default:
		fmt.Fprintf(errOut, "%s: unknown subcommand %q\n", programName, args[0])
		fmt.Fprintf(errOut, "Run `%s --help` for usage.\n", programName)
		return exitCallerErr
	}
}

func isHelpArg(s string) bool { return s == "-h" || s == "--help" || s == "help" }
func isVersionArg(s string) bool {
	return s == "-V" || s == "--version" || s == "version"
}

func printRootUsage(w io.Writer) {
	fmt.Fprintf(w, "Usage: %s <subcommand> [flags]\n\n", programName)
	fmt.Fprintf(w, "Subcommands:\n")
	fmt.Fprintf(w, "  %-12s Parse PrometheusRule YAML into JSON ParseResult.\n", cmdImport)
	fmt.Fprintf(w, "  %-12s Print the embedded VM-only function allowlist.\n", cmdAllowlist)
	fmt.Fprintf(w, "\n")
	fmt.Fprintf(w, "Common flags (any subcommand):\n")
	fmt.Fprintf(w, "  -h, --help        Show this message.\n")
	fmt.Fprintf(w, "  -V, --version     Print version and exit.\n")
	fmt.Fprintf(w, "\n")
	fmt.Fprintf(w, "Run `%s <subcommand> --help` for subcommand-specific flags.\n", programName)
	fmt.Fprintf(w, "\n")
	fmt.Fprintf(w, "Exit codes: 0 OK, 1 portability gate failed, 2 caller error.\n")
}

// ────────────────────────────── import ──────────────────────────────

// importFlags holds the parsed configuration for the `import` subcommand.
type importFlags struct {
	input               string
	output              string
	generatedBy         string
	validateStrictProm  bool
	failOnNonPortable   bool
	failOnAmbiguous     bool
	help                bool
}

func parseImportFlags(args []string, errOut io.Writer) (*importFlags, error) {
	fs := flag.NewFlagSet(programName+" import", flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &importFlags{
		validateStrictProm: true, // PR-2 default — anti-vendor-lock-in promise.
	}
	fs.StringVar(&f.input, "input", "",
		"Path to PrometheusRule YAML. Required. Pass '-' to read from stdin.")
	fs.StringVar(&f.output, "output", "-",
		"Path to write JSON ParseResult. '-' = stdout (default).")
	fs.StringVar(&f.generatedBy, "generated-by", "",
		"Stamped into Provenance.GeneratedBy. Conventional: 'da-parser@<tag>'. "+
			"Empty = '"+programName+"@<binary-version>'.")
	fs.BoolVar(&f.validateStrictProm, "validate-strict-prom", true,
		"Run prometheus/promql/parser strict-compatibility check per rule. "+
			"Default true. Set false to skip the upstream parser cost.")
	fs.BoolVar(&f.failOnNonPortable, "fail-on-non-portable", false,
		"Exit 1 if any rule has prom_compatible=false. Implies --validate-strict-prom.")
	fs.BoolVar(&f.failOnAmbiguous, "fail-on-ambiguous", false,
		"Exit 1 if any rule has dialect=ambiguous (parse error). Off by default.")
	fs.BoolVar(&f.help, "help", false, "Print usage and exit.")
	fs.BoolVar(&f.help, "h", false, "Alias for --help.")

	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s import [flags]\n\n", programName)
		fmt.Fprintf(errOut, "Parse a PrometheusRule YAML file and emit a JSON ParseResult.\n\n")
		fs.PrintDefaults()
		fmt.Fprintf(errOut,
			"\nExit codes:\n"+
				"  0  parse OK, no gate failures\n"+
				"  1  --fail-on-non-portable / --fail-on-ambiguous gate triggered\n"+
				"  2  caller error (bad flags, IO, malformed YAML)\n")
	}

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	return f, nil
}

func runImport(args []string, stdout, errOut io.Writer) int {
	f, err := parseImportFlags(args, errOut)
	if err != nil {
		return exitCallerErr
	}
	if f.help {
		return exitOK
	}
	if f.input == "" {
		fmt.Fprintf(errOut, "%s import: --input is required (use '-' for stdin)\n", programName)
		return exitCallerErr
	}

	// --fail-on-non-portable requires the strict parser to be on,
	// otherwise prom_compatible is always false (zero value) and the
	// gate would trip even on pristine PromQL. Auto-elevate rather
	// than reject — saves the customer one back-and-forth.
	if f.failOnNonPortable && !f.validateStrictProm {
		fmt.Fprintf(errOut, "%s import: --fail-on-non-portable implies --validate-strict-prom; auto-enabling\n", programName)
		f.validateStrictProm = true
	}

	yamlBytes, sourceLabel, err := readInputBytes(f.input)
	if err != nil {
		fmt.Fprintf(errOut, "%s import: %v\n", programName, err)
		return exitCallerErr
	}

	generatedBy := f.generatedBy
	if generatedBy == "" {
		generatedBy = fmt.Sprintf("%s@%s", programName, Version)
	}

	res, err := parser.ParsePromRulesWithOptions(yamlBytes, sourceLabel, generatedBy,
		parser.ParseOptions{StrictPromQL: f.validateStrictProm})
	if err != nil {
		fmt.Fprintf(errOut, "%s import: %v\n", programName, err)
		return exitCallerErr
	}

	// Surface warnings before the JSON so a CI runner sees them
	// without having to parse the result body.
	for _, w := range res.Warnings {
		fmt.Fprintf(errOut, "%s import: warning: %s\n", programName, w)
	}

	if err := writeJSON(stdout, errOut, f.output, res); err != nil {
		fmt.Fprintf(errOut, "%s import: %v\n", programName, err)
		return exitCallerErr
	}

	if gate := evaluateGate(res, f); gate.tripped {
		fmt.Fprintf(errOut, "%s import: gate failed — %s\n", programName, gate.message)
		return exitGateFail
	}

	return exitOK
}

// ─── helpers ───

// readInputBytes returns (bytes, sourceFile-label, err). The
// returned label is what we stamp into Provenance.SourceFile and
// SourceRuleID prefixes — '-' for stdin, otherwise the input path.
func readInputBytes(input string) ([]byte, string, error) {
	if input == "-" {
		b, err := io.ReadAll(os.Stdin)
		if err != nil {
			return nil, "", fmt.Errorf("read stdin: %w", err)
		}
		return b, "<stdin>", nil
	}
	abs, err := filepath.Abs(input)
	if err != nil {
		return nil, "", fmt.Errorf("resolve --input %q: %w", input, err)
	}
	b, err := os.ReadFile(abs)
	if err != nil {
		return nil, "", fmt.Errorf("read %q: %w", abs, err)
	}
	return b, filepath.Base(input), nil
}

// writeJSON dumps `res` to either stdout or a file. Mirrors da-guard's
// IO contract — file path requires the directory to exist; file
// IO failures surface as caller errors.
func writeJSON(stdout, errOut io.Writer, output string, res *parser.ParseResult) error {
	body, err := json.MarshalIndent(res, "", "  ")
	if err != nil {
		return fmt.Errorf("encode JSON: %w", err)
	}
	body = append(body, '\n')
	if output == "-" {
		_, err := stdout.Write(body)
		return err
	}
	abs, err := filepath.Abs(output)
	if err != nil {
		return fmt.Errorf("resolve --output %q: %w", output, err)
	}
	if err := os.WriteFile(abs, body, 0o644); err != nil {
		return fmt.Errorf("write --output %q: %w", abs, err)
	}
	fmt.Fprintf(errOut, "%s import: wrote %d rules to %s\n", programName, len(res.Rules), abs)
	return nil
}

// gateOutcome separates the "tripped" boolean from the message so
// callers can format the message with their own framing. The plural
// counts surface in CI logs and let reviewers triage at a glance.
type gateOutcome struct {
	tripped bool
	message string
}

func evaluateGate(res *parser.ParseResult, f *importFlags) gateOutcome {
	var nonPortable, ambiguous int
	for _, r := range res.Rules {
		if r.Dialect == parser.DialectAmbiguous {
			ambiguous++
		}
		if f.validateStrictProm && !r.PromCompatible {
			nonPortable++
		}
	}
	var msgs []string
	if f.failOnAmbiguous && ambiguous > 0 {
		msgs = append(msgs, fmt.Sprintf("%d rule(s) failed parse (dialect=ambiguous)", ambiguous))
	}
	if f.failOnNonPortable && nonPortable > 0 {
		msgs = append(msgs, fmt.Sprintf("%d rule(s) failed strict PromQL (prom_compatible=false)", nonPortable))
	}
	if len(msgs) == 0 {
		return gateOutcome{}
	}
	return gateOutcome{tripped: true, message: strings.Join(msgs, "; ")}
}

// ──────────────────────────── allowlist ─────────────────────────────

func runAllowlist(args []string, stdout, errOut io.Writer) int {
	fs := flag.NewFlagSet(programName+" allowlist", flag.ContinueOnError)
	fs.SetOutput(errOut)
	var (
		format string
		help   bool
	)
	fs.StringVar(&format, "format", "text",
		"Output format: 'text' (one name per line) or 'json' (sorted array + version).")
	fs.BoolVar(&help, "help", false, "Print usage and exit.")
	fs.BoolVar(&help, "h", false, "Alias for --help.")

	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s allowlist [flags]\n\n", programName)
		fmt.Fprintf(errOut, "Print the embedded VM-only function allowlist.\n\n")
		fs.PrintDefaults()
	}
	if err := fs.Parse(args); err != nil {
		return exitCallerErr
	}
	if help {
		return exitOK
	}
	if format != "text" && format != "json" {
		fmt.Fprintf(errOut, "%s allowlist: --format must be 'text' or 'json' (got %q)\n", programName, format)
		return exitCallerErr
	}

	names := parser.VMOnlyFunctionNames()
	switch format {
	case "text":
		for _, n := range names {
			fmt.Fprintln(stdout, n)
		}
	case "json":
		body := struct {
			MetricsqlVersion string   `json:"metricsql_version"`
			Functions        []string `json:"functions"`
		}{
			MetricsqlVersion: parser.VMOnlyMetricsqlVersion(),
			Functions:        names,
		}
		b, err := json.MarshalIndent(body, "", "  ")
		if err != nil {
			fmt.Fprintf(errOut, "%s allowlist: encode JSON: %v\n", programName, err)
			return exitCallerErr
		}
		fmt.Fprintln(stdout, string(b))
	}
	return exitOK
}

