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
	"strings"

	"github.com/vencil/threshold-exporter/internal/guard"
	"github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/routingpolicy"
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
			"(e.g. 'thresholds.cpu,_routing.receiver.type'). A '_routing' or '_routing.*' path is "+
			"judged against the tenant's RESOLVED routing (_routing_defaults -> routing profile -> "+
			"the tenant's _routing, what the route generator renders), not the effective config. "+
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
		fmt.Fprintf(errOut, "Validate a conf.d/ tree against the C-12 Dangling Defaults Guard.\n")
		fmt.Fprintf(errOut, "'%s' prints the values the exporter's /metrics serves per tenant, as JSON "+
			"(see '%s %s -h').\n", servedValuesCmd, programName, servedValuesCmd)
		fmt.Fprintf(errOut, "'%s' prints every tenant's effective config as tenant-api's /effective resolves it, "+
			"with profile binding and per-key sources, as JSON (see '%s %s -h').\n\n", effectiveCmd, programName, effectiveCmd)
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  clean\n  1  guard found errors\n  2  caller error\n"+
			"  3  config files the exporter cannot decode or cannot read; the report names them (fix, re-run)\n")
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
	// run() never touches the process-global `log` (see main, #2444).
	if len(args) > 0 && args[0] == servedValuesCmd {
		return runServedValues(args[1:], stdout, errOut)
	}
	if len(args) > 0 && args[0] == effectiveCmd {
		return runEffective(args[1:], stdout, errOut)
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
			if werr := writeDecodeStopReport(stdout, errOut, f, de, unreadable); werr != nil {
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

	// No tenants in scope: this is "vacuously safe". Print a friendly
	// message in the chosen format and exit clean. We deliberately
	// don't return exitCallerErr here because GitHub Actions wrappers
	// run the guard on every _defaults.yaml change — and a defaults
	// file under a directory with no tenants yet (e.g. brand-new
	// domain skeleton) is a real, valid scenario.
	//
	// ⛔ Unless a file in scope failed the exporter's decode (#2123) or
	// could not be read at all (#2588): the walker skipped it, so "no
	// tenants" may be exactly the tenants that file declares. That is exit
	// 3, never the vacuous 0.
	if len(scoped.Tenants) == 0 {
		if err := writeEmptyReport(stdout, errOut, f, scoped.ParseFailed, scoped.Unreadable); err != nil {
			fmt.Fprintf(errOut, "%s: %v\n", programName, err)
			return exitCallerErr
		}
		return reportDroppedFiles(errOut, scoped, exitOK)
	}

	input := buildCheckInput(scoped, f)
	report, err := guard.CheckDefaultsImpact(input)
	if err != nil {
		fmt.Fprintf(errOut, "%s: guard run: %v\n", programName, err)
		return exitCallerErr
	}

	if err := writeReport(stdout, errOut, f, scoped, report, notices); err != nil {
		fmt.Fprintf(errOut, "%s: %v\n", programName, err)
		return exitCallerErr
	}

	// Before the findings: see the exit-code contract at the top (3 wins
	// over 1, the findings cover only the files the exporter can read).
	if code := reportDroppedFiles(errOut, scoped, exitOK); code != exitOK {
		return code
	}
	if report.Summary.Errors > 0 {
		return exitFindings
	}
	if f.warnAsError && report.Summary.Warnings > 0 {
		return exitFindings
	}
	return exitOK
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

// pyyamlOwn is the tenant file's block Resolve reads for ec, with the
// receivers, `routes[i].match` values and override `alertname` /
// `metric_group` of its `_routing` as the route generator's PyYAML reads
// them (#2295, #2431): the exporter's merge decodes the file with yaml.v3,
// which keeps a plain `on` a string that PyYAML reads as a boolean. Nothing
// else in the block changes; ec.TenantOverridesRaw is not modified.
// pyRouting holds routingpolicy.PyYAMLRoutingByTenant per source file (nil:
// unreadable).
// ⛔ A `_routing` with no PyYAML counterpart (the file not re-read, the
// tenant or its `_routing` not found there) is not judged as yaml.v3 read
// it: WithPyYAMLRouting(r, nil) makes each of those values Unmatched /
// UnmatchedValue, refused.
func pyyamlOwn(ec *config.EffectiveConfig, pyRouting map[string]map[string]any) map[string]any {
	own := ec.TenantOverridesRaw
	r, has := own["_routing"]
	if !has {
		return own
	}
	py := pyRouting[ec.SourceFile][ec.TenantID]
	out := make(map[string]any, len(own))
	for k, v := range own {
		out[k] = v
	}
	out["_routing"] = routingpolicy.WithPyYAMLRouting(r, py)
	return out
}

// buildCheckInput assembles a guard.CheckInput from the scoped
// resolution. It's where the YAML-shape → guard-input mapping lives:
//
//   - EffectiveConfigs[id]      ← ec.EffectiveConfig
//   - RoutingByTenant[id]       ← the tenant's RESOLVED routing (#2280):
//     routingpolicy.Resolve over the tenant file's `_routing` /
//     `_routing_profile` laid over the root platform overlay's (#2291,
//     never the effective config) and the `_routing_defaults` and routing
//     profiles of the conf.d root and of every directory level down to the
//     tenant's (#2326) — the same three layers the route generator merges.
//     `--required-fields _routing.*` is judged
//     against it too.
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
	provenance := make(map[string]routingpolicy.Provenance)
	unknownProfiles := make(map[string]string)
	disabled := make(map[string]bool)
	notMapping := make(map[string]any)
	invalidIDs := make(map[string]string)
	tenantOverrides := make(map[string]map[string]any)
	newDefaultsByTenant := make(map[string]map[string]any)

	// #2280: the routing layers and the domain policies come from the conf.d
	// tree at --config-dir (never --scope), as the route generator reads them.
	// A file the exporter already fails is skipped so it is named once, by
	// exit 3; what the loader cannot use comes back as PlatformProblems —
	// findings that skip only the checks depending on them (#1654), never
	// entries in ParseFailed.
	failed := make(map[string]bool, len(scoped.ParseFailed))
	for _, pf := range scoped.ParseFailed {
		failed[pf] = true
	}
	skip := func(rel string) bool { return failed[rel] }
	// #2326: the WHOLE tree — `_routing_defaults` along each tenant's
	// directory chain, profiles and domain policies scoped to their subtree
	// (ADR-017 amendment 2026-09-28), as the route generator reads it. Read
	// from --config-dir even for a --scope run: the generator refuses the
	// whole tree on a blocking shape anywhere in it, so a scoped run reports
	// it too.
	tree, policies, problems := routingpolicy.LoadTree(f.configDir, skip)
	// #2291: routing written where the generator never reads it (a defaults
	// block, a threshold profile) — the exporter merges it into the
	// effective config, but no route is rendered from it.
	problems = append(problems, routingpolicy.UnreadRouting(f.configDir, scoped.DefaultsFiles, skip)...)

	// #2295 / #2431: each tenant file's `_routing` blocks as PyYAML reads
	// them, read once per file (a file declares many tenants, #2153); see pyyamlOwn.
	pyRouting := map[string]map[string]any{}
	for _, ec := range scoped.Tenants {
		if _, done := pyRouting[ec.SourceFile]; !done {
			// A read error leaves nil: every receiver of the file's tenants
			// is then Unmatched and refused (fail-closed), not judged as
			// yaml.v3 read it.
			data, err := os.ReadFile(filepath.Join(config.AbsScanRoot(f.configDir), filepath.FromSlash(ec.SourceFile)))
			if err == nil {
				pyRouting[ec.SourceFile] = routingpolicy.PyYAMLRoutingByTenant(data)
			} else {
				pyRouting[ec.SourceFile] = nil
			}
		}
	}

	for _, ec := range scoped.Tenants {
		effective[ec.TenantID] = ec.EffectiveConfig
		// The tenant's routing as the generator renders it: its own
		// `_routing` laid over the referenced profile over
		// `_routing_defaults`, `{{tenant}}` substituted. Tenants with no
		// routing (disabled, or no layer supplies anything) are absent from
		// the map — no finding, per guard/types.go.
		//
		// ⛔ #2291: the tenant layer is what the GENERATOR reads — the tenant
		// file's `_routing` / `_routing_profile` over the root platform
		// files' `tenants.<id>` entries (layers.TenantBlock) — never
		// ec.EffectiveConfig, which also carries the defaults chain and the
		// threshold profile. Routing there is never rendered (UnreadRouting
		// names it); reading it here judged routes that do not exist.
		// #2326: the layers of the tenant's own directory level.
		// #2341 R8: an id the routing plane refuses gets no routing at all —
		// the generator renders nothing for it (routingpolicy.IsValidTenantID).
		if !routingpolicy.IsValidTenantID(ec.TenantID) {
			invalidIDs[ec.TenantID] = ec.SourceFile
		} else {
			layers := tree.LayersFor(routingpolicy.LevelOf(ec.SourceFile))
			block := layers.TenantBlock(ec.TenantID, pyyamlOwn(ec, pyRouting))
			if routingpolicy.IsDisabled(block["_routing"]) {
				disabled[ec.TenantID] = true
			}
			if r, has := block["_routing"]; has && routingpolicy.RoutingNotMapping(r) {
				notMapping[ec.TenantID] = r // #2341 R5
			}
			resolved, ok, prov, unknown := routingpolicy.Resolve(ec.TenantID, block, layers)
			if ok {
				routing[ec.TenantID] = resolved
				provenance[ec.TenantID] = prov
			}
			if unknown != nil {
				unknownProfiles[ec.TenantID] = *unknown
			}
		}
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
	}

	required := splitNonEmpty(f.requiredFields)

	return guard.CheckInput{
		EffectiveConfigs:       effective,
		RequiredFields:         required,
		RoutingByTenant:        routing,
		RoutingProvenance:      provenance,
		RoutingDisabled:        disabled,
		RoutingNotMapping:      notMapping,
		InvalidTenantIDs:       invalidIDs,
		UnknownRoutingProfiles: unknownProfiles,
		DomainPolicies:         policies,
		PlatformProblems:       problems,
		RoutingEnforced:        tree.Enforced,
		TenantOverrides:        tenantOverrides,
		NewDefaultsByTenant:    newDefaultsByTenant,
		CardinalityLimit:       f.cardinalityLimit,
		CardinalityWarnRatio:   f.cardinalityWarnRatio,
		DefaultsFiles:          scoped.DefaultsFiles,
		ParseFailed:            scoped.ParseFailed,
		// #1976: the exporter's own build's undeliverable set, in-scope
		// tenants only (ScopeEffective filters it).
		UndeliverableInherited: scoped.Undeliverable,
		// #2388: reserved keys in the tenants' subtree defaults, in-scope
		// tenants only (ScopeEffective filters it).
		SubtreeReservedKeys:  scoped.SubtreeReserved,
		DeclaredStateFilters: scoped.DeclaredStateFilters,
		// #2388 A: the overlay's own verdict on each such key.
		SubtreeReservedApplied: scoped.SubtreeReservedApplied,
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
func writeReport(stdout, errOut io.Writer, f *flags, scoped *config.ScopedTenants, report *guard.GuardReport, notices []string) error {
	var body string
	switch f.format {
	case "md":
		body = renderMarkdown(scoped, report, notices)
	case "json":
		b, err := json.MarshalIndent(struct {
			ConfigDir   string             `json:"config_dir"`
			Scope       string             `json:"scope,omitempty"`
			SourceFiles []string           `json:"source_files"`
			Notices     []string           `json:"notices,omitempty"`
			ParseFailed []string           `json:"parse_failed,omitempty"`
			Unreadable  []skippedFile      `json:"unreadable,omitempty"`
			Report      *guard.GuardReport `json:"report"`
		}{
			ConfigDir:   f.configDir,
			Scope:       f.scopeDir,
			SourceFiles: scoped.SourceFiles,
			Notices:     notices,
			ParseFailed: scoped.ParseFailed,
			Unreadable:  unreadableOrNil(scoped.Unreadable),
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
func renderMarkdown(scoped *config.ScopedTenants, report *guard.GuardReport, notices []string) string {
	var b strings.Builder
	for _, n := range notices {
		b.WriteString("> ⚠️ " + n + "\n\n")
	}
	b.WriteString(parseFailedMarkdown(scoped.ParseFailed))
	b.WriteString(unreadableMarkdown(scoped.Unreadable))
	body := report.Markdown()
	if len(scoped.ParseFailed) > 0 || len(scoped.Unreadable) > 0 {
		// #2123: the guard library's all-clear line says the change is safe
		// to merge; with files the exporter cannot decode it is not. Swapped
		// here, not in GuardReport.Markdown(), so no other caller's output
		// changes. TestRun_ParseFailedFileBesideGoodFile_ExitsThree asserts
		// "safe to merge" is absent, so a reworded library line cannot slip
		// past this replacement unseen.
		body = strings.Replace(body, noFindingsSafeLine, noFindingsNotSafeLine, 1)
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
func writeEmptyReport(stdout, errOut io.Writer, f *flags, parseFailed []string, unreadable []config.UnreadableFile) error {
	var body string
	switch f.format {
	case "md":
		verdict := "_No tenants under the requested scope; defaults change is vacuously safe._\n"
		if len(parseFailed) > 0 || len(unreadable) > 0 {
			// #2123, #2588: not "safe" — the tenants may be in the files listed.
			verdict = "_No tenant could be checked under the requested scope; " +
				"the files listed above cannot be decoded or read, so this is NOT a safe result._\n"
		}
		body = parseFailedMarkdown(parseFailed) + unreadableMarkdown(unreadable) +
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
func writeDecodeStopReport(stdout, errOut io.Writer, f *flags, de *config.DecodeError, unreadable []skippedFile) error {
	files := []string{de.Path}
	var body string
	switch f.format {
	case "md":
		body = parseFailedMarkdown(files) + unreadableMarkdownEntries(unreadable) +
			"## Dangling Defaults Guard\n\n" +
			"_Stopped before checking any tenant: resolving the scope hit the file above " +
			"(`" + de.Err.Error() + "`). The run stops at the first such file; fix it and re-run._\n"
	case "json":
		doc := map[string]any{
			"config_dir":   f.configDir,
			"scope":        f.scopeDir,
			"source_files": []string{},
			"parse_failed": files,
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
)

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
