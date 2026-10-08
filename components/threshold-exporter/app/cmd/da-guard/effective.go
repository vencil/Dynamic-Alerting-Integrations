package main

// effective: what tenant-api's /effective answers per tenant, as JSON, for
// every tenant of a tree (#2564) — the walker-plane half beside
// served-values (the /metrics half). The Python readers
// (`scripts/tools/_lib_tenant_values.py`, load_effective) call this instead
// of re-deriving the merge, the platform overlay and profile binding with
// their own walker.
//
// ⛔ NO SEMANTICS OF ITS OWN. Every tenant entry is config.EffectiveTree's —
// the resolver ResolveEffective (tenant-api /effective) runs, over one walk
// of the tree — serialized field for field as the /effective body, plus two
// fields the body leaves out: the profile the tenant is bound to
// (EffectiveConfig.BoundProfile) and per-key attribution
// (EffectiveConfig.KeySources). TestEffective_MatchesResolveEffective keeps
// every entry equal to ResolveEffective's own answer.

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"reflect"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// effectiveCmd is the subcommand name: `da-guard effective ...`.
const effectiveCmd = "effective"

// effectiveSchema names this document's shape. A reader refuses any other
// value, so a change to the shape a reader relies on is a new value here.
const effectiveSchema = "da-guard.effective/v1"

// effectiveDoc is the JSON document on stdout. The --config-dir argument is
// not echoed back (see servedValuesDoc).
type effectiveDoc struct {
	Schema string `json:"schema"`
	// ParseFailed: the files the exporter's load of the tree skips because
	// they do not decode (ScopedTenants.ParseFailed over the whole tree).
	// Always present ([] when none).
	ParseFailed []string `json:"parse_failed"`
	// Unreadable: the config-named files the walk could not stat or read and
	// the directories below the root it could not list
	// (ScopedTenants.Unreadable over the whole tree), each {file, reason} as
	// served-values names them (#2588). A tenant in such a file is absent
	// from Tenants, and a value an unreadable `_defaults.yaml` would have
	// given is absent from every effective_config — so the exit code is 3,
	// as for ParseFailed. Added without a schema bump: a reader that does not
	// know the field still fails on the exit code. A symlink to a directory
	// is not listed. Always present ([] when none).
	Unreadable []skippedFile `json:"unreadable"`
	// Skipped: the files the walk read but takes no tenant from
	// (ScopedTenants.NoTenant), each with the load's reason, so a reader
	// that needs only this document can name them (#2115 R3). On a run that
	// exits 0 it matches served-values' `skipped` (tested); when a file does
	// not decode (exit 3) it can be empty where served-values still lists
	// files, so it is not a complete list there. Not a failure: the exit
	// code is unchanged. Added without a schema bump, as Unreadable was.
	// Always present ([] when none).
	Skipped []skippedFile              `json:"skipped"`
	Tenants map[string]effectiveTenant `json:"tenants"`
}

// effectiveTenant is one tenant: the /effective body (the embedded
// EffectiveConfig serializes exactly as tenant-api writes it) plus the two
// fields that body leaves out.
type effectiveTenant struct {
	config.EffectiveConfig
	// Profile is the profile the tenant is bound to; null for none.
	Profile *string `json:"profile"`
	// KeySources: per top-level key of effective_config, its layer and file.
	KeySources map[string]config.KeySource `json:"key_sources"`
	// NotServed: each key of effective_config whose value /metrics does not
	// serve as shown, with the exporter's own reason and the file of the
	// value shown (EffectiveConfig.NotServed, #2296). Always present ({}
	// when none); the /effective body omits it when empty.
	NotServed map[string]config.NotServedKey `json:"not_served"`
	// ChainParseFailed: the tenant's chain files the exporter does not read
	// because they do not parse, read as empty here (#2296). Always present
	// ([] when none). Such a file is in parse_failed too (exit 3).
	ChainParseFailed []string `json:"chain_parse_failed"`
}

