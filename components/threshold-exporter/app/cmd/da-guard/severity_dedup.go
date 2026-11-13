package main

// severity-dedup: each tenant's `_severity_dedup` mode as the exporter
// reads it, as JSON, for the route generator (#3621) — which renders a
// tenant's static severity-dedup inhibit rule from that mode, and used to
// read the key with a Python word list and merge of its own.
//
// ⛔ NO SEMANTICS OF ITS OWN. Every field is the exporter's own load's:
//   - the tree is read by config.ScanDirTree and config.LoadDirFromScan —
//     the walk and post-walk half of the exporter's load, the one
//     key-verdict reads (no Gather, no served-values rows);
//   - mode: config.ThresholdConfig.ResolveSeverityDedupLogf on that
//     config, the resolver the exporter's collector runs — a tenant it gives
//     an entry is its Mode, a tenant it gives none is "disable" (its
//     contract: "Tenants with disable produce no entry"), as served-values'
//     `values._severity_dedup` reads it;
//   - problems: the load's LoadReport.ValueProblems whose key is
//     `_severity_dedup` (config.LayerValueProblems: each value the exporter
//     does not read, judged in the file and layer that writes it) — the one
//     source of key-verdict's value_unread for the key;
//   - schedules: the load's LoadReport.ScheduledReserved whose key is
//     `_severity_dedup`, in a `tenants:` section: each entry written with a
//     time window or expires, of which the exporter reads the `default`
//     alone (#3269). A `profiles:` entry is left out, as it is of problems:
//     the exporter does not apply a profile's `_severity_dedup` at all, not
//     even its default (#2914);
//   - parse_failed / unreadable: the files the load drops, as served-values
//     names them.
//
// TestSeverityDedup_MatchesServedValues keeps every mode equal to
// served-values' `values._severity_dedup` over the same tree.

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"reflect"
	"sort"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// severityDedupCmd is the subcommand name: `da-guard severity-dedup ...`.
const severityDedupCmd = "severity-dedup"

// severityDedupSchema names the document's shape (see servedValuesSchema):
// a reader refuses any other value.
const severityDedupSchema = "da-guard.severity-dedup/v1"

// severityDedupKey is the one key this subcommand reports on.
const severityDedupKey = "_severity_dedup"

// severityDedupDoc is the JSON document on stdout.
type severityDedupDoc struct {
	Schema string `json:"schema"`
	// Tenants: every tenant of the exporter's config → its mode.
	Tenants map[string]severityDedupTenant `json:"tenants"`
	// Problems: each `_severity_dedup` value the exporter does not read
	// (it serves enable for it, with a WARN), in the file and layer that
	// writes it. Always present ([] when none).
	Problems []severityDedupProblem `json:"problems"`
	// Schedules: each `_severity_dedup` written with a time window or
	// expires; the exporter reads its `default` alone. Always present ([]
	// when none).
	Schedules []severityDedupSchedule `json:"schedules"`
	// ParseFailed / Unreadable: the files the exporter's load drops, as
	// served-values names them (exit 3). Always present ([] when none).
	ParseFailed []string      `json:"parse_failed"`
	Unreadable  []skippedFile `json:"unreadable"`
}

// severityDedupTenant is one tenant's reading.
type severityDedupTenant struct {
	// Mode: "enable" or "disable".
	Mode string `json:"mode"`
}

// severityDedupProblem is one config.LayerValueProblem of the key.
type severityDedupProblem struct {
	File string `json:"file"`
	// Layer: config.ValueLayerTenant, ValueLayerPlatform or ValueLayerProfile.
	Layer string `json:"layer"`
	// Owner: the tenant id, or for the profile layer the profile name.
	Owner string `json:"owner"`
	// Problem: the exporter's own words, which quote the value as its
	// resolver compares it (trimmed, lower-cased).
	Problem string `json:"problem"`
	// Value: the value as the file wrote it (LayerValueProblem.Value), so a
	// reader can show it its own way — or not at all — apart from Problem.
	Value string `json:"value"`
}

// severityDedupSchedule is one config.ScheduledReservedEntry of the key.
type severityDedupSchedule struct {
	File string `json:"file"`
	// Section: "tenants" (a profiles: entry is not listed: the exporter
	// does not apply a profile's _severity_dedup).
	Section string `json:"section"`
	// Owner: the tenant id.
	Owner string `json:"owner"`
}

func severityDedupUsage(errOut io.Writer, fs *flag.FlagSet) func() {
	return func() {
		fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir>\n", programName, severityDedupCmd)
		fmt.Fprintf(errOut, "Print, as JSON, each tenant's _severity_dedup mode as the exporter reads it, with each\n"+
			"value it does not read and each value written with a schedule:\n"+
			"{\"schema\": %q, \"tenants\": {<tenant>: {\"mode\": \"enable\"|\"disable\"}},\n"+
			"\"problems\": [{file, layer, owner, problem, value}], \"schedules\": [{file, section, owner}],\n"+
			"\"parse_failed\": [<file>], \"unreadable\": [{file, reason}]}.\n"+
			"mode is the exporter's resolver's (a value it does not read is enable, as /metrics serves it);\n"+
			"problems are the values it does not read, judged in the file and layer that write them (layer\n"+
			"tenant, platform_tenants or profile; owner the tenant id or profile name); schedules are the\n"+
			"tenants: entries written with overrides:/expires:/reason:, of which the exporter reads the\n"+
			"default alone (a reserved key has no schedule); a profiles: entry is not listed, as the\n"+
			"exporter does not apply a profile's _severity_dedup at all.\n\n", severityDedupSchema)
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  ok\n"+
			"  2  caller error, or a tree the exporter's load rejects (e.g. a tenant declared twice, no\n"+
			"     .yaml file at all), or a conf.d path that is not valid UTF-8\n"+
			"  3  config files the exporter cannot decode or cannot read; the JSON is still written and\n"+
			"     names them in parse_failed / unreadable\n")
	}
}

