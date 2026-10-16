// da-guard is the CLI wrapper around internal/guard.CheckDefaultsImpact.
//
// It's the v2.8.0 Phase .c C-12 PR-4 deliverable: the first PR that
// turns the guard library (PR-1 schema, PR-2 routing, PR-3
// cardinality) into a customer-runnable binary. Since #2904 the CLI no
// longer runs the routing checks: the route generator judges routing and
// domain policy (exit 4 below). Customers run it
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
//	3  config files the exporter drops (#2123, #2179; the one definition
//	   of which files: docs/cli-reference.md §guard): the files
//	   ScopeEffective reports in ParseFailed — the exporter's own load's
//	   parse-failure list, kept when the file bears on --scope — plus the
//	   files the route generator refuses whole for a repeated key yaml.v3
//	   lets through (#2295, withGeneratorDuplicates) — or a
//	   config.DecodeError met while resolving — plus the paths the walk
//	   cannot stat, read or list that bear on --scope
//	   (ScopedTenants.Unreadable, #2588: the tenants and defaults in them
//	   are absent from what is checked). Independent of
//	   --cardinality-limit. The report names them (relative to
//	   --config-dir). A DecodeError stops the run before any tenant is
//	   checked, so only that first undecodable file is named (the
//	   unreadable paths still are).
//	4  undetermined (#2904, ADR-036 §9 step 2): the tree — the whole of
//	   --config-dir, whatever --scope says — holds what only the route
//	   generator judges (undetermined.go: a routing key at any depth, a
//	   domain policy or routing profiles file, a file parse-tree names
//	   unreadable, a tenant id the tenant-id rule refuses or two tenant files
//	   declare), and this check
//	   no longer judges routing or domain policy: it reports no routing
//	   finding at all. The report lists the evidence (`undetermined`, always
//	   present in the JSON, [] when there is none); the remedy is
//	   `da-tools generate-routes --validate --strict`. A tree without any of
//	   it is 0 as before — an empty scope included.
//
// Precedence 3 > 1 > 4 > 0. 1 wins over 4: a threshold error is
// actionable without the generator; --warn-as-error affects 1 only.
// `--required-fields` with a path under a routing key is a caller error (2),
// never a check that silently passes.
//
// 3 wins over 1: findings computed over a tree with a skipped or unreadable
// file describe only part of it, so "fix the file first" is the one
// actionable answer. 3 also replaces the vacuously-safe 0 of an
// empty scope — a scope whose only tenant file is broken has no
// tenants to check, and that is not "safe" — nor is a scope with paths the
// walk cannot stat, read or list (3, #2588). A --config-dir the walk cannot
// list at all is a path it cannot read too: 3, the root named in unreadable
// as "." (walk_error) and the walk's reason on stderr, as for served-values
// and effective (#2627; it was 2 before — never the vacuously-safe 0).
// A --config-dir or --scope whose stat fails is split by
// config.StatErrIsWrongPath (#2627): ENOENT, ENOTDIR or ELOOP is a wrong
// path, a caller error (2); any other failure (permission denied, EIO, a
// name too long, …) is a path that cannot be read: 3, named in unreadable
// as stat_error ("." for --config-dir), as the walker records any failed
// stat of an entry. A --scope that resolves, through a symlink, outside
// --config-dir is a caller error (2) even when the target cannot be read.
//
// Warnings never affect exit code (`--warn-as-error` flips this if
// a customer wants strict mode).
//
// Subcommand `served-values` (#2115, served_values.go) prints what /metrics
// serves per tenant as JSON, for the Python readers. Same exit codes: 0 ok,
// 2 caller error or a tree the exporter's load rejects, 3 files the load
// skips or cannot read (the JSON is still written, naming them in
// parse_failed / unreadable).
//
// Subcommand `effective` (#2564, effective.go) prints every tenant's
// effective config as tenant-api's /effective resolves it, with profile
// binding and per-key sources, as JSON. Same exit codes as served-values
// (3 also for paths the walk cannot read, named in unreadable, #2588).
//
// Subcommand `key-refs` (#1822, key_refs.go) prints, as JSON, every key of a
// tree whose platform key is one of the --metric keys as ValidateTenantKeys
// judges it (retired alias spellings, `_critical`, dimensional keys), beside
// the load's parse_failed / unreadable, for deprecate_rule's pre-check. Same
// exit codes as served-values.
//
// Subcommand `parse-tree` (#2897, parse_tree.go) prints, as JSON, which
// conf.d files the exporter and the route generator read, every readable
// file's content as yaml.v3 decodes it, and the files that cannot be read,
// each with a fixed kind (ADR-036 §2); with --path / --input, the same for
// one text meant for one path. Exit codes: 0 ok (an unreadable file is data,
// not a verdict), 2 caller error.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/internal/guard/findingkind"
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
	exitOK          = 0
	exitFindings    = 1
	exitCallerErr   = 2
	exitParseFailed = 3
	// exitUndetermined: the tree holds what only the route generator judges
	// (routing, domain policy; undetermined.go), which this check does not
	// (#2904, ADR-036 §9). Below 3 and 1, above 0.
	exitUndetermined = 4
)

