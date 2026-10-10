package main

// key-refs: which keys of a conf.d tree belong to the metrics a caller is
// about to retire, as JSON (#1822), for `deprecate_rule` — so that tool asks
// the exporter, instead of guessing, (i) whether the exporter reads the
// tree's files at all and (ii) which keys, in which spelling, still name a
// metric once its canonical key is gone.
//
// ⛔ NO SEMANTICS OF ITS OWN. parse_failed / unreadable are the exporter's
// own load's (config.LoadDirReport, as served-values reads them); every
// reference is config.KeyRefs', whose metric attribution is
// config.PlatformKeyFor — the classifier ValidateTenantKeys itself runs.

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"reflect"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// keyRefsCmd is the subcommand name: `da-guard key-refs ...`.
const keyRefsCmd = "key-refs"

// keyRefsSchema names this document's shape; a reader refuses any other.
const keyRefsSchema = "da-guard.key-refs/v1"

// keyRefsDoc is the JSON document on stdout. --config-dir is not echoed
// back (see servedValuesDoc).
type keyRefsDoc struct {
	Schema string `json:"schema"`
	// ParseFailed: the files the exporter's load skips because they do not
	// decode — served-values' parse_failed, read off the same load. A root
	// `_defaults.yaml` here means the exporter drops the whole carrier.
	// Always present ([] when none).
	ParseFailed []string `json:"parse_failed"`
	// Unreadable: the paths the load could not stat, read or list, as
	// served-values names them. Always present ([] when none).
	Unreadable []skippedFile `json:"unreadable"`
	// Unscanned: files the load kept whose keys could not be listed, each
	// with the reason; their references are unknown. Not a failure of the
	// tree (the exit code is unchanged). Always present ([] when none).
	Unscanned []skippedFile `json:"unscanned"`
	// Refs: every --metric → the keys whose platform key it is
	// (config.KeyRefs), each {file, section, owner, key}. Every --metric has
	// an entry ([] when nothing refers to it).
	Refs map[string][]config.KeyRef `json:"refs"`
}

// metricList is a repeatable --metric.
type metricList []string

func (m *metricList) String() string { return strings.Join(*m, ",") }
func (m *metricList) Set(v string) error {
	if v == "" {
		return errors.New("must not be empty")
	}
	*m = append(*m, v)
	return nil
}

func parseKeyRefsFlags(args []string, errOut io.Writer) (string, []string, error) {
	fs := flag.NewFlagSet(programName+" "+keyRefsCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	var configDir string
	var metrics metricList
	fs.StringVar(&configDir, "config-dir", "", "Path to the conf.d/ root. Required.")
	fs.Var(&metrics, "metric", "A platform metric key (canonical spelling) to list the references of. "+
		"Repeatable; at least one.")
	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir> --metric <key> [--metric <key> ...]\n", programName, keyRefsCmd)
		fmt.Fprintf(errOut, "Print, as JSON, every key of the tree whose platform key is one of the --metric keys,\n"+
			"as the exporter judges it (the check ValidateTenantKeys runs): the key itself, a retired\n"+
			"alias spelling, its _critical tier and its dimensional keys in any label spelling. Each\n"+
			"reference is {file, section (defaults|optional_overrides|tenants|profiles), owner, key}\n"+
			"with key as written. Files the exporter drops are named in parse_failed / unreadable and\n"+
			"their keys are not listed.\n\n")
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  ok\n  2  caller error, or a tree the exporter rejects (e.g. a tenant declared twice,\n"+
			"     no .yaml file at all), or any output string that is not valid UTF-8\n"+
			"  3  config files the exporter cannot decode or cannot read; the JSON is still written and\n"+
			"     names them in parse_failed / unreadable\n")
	}
	if err := fs.Parse(args); err != nil {
		return "", nil, err
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(errOut, "%s %s: unexpected argument %q\n", programName, keyRefsCmd, fs.Arg(0))
		return "", nil, errors.New("unexpected argument")
	}
	return configDir, metrics, nil
}

// runKeyRefs is the subcommand's entry point.
func runKeyRefs(args []string, stdout, errOut io.Writer) int {
	configDir, metrics, err := parseKeyRefsFlags(args, errOut)
	if errors.Is(err, flag.ErrHelp) {
		return exitOK
	}
	if err != nil {
		return exitCallerErr
	}
	if configDir == "" {
		fmt.Fprintf(errOut, "%s %s: --config-dir is required\n", programName, keyRefsCmd)
		return exitCallerErr
	}
	if len(metrics) == 0 {
		fmt.Fprintf(errOut, "%s %s: at least one --metric is required\n", programName, keyRefsCmd)
		return exitCallerErr
	}

	rep, err := config.KeyRefs(configDir, metrics, log.New(errOut, "", 0))
	// As served-values (#2627): a tree whose every config file is unreadable
	// is exit 3 with the files named, not the "no .yaml files" refusal.
	allUnreadable := errors.Is(err, config.ErrNoYAMLFiles) && len(rep.Load.Unreadable) > 0
	if err != nil && !allUnreadable {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, keyRefsCmd, err)
		return exitCallerErr
	}
	if rep.Load.RootListErr != nil {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, keyRefsCmd, rep.Load.RootListErr)
	}
	doc := keyRefsDoc{Schema: keyRefsSchema, ParseFailed: rep.Load.ParseFailed,
		Unreadable: unreadableEntries(rep.Load.Unreadable), Unscanned: []skippedFile{}, Refs: rep.Refs}
	if doc.ParseFailed == nil {
		doc.ParseFailed = []string{}
	}
	for file, reason := range rep.Unscanned {
		doc.Unscanned = append(doc.Unscanned, skippedFile{File: file, Reason: reason})
	}
	sort.Slice(doc.Unscanned, func(i, j int) bool { return doc.Unscanned[i].File < doc.Unscanned[j].File })

	if err := checkOutputUTF8("", reflect.ValueOf(doc)); err != nil {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, keyRefsCmd, err)
		return exitCallerErr
	}
	b, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		fmt.Fprintf(errOut, "%s %s: encode JSON: %v\n", programName, keyRefsCmd, err)
		return exitCallerErr
	}
	if _, err := stdout.Write(append(b, '\n')); err != nil {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, keyRefsCmd, err)
		return exitCallerErr
	}
	for _, u := range doc.Unscanned {
		fmt.Fprintf(errOut, "%s %s: WARN: keys of %s not listed: %s\n", programName, keyRefsCmd, u.File, u.Reason)
	}
	if len(doc.ParseFailed) > 0 {
		reportParseFailed(errOut, doc.ParseFailed)
	}
	if len(doc.Unreadable) > 0 {
		reportUnreadable(errOut, programName+" "+keyRefsCmd, doc.Unreadable)
	}
	if len(doc.ParseFailed) > 0 || len(doc.Unreadable) > 0 {
		return exitParseFailed
	}
	return exitOK
}