// runSeverityDedup is the subcommand's entry point.
func runSeverityDedup(args []string, stdout, errOut io.Writer) int {
	fs := flag.NewFlagSet(programName+" "+severityDedupCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	configDir := fs.String("config-dir", "", "Path to the conf.d/ root. Required.")
	fs.Usage = severityDedupUsage(errOut, fs)
	if err := fs.Parse(args); errors.Is(err, flag.ErrHelp) {
		return exitOK
	} else if err != nil {
		return exitCallerErr
	}
	fail := func(err error) int {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, severityDedupCmd, err)
		return exitCallerErr
	}
	if fs.NArg() > 0 {
		return fail(fmt.Errorf("unexpected argument %q", fs.Arg(0)))
	}
	if *configDir == "" {
		return fail(errors.New("--config-dir is required"))
	}
	if refuseNonUTF8Tree(*configDir, programName+" "+severityDedupCmd, errOut) {
		return exitCallerErr // utf8_names.go (#3206)
	}
	doc, err := severityDedupOf(*configDir, log.New(errOut, "", 0), errOut)
	if err != nil {
		return fail(err)
	}
	if err := checkOutputUTF8("", reflect.ValueOf(doc)); err != nil {
		return fail(err)
	}
	b, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		return fail(fmt.Errorf("encode JSON: %v", err))
	}
	if _, err := stdout.Write(append(b, '\n')); err != nil {
		return fail(err)
	}
	if len(doc.ParseFailed) > 0 {
		reportParseFailed(errOut, doc.ParseFailed)
	}
	if len(doc.Unreadable) > 0 {
		reportUnreadable(errOut, programName+" "+severityDedupCmd, doc.Unreadable)
	}
	if len(doc.ParseFailed) > 0 || len(doc.Unreadable) > 0 {
		return exitParseFailed
	}
	return exitOK
}

// severityDedupOf is the document for the tree at dir. The load logs on
// logger, as served-values' does (a file in parse_failed is named with the
// exporter's reason); a root the walk cannot list is said on errOut. An
// error is a tree the exporter's load rejects (exit 2).
func severityDedupOf(dir string, logger *log.Logger, errOut io.Writer) (severityDedupDoc, error) {
	doc := severityDedupDoc{Schema: severityDedupSchema, Tenants: map[string]severityDedupTenant{},
		Problems: []severityDedupProblem{}, Schedules: []severityDedupSchedule{},
		ParseFailed: []string{}, Unreadable: []skippedFile{}}
	scan, err := config.ScanDirTree(dir, nil, nil, logger)
	if err != nil {
		return doc, err
	}
	cfg, rep, _, err := config.LoadDirFromScan(scan, dir, logger)
	if rep.RootListErr != nil {
		// The reason; the root itself is named in unreadable as "." (#2627).
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, severityDedupCmd, rep.RootListErr)
	}
	if errors.Is(err, config.ErrNoYAMLFiles) && len(rep.Unreadable) > 0 {
		// Every config file unreadable (loadJSONTree's allUnreadable): no
		// tenant, the files named (exit 3).
		doc.Unreadable = unreadableEntries(rep.Unreadable)
		return doc, nil
	}
	if err != nil {
		return doc, err
	}
	if err := checkUTF8(cfg); err != nil {
		return doc, err
	}
	for tenant := range cfg.Tenants {
		doc.Tenants[tenant] = severityDedupTenant{Mode: "disable"}
	}
	for _, d := range cfg.ResolveSeverityDedupLogf(nil) {
		doc.Tenants[d.Tenant] = severityDedupTenant{Mode: d.Mode}
	}
	for _, p := range rep.ValueProblems {
		if p.Key == severityDedupKey {
			doc.Problems = append(doc.Problems, severityDedupProblem{File: p.File, Layer: p.Layer,
				Owner: p.Owner, Problem: p.Problem, Value: p.Value})
		}
	}
	for _, e := range rep.ScheduledReserved {
		if e.Key == severityDedupKey && e.Section == "tenants" {
			doc.Schedules = append(doc.Schedules, severityDedupSchedule{File: e.File, Section: e.Section,
				Owner: e.Owner})
		}
	}
	// Both already sorted by file (LayerValueProblems, ScheduledReserved);
	// kept stable whatever the source's order becomes.
	sort.SliceStable(doc.Problems, func(i, j int) bool { return doc.Problems[i].File < doc.Problems[j].File })
	sort.SliceStable(doc.Schedules, func(i, j int) bool { return doc.Schedules[i].File < doc.Schedules[j].File })
	doc.ParseFailed = nonNilStrings(rep.ParseFailed)
	doc.Unreadable = unreadableEntries(rep.Unreadable)
	return doc, nil
}