// flags is the parsed configuration for one run.
type flags struct {
	configDir            string
	scopeDir             string
	requiredFields       string
	cardinalityLimit     int
	cardinalityLimitSet  bool // --cardinality-limit given explicitly (else: root _defaults.yaml)
	baselineConfigDir    string
	cardinalityWarnRatio float64
	format               string
	output               string
	warnAsError          bool
	showVersion          bool
}

func parseFlags(args []string, errOut io.Writer) (*flags, error) {
	fs := flag.NewFlagSet(programName, flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &flags{}

	fs.StringVar(&f.configDir, "config-dir", "",
		"Path to the conf.d/ root. Required. Defaults chains anchor here.")
	fs.StringVar(&f.scopeDir, "scope", "",
		"Subdirectory under --config-dir to limit validation. Empty = whole tree. "+
			"A relative path is relative to --config-dir, not to the working directory "+
			"(--config-dir conf.d --scope db; '.' = the whole tree); an absolute path is used as is. "+
			"Typical use in a CI hook: the changed _defaults.yaml's directory, relative to --config-dir (or absolute).")
	fs.StringVar(&f.requiredFields, "required-fields", "",
		"Comma-separated dotted paths every tenant's effective config must have "+
			"(e.g. 'thresholds.cpu'). A path under a routing key ('_routing', '_routing.*', "+
			"'_severity_dedup', ...) is refused (exit 2): routing is judged by "+
			"'da-tools generate-routes --validate --strict', not here. "+
			"Empty disables the schema check.")
	fs.IntVar(&f.cardinalityLimit, "cardinality-limit", 0,
		"Per-tenant predicted-metric-count ceiling; 0 disables. When omitted, the cap the exporter "+
			"enforces is used: max_metrics_per_tenant from the ROOT _defaults.yaml of --config-dir "+
			"(unset/0 = 500, negative = no check).")
	fs.StringVar(&f.baselineConfigDir, "baseline-config-dir", "",
		"The same conf.d/ tree BEFORE the change (e.g. the PR base). When set and the root "+
			"_defaults.yaml's max_metrics_per_tenant is raised or disabled relative to it, the report "+
			"opens with a notice. Never affects the exit code.")
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
	// -h / -help / --help are deliberately NOT registered: the flag package
	// then prints fs.Usage and returns flag.ErrHelp, which run() maps to
	// exit 0. Registering them as bools (before #2179) swallowed the usage.

	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s [flags]\n", programName)
		fmt.Fprintf(errOut, "       %s %s --config-dir <dir> [--at <RFC3339>]\n", programName, servedValuesCmd)
		fmt.Fprintf(errOut, "       %s %s --config-dir <dir>\n", programName, effectiveCmd)
		fmt.Fprintf(errOut, "       %s %s --config-dir <dir> --metric <key> [--metric <key> ...]\n", programName, keyRefsCmd)
		fmt.Fprintf(errOut, "       %s %s --config-dir <dir> [--path <rel> --input <file|->]\n", programName, parseTreeCmd)
		fmt.Fprintf(errOut, "Validate a conf.d/ tree against the C-12 Dangling Defaults Guard.\n")
		fmt.Fprintf(errOut, "'%s' prints the values the exporter's /metrics serves per tenant, as JSON "+
			"(see '%s %s -h').\n", servedValuesCmd, programName, servedValuesCmd)
		fmt.Fprintf(errOut, "'%s' prints every tenant's effective config as tenant-api's /effective resolves it, "+
			"with profile binding and per-key sources, as JSON (see '%s %s -h').\n", effectiveCmd, programName, effectiveCmd)
		fmt.Fprintf(errOut, "'%s' prints every key that belongs to the given metric keys as the exporter judges it "+
			"(retired alias spellings, _critical, dimensional keys), as JSON (see '%s %s -h').\n",
			keyRefsCmd, programName, keyRefsCmd)
		fmt.Fprintf(errOut, "'%s' prints which conf.d files each reader reads and what each holds as yaml.v3 decodes it, "+
			"or why it cannot be read, as JSON (see '%s %s -h').\n\n", parseTreeCmd, programName, parseTreeCmd)
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  clean\n  1  guard found errors\n  2  caller error\n"+
			"  3  config files the exporter cannot decode or cannot read; the report names them (fix, re-run)\n"+
			"  4  routing or domain policy in the tree, which this check does not judge; the report names it\n"+
			"     (run 'da-tools generate-routes --validate --strict'). 3 wins over 1, 1 over 4.\n")
	}

	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	fs.Visit(func(fl *flag.Flag) {
		if fl.Name == "cardinality-limit" {
			f.cardinalityLimitSet = true
		}
	})
	return f, nil
}