func parseEffectiveFlags(args []string, errOut io.Writer) (string, error) {
	fs := flag.NewFlagSet(programName+" "+effectiveCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	var configDir string
	fs.StringVar(&configDir, "config-dir", "", "Path to the conf.d/ root. Required.")
	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir>\n", programName, effectiveCmd)
		fmt.Fprintf(errOut, "Print, as JSON, every tenant's effective config as tenant-api's /effective resolves it,\n"+
			"with the profile each tenant is bound to and, per key, the layer and file its value came from.\n\n")
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  ok\n  2  caller error, or a tree the resolver rejects (e.g. a tenant declared twice,\n"+
			"     no .yaml file at all), or any output string that is not valid UTF-8\n"+
			"  3  config files the exporter cannot decode or cannot read; the JSON is still written and\n"+
			"     names them in parse_failed / unreadable\n")
	}
	if err := fs.Parse(args); err != nil {
		return "", err
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(errOut, "%s %s: unexpected argument %q\n", programName, effectiveCmd, fs.Arg(0))
		return "", errors.New("unexpected argument")
	}
	return configDir, nil
}

// runEffective is the subcommand's entry point.
func runEffective(args []string, stdout, errOut io.Writer) int {
	configDir, err := parseEffectiveFlags(args, errOut)
	if errors.Is(err, flag.ErrHelp) {
		return exitOK
	}
	if err != nil {
		return exitCallerErr
	}
	if configDir == "" {
		fmt.Fprintf(errOut, "%s %s: --config-dir is required\n", programName, effectiveCmd)
		return exitCallerErr
	}

	doc := effectiveDoc{Schema: effectiveSchema, ParseFailed: []string{}, Unreadable: []skippedFile{},
		Skipped: []skippedFile{}, Tenants: map[string]effectiveTenant{}}
	tree, err := config.EffectiveTree(configDir)
	if tree != nil && tree.RootListErr != nil {
		// The reason; the root itself is named in unreadable (exit 3, #2627).
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, effectiveCmd, tree.RootListErr)
	}
	if tree != nil {
		// Set on success and on a DecodeError stop alike (#2588): a run
		// stopped by one undecodable file still names the unreadable ones.
		doc.Unreadable = unreadableEntries(tree.Unreadable)
	}
	if err != nil {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, effectiveCmd, err)
		// A file the resolve's decode rejects (a chain `_defaults.yaml`, or a
		// tenant file the walker kept but the merge rejects) is the author's
		// to fix: exit 3 naming it, as the guard does (#2123). No tenant was
		// resolved, so the document carries none.
		var de *config.DecodeError
		if !errors.As(err, &de) {
			return exitCallerErr
		}
		doc.ParseFailed = []string{de.Path}
	} else {
		if tree.ParseFailed != nil {
			doc.ParseFailed = tree.ParseFailed
		}
		for _, name := range tree.NoTenant {
			doc.Skipped = append(doc.Skipped, skippedFile{File: name, Reason: config.NoTenantReason})
		}
		for _, ec := range tree.Tenants {
			t := effectiveTenant{EffectiveConfig: *ec, KeySources: ec.KeySources,
				NotServed: ec.NotServed, ChainParseFailed: ec.ChainParseFailed}
			// A YAML `.inf` / `.nan` is sent as text, as tenant-api sends it.
			t.EffectiveConfig.EffectiveConfig = config.NonFiniteAsText(ec.EffectiveConfig)
			if ec.BoundProfile != "" {
				p := ec.BoundProfile
				t.Profile = &p
			}
			if t.KeySources == nil {
				t.KeySources = map[string]config.KeySource{}
			}
			if t.NotServed == nil {
				t.NotServed = map[string]config.NotServedKey{}
			}
			if t.ChainParseFailed == nil {
				t.ChainParseFailed = []string{}
			}
			doc.Tenants[ec.TenantID] = t
		}
	}

	if err := checkOutputUTF8("", reflect.ValueOf(doc)); err != nil {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, effectiveCmd, err)
		return exitCallerErr
	}
	b, err := json.MarshalIndent(doc, "", "  ")
	if err != nil {
		fmt.Fprintf(errOut, "%s %s: encode JSON: %v\n", programName, effectiveCmd, err)
		return exitCallerErr
	}
	if _, err := stdout.Write(append(b, '\n')); err != nil {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, effectiveCmd, err)
		return exitCallerErr
	}
	if len(doc.ParseFailed) > 0 {
		reportParseFailed(errOut, doc.ParseFailed)
	}
	if len(doc.Unreadable) > 0 {
		reportUnreadable(errOut, programName+" "+effectiveCmd, doc.Unreadable)
	}
	if len(doc.ParseFailed) > 0 || len(doc.Unreadable) > 0 {
		return exitParseFailed
	}
	return exitOK
}
