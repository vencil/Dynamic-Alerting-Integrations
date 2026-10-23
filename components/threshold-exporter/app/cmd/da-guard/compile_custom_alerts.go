package main

// compile_custom_alerts.go — `da-guard compile-custom-alerts` (#3229 step 2):
// the custom-alert rule pack (ADR-024 Capability B) as JSON, for
// scripts/tools/dx/compile_custom_alerts.py to serialise and recipe-preview to
// evaluate. The entries are the exporter's (config.CustomAlertEntries, the
// same function served-values lists them with); the rule text is
// config.CompileCustomAlertPack's. Nothing here judges an entry.

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"reflect"
	"sort"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

const (
	compileCustomAlertsCmd    = "compile-custom-alerts"
	compileCustomAlertsSchema = "da-guard.compile-custom-alerts/v1"
)

// recipeStatus is config.RecipeStatus; a test swaps it to see a contract
// with a non-active recipe (every shipped recipe is active).
var recipeStatus = config.RecipeStatus

type notCompiledEntry struct {
	Tenant string `json:"tenant"`
	Index  int    `json:"index"`
	Name   string `json:"name"`
	// Status: rejected or duplicate (config.CustomAlert*).
	Status string `json:"status"`
	Reason string `json:"reason"`
}

// lifecycleUse: a recipe whose contract status is not active, and the
// tenants a compiled entry declares it for (ADR-024 #6): such recipes keep
// compiling (no silent alert loss); the caller says so.
type lifecycleUse struct {
	Recipe  string   `json:"recipe"`
	Status  string   `json:"status"`
	Tenants []string `json:"tenants"`
}

type compileCustomAlertsDoc struct {
	Schema string `json:"schema"`
	// Groups: the pack's `groups:` list, in pack order.
	Groups any `json:"groups"`
	Shapes int `json:"shapes"`
	// NotCompiled: entries the exporter does not serve (rejected,
	// duplicate) — no rule, and the caller names each.
	NotCompiled []notCompiledEntry `json:"not_compiled"`
	// BlockErrors: tenant → why its `_custom_alerts` does not decode (the
	// exporter serves none of it).
	BlockErrors map[string]string `json:"block_errors"`
	Lifecycle   []lifecycleUse    `json:"lifecycle"`
	// As served-values: files the load serves no tenant from, does not
	// decode, or cannot read — their tenants' custom alerts are not compiled.
	ParseFailed []string      `json:"parse_failed"`
	Skipped     []skippedFile `json:"skipped"`
	Unreadable  []skippedFile `json:"unreadable"`
}

func parseCompileCustomAlertsFlags(args []string, errOut io.Writer) (string, error) {
	fs := flag.NewFlagSet(programName+" "+compileCustomAlertsCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	var configDir string
	fs.StringVar(&configDir, "config-dir", "", "Path to the conf.d/ root. Required.")
	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir>\n", programName, compileCustomAlertsCmd)
		fmt.Fprintf(errOut, "Print, as JSON, the custom-alert rule pack (ADR-024) for the tree: one vectorised\n"+
			"rule set per shape (recipe_id) over every tenant's _custom_alerts entry the exporter serves\n"+
			"or has switched off, judged by the exporter (the entries served-values lists). Entries it\n"+
			"does not serve are named in not_compiled; a list that does not decode in block_errors.\n\n")
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  ok\n  2  caller error, a tree the exporter rejects (e.g. a tenant declared twice,\n"+
			"     no .yaml file at all), a tree whose /metrics cannot be gathered, a conf.d path or\n"+
			"     output string that is not valid UTF-8\n"+
			"  3  config files the exporter cannot decode or cannot read; the JSON is still written and\n"+
			"     names them in parse_failed / unreadable\n")
	}
	if err := fs.Parse(args); err != nil {
		return "", err
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(errOut, "%s %s: unexpected argument %q\n", programName, compileCustomAlertsCmd, fs.Arg(0))
		return "", errors.New("unexpected argument")
	}
	return configDir, nil
}