// run is the testable entry point. Returns an exit code; main()
// passes it to os.Exit. errOut receives caller-facing diagnostics
// (usage errors, exception messages); the report itself goes to
// either stdout (if --output empty) or the named file.
func run(args []string, stdout, errOut io.Writer) int {
	return runWith(args, stdout, errOut, guard.CheckDefaultsImpact)
}

// checkFunc is guard.CheckDefaultsImpact's shape; runWith takes it so a test
// can hand the CLI a report no real check produces (a finding with an
// unregistered kind, #2904).
type checkFunc func(guard.CheckInput) (*guard.GuardReport, error)

// runWith is run with the guard check passed in.
func runWith(args []string, stdout, errOut io.Writer, check checkFunc) int {
	// run() never touches the process-global `log` (see main, #2444).
	if len(args) > 0 && args[0] == servedValuesCmd {
		return runServedValues(args[1:], stdout, errOut)
	}
	if len(args) > 0 && args[0] == effectiveCmd {
		return runEffective(args[1:], stdout, errOut)
	}
	if len(args) > 0 && args[0] == keyRefsCmd {
		return runKeyRefs(args[1:], stdout, errOut)
	}
	if len(args) > 0 && args[0] == parseTreeCmd {
		return runParseTree(args[1:], os.Stdin, stdout, errOut)
	}

	f, err := parseFlags(args, errOut)
	if errors.Is(err, flag.ErrHelp) {
		// -h / -help / --help: the flag package already printed fs.Usage
		// to errOut; asking for help is not an error.
		return exitOK
	}
	if err != nil {
		// flag.ContinueOnError already printed the message; just
		// return the code.
		return exitCallerErr
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

	if field := routingRequiredField(splitNonEmpty(f.requiredFields)); field != "" {
		fmt.Fprintf(errOut, "%s: --required-fields %q: routing is no longer judged by da-guard; "+
			"require it in the route generator's checks instead (%s)\n", programName, field, undeterminedRemedy)
		return exitCallerErr
	}

	scoped, err := config.ScopeEffective(f.configDir, f.scopeDir)
	if err != nil {
		fmt.Fprintf(errOut, "%s: %v\n", programName, err)
		// #2123: a resolve failure that is one file failing the decode (a
		// `_defaults.yaml` in the chain, or a tenant file the walker accepted
		// but the merge rejects) is the author's to fix — exit 3, not 2.
		var de *config.DecodeError
		if errors.As(err, &de) {
			// ScopeEffective returns the files the walk could not read
			// beside a DecodeError (#2588); name them too.
			var unreadable []skippedFile
			if scoped != nil {
				unreadable = unreadableEntries(scoped.Unreadable)
			}
			// The evidence is the whole tree's, so the stopped run still lists
			// it (the exit code stays 3).
			undetermined := undeterminedEvidence(f.configDir, rootUnreadable(unreadable), errOut)
			if werr := writeDecodeStopReport(stdout, errOut, f, de, unreadable, undetermined); werr != nil {
				fmt.Fprintf(errOut, "%s: %v\n", programName, werr)
				return exitCallerErr
			}
			reportParseFailed(errOut, []string{de.Path})
			if len(unreadable) > 0 {
				reportUnreadable(errOut, programName, unreadable)
			}
			return exitParseFailed
		}
		return exitCallerErr
	}
	if scoped.RootListErr != nil {
		// The reason; the root itself is named in unreadable (exit 3, #2627).
		fmt.Fprintf(errOut, "%s: %v\n", programName, scoped.RootListErr)
	}
	withGeneratorDuplicates(f.configDir, scoped, errOut)
	undetermined := undeterminedEvidence(f.configDir, rootUnreadable(unreadableEntries(scoped.Unreadable)), errOut)

	// #2043: without an explicit --cardinality-limit, predict against the cap
	// the exporter will actually enforce for this tree, not a constant. The
	// root carrier is read from --config-dir, never from --scope: the cap is
	// one global value (#2028), so a scoped run must use the same one.
	if !f.cardinalityLimitSet {
		limit, source, err := config.RootMaxMetricsPerTenant(f.configDir)
		if err != nil {
			// #2179: a root carrier that fails the decode is a file the
			// exporter drops, so it is in scoped.ParseFailed and this run
			// ends in exit 3. The exporter then enforces the built-in cap
			// (nothing sets max_metrics_per_tenant), so predict against that.
			if !rootCarrierDropped(f.configDir, source, scoped.ParseFailed) {
				fmt.Fprintf(errOut, "%s: %v\n", programName, err)
				return exitCallerErr
			}
			limit = config.EffectiveMaxMetricsPerTenant(0)
			source = "built-in default (the root defaults file fails the exporter's decode, which drops it)"
		}
		if limit < 0 {
			limit = 0 // exporter: negative = no truncation; guard: 0 = no check
		}
		f.cardinalityLimit = limit
		if source == "" {
			source = "built-in default (root _defaults.yaml absent or unreadable)"
		}
		fmt.Fprintf(errOut, "%s: cardinality limit %d from %s (0 = no check; override with --cardinality-limit)\n",
			programName, limit, source)
	}

	notices := capChangeNotices(f)
	for _, n := range notices {
		fmt.Fprintf(errOut, "%s: NOTICE: %s\n", programName, n)
	}

	// The checks run whether or not the scope has tenants (#2991): what is
	// judged on the whole tree or on the scope's own files — the defaults
	// carriers, the /metrics gather verdict (#2031) — is reported with no
	// tenant exactly as with some. The per-tenant checks find nothing on zero
	// tenants.
	input := buildCheckInput(scoped, f)
	report, err := check(input)
	if err != nil {
		fmt.Fprintf(errOut, "%s: guard run: %v\n", programName, err)
		return exitCallerErr
	}

	// No tenants in scope and nothing found: this is "vacuously safe".
	// Print a friendly message in the chosen format and exit clean. We
	// deliberately don't return exitCallerErr here because GitHub Actions
	// wrappers run the guard on every _defaults.yaml change — and a
	// defaults file under a directory with no tenants yet (e.g. brand-new
	// domain skeleton) is a real, valid scenario.
	//
	// ⛔ Unless a file in scope failed the exporter's decode (#2123) or
	// could not be read at all (#2588): the walker skipped it, so "no
	// tenants" may be exactly the tenants that file declares. That is exit
	// 3, never the vacuous 0. Nor when the tree holds what only the route
	// generator judges (#2991, #2904): that is exit 4.
	if len(scoped.Tenants) == 0 && len(report.Findings) == 0 {
		if err := writeEmptyReport(stdout, errOut, f, scoped.ParseFailed, scoped.Unreadable, undetermined); err != nil {
			fmt.Fprintf(errOut, "%s: %v\n", programName, err)
			return exitCallerErr
		}
		return reportDroppedFiles(errOut, scoped, undeterminedExit(errOut, undetermined, exitOK))
	}

	spellFindingsAsWritten(report, scoped)
	// #2904: the report comes sealed (internal/guard/seal.go); its summary,
	// and so the exit code below, already counts these as errors.
	if n := report.UnregisteredKinds(); n > 0 {
		fmt.Fprintf(errOut, "%s: %d finding(s) carried no registered kind; reported as %q with severity error (a da-guard bug: please report it)\n",
			programName, n, findingkind.Unregistered())
	}

	if err := writeReport(stdout, errOut, f, scoped, report, notices, undetermined); err != nil {
		fmt.Fprintf(errOut, "%s: %v\n", programName, err)
		return exitCallerErr
	}

	// Before the findings: see the exit-code contract at the top (3 wins
	// over 1, the findings cover only the files the exporter can read; 1
	// wins over 4, a threshold error is actionable without the generator).
	code := exitOK
	if report.Summary.Errors > 0 || (f.warnAsError && report.Summary.Warnings > 0) {
		code = exitFindings
	}
	return reportDroppedFiles(errOut, scoped, undeterminedExit(errOut, undetermined, code))
}

// undeterminedExit: when the tree holds what only the route generator judges
// (undetermined.go), say so on stderr — whatever the exit code — and turn
// exitOK into exitUndetermined; any other code (1) is returned as is.
func undeterminedExit(errOut io.Writer, undetermined []undeterminedItem, code int) int {
	if len(undetermined) == 0 {
		return code
	}
	reportUndetermined(errOut, undetermined)
	if code == exitOK {
		return exitUndetermined
	}
	return code
}

// reportDroppedFiles: when the scope has files the exporter's load drops
// (ParseFailed) or the walk cannot read (Unreadable, #2588), name them on
// stderr and return exitParseFailed; otherwise return otherwise.
func reportDroppedFiles(errOut io.Writer, scoped *config.ScopedTenants, otherwise int) int {
	if len(scoped.ParseFailed) == 0 && len(scoped.Unreadable) == 0 {
		return otherwise
	}
	if len(scoped.ParseFailed) > 0 {
		reportParseFailed(errOut, scoped.ParseFailed)
	}
	if len(scoped.Unreadable) > 0 {
		reportUnreadable(errOut, programName, unreadableEntries(scoped.Unreadable))
	}
	return exitParseFailed
}

// rootCarrierDropped reports whether source (the root defaults carrier
// RootMaxMetricsPerTenant read, an absolute path) is one of the files the
// exporter drops (parseFailed, keys relative to configDir).
func rootCarrierDropped(configDir, source string, parseFailed []string) bool {
	if source == "" {
		return false
	}
	rel, err := filepath.Rel(config.AbsScanRoot(configDir), source)
	if err != nil {
		return false
	}
	key := filepath.ToSlash(rel)
	for _, pf := range parseFailed {
		if pf == key {
			return true
		}
	}
	return false
}

// buildCheckInput assembles a guard.CheckInput from the scoped
// resolution. It's where the YAML-shape → guard-input mapping lives:
//
//   - EffectiveConfigs[id]      ← ec.MergedConfig (the merge, not the
//     per-threshold view /effective reports: #2115 changed that view and
//     must not change this gate's findings)
//   - TenantOverrides[id]       ← ec.TenantOverridesRaw (PR-5)
//   - NewDefaultsByTenant[id]   ← ec.MergedDefaults      (PR-5)
//
// ⛔ No routing-plane input (#2904, ADR-036 §9 step 2): RoutingByTenant,
// the domain policies, the routing tree's problems, the invalid / unloaded
// tenant ids and `_routing_enforced` are left unset, so the CLI never reports
// a findingkind.PlaneRouting finding. What only the route generator judges
// is named as undetermined instead (undetermined.go, exit 4). The routing
// checks of internal/guard stay until ADR-036 step 3 (#2905) deletes them.
//
// We deliberately use NewDefaultsByTenant rather than NewDefaults: a
// scope spanning multiple cascading _defaults.yaml levels means
// different tenants inherit different merged-defaults views, and
// the guard's per-tenant resolution honors that. See
// guard.CheckInput documentation for the resolution rule.
func buildCheckInput(scoped *config.ScopedTenants, f *flags) guard.CheckInput {
	effective := make(map[string]map[string]any, len(scoped.Tenants))
	tenantOverrides := make(map[string]map[string]any)
	newDefaultsByTenant := make(map[string]map[string]any)
	var notServed map[string]map[string]config.NotServedKey

	for _, ec := range scoped.Tenants {
		effective[ec.TenantID] = ec.MergedConfig
		// PR-5: redundant-override warn-tier inputs. We populate
		// both fields as soon as the resolver hands them to us; an
		// absent tenant.yaml block (TenantOverridesRaw == nil) or
		// a defaults-less tree (MergedDefaults == nil) leaves the
		// per-tenant entry out of the map, which the guard treats
		// as "skip this tenant for redundant-override".
		if ec.TenantOverridesRaw != nil {
			// #2368: a threshold the tenant writes under BOTH #1231
			// spellings is not judged — deleting either spelling serves the
			// other one's value, which no inherited value can stand for.
			tenantOverrides[ec.TenantID] = config.WithoutDoubleSpelledThresholds(ec.TenantOverridesRaw)
		}
		if ec.MergedDefaults != nil {
			newDefaultsByTenant[ec.TenantID] = ec.MergedDefaults
		}
		// #2065: what /metrics does not serve as the effective config shows
		// it — ScopeEffective fills it as `da-guard effective` does.
		if len(ec.NotServed) > 0 {
			if notServed == nil {
				notServed = map[string]map[string]config.NotServedKey{}
			}
			notServed[ec.TenantID] = ec.NotServed
		}
	}

	return guard.CheckInput{
		EffectiveConfigs: effective,
		// runWith refuses a routing path here (exit 2) before this runs.
		RequiredFields:       splitNonEmpty(f.requiredFields),
		TenantOverrides:      tenantOverrides,
		NewDefaultsByTenant:  newDefaultsByTenant,
		CardinalityLimit:     f.cardinalityLimit,
		CardinalityWarnRatio: f.cardinalityWarnRatio,
		DefaultsFiles:        scoped.DefaultsFiles,
		ParseFailed:          scoped.ParseFailed,
		// #1976: the exporter's own build's undeliverable set, in-scope
		// tenants only (ScopeEffective filters it).
		UndeliverableInherited: scoped.Undeliverable,
		// #2388: reserved keys in the tenants' subtree defaults, in-scope
		// tenants only (ScopeEffective filters it).
		SubtreeReservedKeys:  scoped.SubtreeReserved,
		DeclaredStateFilters: scoped.DeclaredStateFilters,
		// #2388 A: the overlay's own verdict on each such key.
		SubtreeRefusedVerdicts: scoped.SubtreeRefusedVerdicts,
		RootNullUndeclared:     scoped.RootNullUndeclared,
		// #2708: nulls inside a schedule with windows, in-scope only.
		ScheduleNulls: scoped.ScheduleNulls,
		// #2065: the effective configs' not_served, in-scope tenants only.
		ValuesNotServed: notServed,
		// #2031: the exporter's own build drops these entries.
		CustomAlertDuplicates: customAlertDuplicates(scoped),
		// #2031: the whole tree, whatever the scope — one series two keys
		// share fails the scrape for every tenant.
		MetricsNotGatherable: gatherVerdict(scoped.Config, time.Now(), writtenNamer(scoped.WrittenKeys)),
	}
}

// spellFindingsAsWritten re-spells, in place, each finding's Field and
// Message with its tenant's keys as the layer that supplied them wrote them
// (#2031): the guard reads maps keyed by the canonical dimensional spelling
// (config.EffectiveConfig.KeySpellings). A field below a key (`key.leaf`)
// keeps its leaf. In a message only a whole key is replaced — between
// backticks, or quoted whole with %q — never a key inside another one.
func spellFindingsAsWritten(report *guard.GuardReport, scoped *config.ScopedTenants) {
	byTenant := map[string]*config.EffectiveConfig{}
	for _, ec := range scoped.Tenants {
		if len(ec.KeySpellings) > 0 {
			byTenant[ec.TenantID] = ec
		}
	}
	if len(byTenant) == 0 {
		return
	}
	for i := range report.Findings {
		f := &report.Findings[i]
		ec := byTenant[f.TenantID]
		if ec == nil {
			continue
		}
		for canon, written := range ec.KeySpellings {
			if f.Field == canon || strings.HasPrefix(f.Field, canon+".") {
				f.Field = written + f.Field[len(canon):]
			}
			f.Message = strings.ReplaceAll(f.Message, "`"+canon+"`", "`"+written+"`")
			f.Message = strings.ReplaceAll(f.Message, strconv.Quote(canon), strconv.Quote(written))
		}
	}
}

// customAlertDuplicates is guard.CheckInput.CustomAlertDuplicates: the
// `_custom_alerts` entries the exporter's own build of the tree drops as
// duplicates of an earlier entry's series (config.CustomAlertDuplicates), for
// the in-scope tenants. nil when none.
func customAlertDuplicates(scoped *config.ScopedTenants) map[string][]config.CustomAlertDuplicate {
	if scoped.Config == nil {
		return nil
	}
	var out map[string][]config.CustomAlertDuplicate
	for _, ec := range scoped.Tenants {
		if d := config.CustomAlertDuplicates(ec.TenantID, scoped.Config.Tenants[ec.TenantID]); len(d) > 0 {
			if out == nil {
				out = map[string][]config.CustomAlertDuplicate{}
			}
			out[ec.TenantID] = d
		}
	}
	return out
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
func writeReport(stdout, errOut io.Writer, f *flags, scoped *config.ScopedTenants, report *guard.GuardReport,
	notices []string, undetermined []undeterminedItem) error {
	var body string
	switch f.format {
	case "md":
		body = renderMarkdown(scoped, report, notices, undetermined)
	case "json":
		b, err := json.MarshalIndent(struct {
			ConfigDir   string        `json:"config_dir"`
			Scope       string        `json:"scope,omitempty"`
			SourceFiles []string      `json:"source_files"`
			Notices     []string      `json:"notices,omitempty"`
			ParseFailed []string      `json:"parse_failed,omitempty"`
			Unreadable  []skippedFile `json:"unreadable,omitempty"`
			// #2904: always present, [] when there is none.
			Undetermined []undeterminedItem `json:"undetermined"`
			Report       *guard.GuardReport `json:"report"`
		}{
			ConfigDir:    f.configDir,
			Scope:        f.scopeDir,
			SourceFiles:  nonNilStrings(scoped.SourceFiles), // #2031: [] for a scope with no tenant
			Notices:      notices,
			ParseFailed:  scoped.ParseFailed,
			Unreadable:   unreadableOrNil(scoped.Unreadable),
			Undetermined: nonNilUndetermined(undetermined),
			Report:       report,
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
	fmt.Fprintf(errOut, "%s: wrote report to %s (errors=%d, warnings=%d, undetermined=%d)\n",
		programName, abs, report.Summary.Errors, report.Summary.Warnings, len(undetermined))
	return nil
}

// renderMarkdown wraps GuardReport.Markdown() with a small caller
// preamble (config dir, scanned files) so the PR-comment reader
// has the context they need without scrolling back to the workflow
// definition.
func renderMarkdown(scoped *config.ScopedTenants, report *guard.GuardReport, notices []string, undetermined []undeterminedItem) string {
	var b strings.Builder
	for _, n := range notices {
		b.WriteString("> ⚠️ " + n + "\n\n")
	}
	b.WriteString(parseFailedMarkdown(scoped.ParseFailed))
	b.WriteString(unreadableMarkdown(scoped.Unreadable))
	b.WriteString(undeterminedMarkdown(undetermined))
	body := report.Markdown()
	if len(scoped.ParseFailed) > 0 || len(scoped.Unreadable) > 0 {
		// #2123: the guard library's all-clear line says the change is safe
		// to merge; with files the exporter cannot decode it is not. Swapped
		// here, not in GuardReport.Markdown(), so no other caller's output
		// changes. TestRun_ParseFailedFileBesideGoodFile_ExitsThree asserts
		// "safe to merge" is absent, so a reworded library line cannot slip
		// past this replacement unseen.
		body = strings.Replace(body, noFindingsSafeLine, noFindingsNotSafeLine, 1)
	} else if len(undetermined) > 0 {
		// #2904: nor is it with routing nobody here judged.
		body = strings.Replace(body, noFindingsSafeLine, noFindingsUndeterminedLine, 1)
	}
	b.WriteString(body)
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
func writeEmptyReport(stdout, errOut io.Writer, f *flags, parseFailed []string, unreadable []config.UnreadableFile,
	undetermined []undeterminedItem) error {
	var body string
	switch f.format {
	case "md":
		verdict := "_No tenants under the requested scope; defaults change is vacuously safe._\n"
		switch {
		case len(parseFailed) > 0 || len(unreadable) > 0:
			// #2123, #2588: not "safe" — the tenants may be in the files listed.
			verdict = "_No tenant could be checked under the requested scope; " +
				"the files listed above cannot be decoded or read, so this is NOT a safe result._\n"
		case len(undetermined) > 0:
			// #2904: nor with routing nobody here judged.
			verdict = "_No tenants under the requested scope; the routing and domain policy listed above " +
				"are not judged here, so this is NOT a safe result (exit 4)._\n"
		}
		body = parseFailedMarkdown(parseFailed) + unreadableMarkdown(unreadable) + undeterminedMarkdown(undetermined) +
			"## Dangling Defaults Guard\n\n" +
			"### Summary\n\n" +
			"- Tenants in scope: **0**\n" +
			"- Errors: **0**\n" +
			"- Warnings: **0**\n" +
			"- Tenants passing (zero errors): **0**\n\n" +
			verdict
	case "json":
		doc := map[string]any{
			"config_dir":   f.configDir,
			"scope":        f.scopeDir,
			"source_files": []string{},
			"undetermined": nonNilUndetermined(undetermined),
			"report": map[string]any{
				"findings": []any{},
				"summary": map[string]int{
					"total_tenants":       0,
					"errors":              0,
					"warnings":            0,
					"passed_tenant_count": 0,
				},
			},
		}
		if len(parseFailed) > 0 {
			doc["parse_failed"] = parseFailed
		}
		if len(unreadable) > 0 {
			doc["unreadable"] = unreadableEntries(unreadable)
		}
		b, err := json.MarshalIndent(doc, "", "  ")
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

// writeDecodeStopReport is the report for a run that stopped while resolving
// the scope because one file failed the decode (#2123, exit 3). No tenant was
// checked: ScopeEffective fails the whole scope on the first such file, so
// the list names that file only — fix it and re-run to see the next one.
func writeDecodeStopReport(stdout, errOut io.Writer, f *flags, de *config.DecodeError, unreadable []skippedFile,
	undetermined []undeterminedItem) error {
	files := []string{de.Path}
	var body string
	switch f.format {
	case "md":
		body = parseFailedMarkdown(files) + unreadableMarkdownEntries(unreadable) + undeterminedMarkdown(undetermined) +
			"## Dangling Defaults Guard\n\n" +
			"_Stopped before checking any tenant: resolving the scope hit the file above " +
			"(`" + de.Err.Error() + "`). The run stops at the first such file; fix it and re-run._\n"
	case "json":
		doc := map[string]any{
			"config_dir":   f.configDir,
			"scope":        f.scopeDir,
			"source_files": []string{},
			"parse_failed": files,
			"undetermined": nonNilUndetermined(undetermined),
			"error":        de.Err.Error(),
			"report":       nil,
		}
		if len(unreadable) > 0 {
			doc["unreadable"] = unreadable
		}
		b, err := json.MarshalIndent(doc, "", "  ")
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
	fmt.Fprintf(errOut, "%s: wrote report to %s\n", programName, abs)
	return nil
}

// noFindingsSafeLine is internal/guard's all-clear line (render.go);
// noFindingsNotSafeLine replaces it when files failed the decode (#2123).
const (
	noFindingsSafeLine    = "✅ No findings — defaults change is safe to merge.\n"
	noFindingsNotSafeLine = "No findings among the tenants that were checked; the change cannot be " +
		"judged until the files listed above can be decoded and read (exit 3).\n"
	// noFindingsUndeterminedLine replaces it when the tree holds routing or
	// domain policy (#2904).
	noFindingsUndeterminedLine = "No threshold findings; routing and domain policy, listed above, are not " +
		"judged here (exit 4).\n"
)

// nonNilUndetermined: the reports' `undetermined` is always present, [] when
// there is none.
func nonNilUndetermined(items []undeterminedItem) []undeterminedItem {
	if items == nil {
		return []undeterminedItem{}
	}
	return items
}

// parseFailedMarkdown is the report block naming the files whose exporter
// YAML decode fails (#2123); "" when there are none, so a clean tree's
// report is byte-identical to before. Neutral on purpose: the list may hold
// a root `_defaults.yaml` outside --scope, or a tenant file the walker
// accepted and only the merge rejects — neither is "skipped by the exporter".
func parseFailedMarkdown(files []string) string {
	if len(files) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteString("### Files the exporter cannot parse\n\n")
	b.WriteString("The exporter cannot decode these files. Fix them and re-run (exit 3):\n\n")
	for _, f := range files {
		b.WriteString("- `" + f + "`\n")
	}
	b.WriteString("\n")
	return b.String()
}

// unreadableMarkdown is the report block naming the paths the walk cannot
// stat, read or list (#2588); "" when there are none, so a clean tree's
// report is unchanged.
func unreadableMarkdown(us []config.UnreadableFile) string {
	return unreadableMarkdownEntries(unreadableEntries(us))
}

func unreadableMarkdownEntries(files []skippedFile) string {
	if len(files) == 0 {
		return ""
	}
	var b strings.Builder
	b.WriteString("### Files the exporter cannot read\n\n")
	b.WriteString("The exporter cannot stat, read or list these paths and serves the tree without them, " +
		"so the tenants and defaults they hold are not checked. Fix them and re-run (exit 3):\n\n")
	for _, u := range files {
		b.WriteString("- `" + u.File + "` (" + u.Reason + ")\n")
	}
	b.WriteString("\n")
	return b.String()
}

// unreadableOrNil: the guard report's optional `unreadable` (omitted when
// there is none, like `parse_failed`).
func unreadableOrNil(us []config.UnreadableFile) []skippedFile {
	if len(us) == 0 {
		return nil
	}
	return unreadableEntries(us)
}

// reportParseFailed is the stderr line for exit 3, so a CI log names the
// files even when the report went to --output.
func reportParseFailed(errOut io.Writer, files []string) {
	fmt.Fprintf(errOut, "%s: %d file(s) cannot be decoded: %s — "+
		"fix them and re-run (exit 3)\n",
		programName, len(files), strings.Join(files, ", "))
}

// rootUnreadable reports whether entries name the --config-dir root itself
// ("."), whose reason the walk has already printed.
func rootUnreadable(entries []skippedFile) bool {
	for _, e := range entries {
		if e.File == "." {
			return true
		}
	}
	return false
}

// unreadableEntries is the JSON form of the walk's unreadable entries
// (served-values, effective and the guard report share it): never nil, so a
// field that is always present encodes as [] when there is none.
func unreadableEntries(us []config.UnreadableFile) []skippedFile {
	out := make([]skippedFile, 0, len(us))
	for _, u := range us {
		out = append(out, skippedFile{File: u.RelKey, Reason: u.Reason})
	}
	return out
}

// reportUnreadable is the stderr line for exit 3 when the walk could not
// stat, read or list paths (#2115, #2588), so a CI log names them even when
// the report went to --output. who is the program name, plus the subcommand
// for one.
func reportUnreadable(errOut io.Writer, who string, files []skippedFile) {
	names := make([]string, 0, len(files))
	for _, u := range files {
		names = append(names, u.File+" ("+u.Reason+")")
	}
	fmt.Fprintf(errOut, "%s: %d path(s) cannot be read: %s — fix them and re-run (exit 3)\n",
		who, len(names), strings.Join(names, ", "))
}

func main() {
	args := os.Args[1:]
	// The report path prints dependency WARNs (pkg/config, the collector —
	// they log through the process-global `log`) without timestamps.
	// ⛔ Set here, once, and never in run(): run() pointing the global logger
	// at its errOut made every t.Parallel test calling run() hand its buffer
	// to every other test's resolver, and -race failed whichever pair
	// overlapped (#2444). The global keeps the binary's own stderr.
	if len(args) == 0 || args[0] != servedValuesCmd {
		log.SetFlags(0)
	}
	os.Exit(run(args, os.Stdout, os.Stderr))
}