// runCompileCustomAlerts is the subcommand's entry point.
func runCompileCustomAlerts(args []string, stdout, errOut io.Writer) int {
	configDir, err := parseCompileCustomAlertsFlags(args, errOut)
	if errors.Is(err, flag.ErrHelp) {
		return exitOK
	}
	if err != nil {
		return exitCallerErr
	}
	if configDir == "" {
		fmt.Fprintf(errOut, "%s %s: --config-dir is required\n", programName, compileCustomAlertsCmd)
		return exitCallerErr
	}
	fail := func(err error) int {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, compileCustomAlertsCmd, err)
		return exitCallerErr
	}
	cfg, rep, written, allUnreadable, ok := loadJSONTree(configDir, compileCustomAlertsCmd, errOut)
	if !ok {
		return exitCallerErr
	}

	doc := compileCustomAlertsDoc{
		Schema:      compileCustomAlertsSchema,
		Groups:      []any{},
		NotCompiled: []notCompiledEntry{},
		BlockErrors: map[string]string{},
		Lifecycle:   []lifecycleUse{},
		ParseFailed: rep.ParseFailed,
		Skipped:     make([]skippedFile, 0, len(rep.NoTenant)),
		Unreadable:  unreadableEntries(rep.Unreadable),
	}
	if doc.ParseFailed == nil {
		doc.ParseFailed = []string{}
	}
	for _, name := range rep.NoTenant {
		doc.Skipped = append(doc.Skipped, skippedFile{File: name, Reason: config.NoTenantReason})
	}

	if !allUnreadable {
		// A tree /metrics cannot be gathered for serves no tenant anything —
		// the same verdict the guard's metrics_not_gatherable finding and
		// served-values give (over every schedule cut, so it does not depend
		// on when this runs). A pack compiled from it would describe rows the
		// exporter never serves.
		name := func(tenant, key string) string { return config.WrittenKey(written[tenant], key) }
		if v := gatherVerdict(cfg, time.Now().UTC(), name); v != "" {
			return fail(fmt.Errorf("/metrics cannot be gathered for this tree, so the exporter serves "+
				"none of its custom alerts: %s", v))
		}
		names := make([]string, 0, len(cfg.Tenants))
		for t := range cfg.Tenants {
			names = append(names, t)
		}
		sort.Strings(names)
		var tenants []config.TenantCustomAlerts
		lifecycle := map[[2]string]map[string]bool{}
		for _, t := range names {
			entries, blockErr := config.CustomAlertEntries(t, cfg.Tenants[t])
			if blockErr != "" {
				doc.BlockErrors[t] = blockErr
			}
			for _, e := range entries {
				switch e.Status {
				case config.CustomAlertServed, config.CustomAlertDisabled:
					if st := recipeStatus(e.Spec.Recipe); st != "" && st != "active" {
						k := [2]string{st, e.Spec.Recipe}
						if lifecycle[k] == nil {
							lifecycle[k] = map[string]bool{}
						}
						lifecycle[k][t] = true
					}
				default:
					doc.NotCompiled = append(doc.NotCompiled, notCompiledEntry{
						Tenant: t, Index: e.Index, Name: e.Spec.Name, Status: e.Status, Reason: e.Reason})
				}
			}
			tenants = append(tenants, config.TenantCustomAlerts{Tenant: t, Entries: entries})
		}
		pack, err := config.CompileCustomAlertPack(tenants)
		if err != nil {
			return fail(err)
		}
		doc.Groups, doc.Shapes = pack.Groups, pack.Shapes
		if doc.Groups == nil || reflect.ValueOf(pack.Groups).Len() == 0 {
			doc.Groups = []any{}
		}
		keys := make([][2]string, 0, len(lifecycle))
		for k := range lifecycle {
			keys = append(keys, k)
		}
		sort.Slice(keys, func(i, j int) bool {
			if keys[i][0] != keys[j][0] {
				return keys[i][0] < keys[j][0]
			}
			return keys[i][1] < keys[j][1]
		})
		for _, k := range keys {
			ts := make([]string, 0, len(lifecycle[k]))
			for t := range lifecycle[k] {
				ts = append(ts, t)
			}
			sort.Strings(ts)
			doc.Lifecycle = append(doc.Lifecycle, lifecycleUse{Recipe: k[1], Status: k[0], Tenants: ts})
		}
	}

	if err := checkOutputUTF8("", reflect.ValueOf(doc)); err != nil {
		return fail(err)
	}
	// Not json.MarshalIndent: it HTML-escapes `<`, `>`, `&` in every expr,
	// which a reader decodes the same but a human reading the JSON cannot.
	var buf bytes.Buffer
	enc := json.NewEncoder(&buf)
	enc.SetEscapeHTML(false)
	enc.SetIndent("", "  ")
	if err := enc.Encode(doc); err != nil {
		return fail(fmt.Errorf("encode JSON: %w", err))
	}
	if _, err := stdout.Write(buf.Bytes()); err != nil {
		return fail(err)
	}
	if len(doc.ParseFailed) > 0 {
		reportParseFailed(errOut, doc.ParseFailed)
	}
	if len(doc.Unreadable) > 0 {
		reportUnreadable(errOut, programName+" "+compileCustomAlertsCmd, doc.Unreadable)
	}
	if len(doc.ParseFailed) > 0 || len(doc.Unreadable) > 0 {
		return exitParseFailed
	}
	return exitOK
}
