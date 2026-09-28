package main

// served-values: what the exporter's /metrics serves per tenant, as JSON
// (#2115). The Python readers (`scripts/tools/_lib_tenant_values.py`) call
// this instead of re-deriving the exporter's merge, profile, schedule and
// disable rules.
//
// ⛔ NO SEMANTICS OF ITS OWN. Every value comes out of the exporter's own
// functions over the exporter's own load: config.LoadDir (the tree the
// collector serves), ThresholdConfig.ResolveAtWithKeys (the user_threshold
// rows, each paired by the resolver itself with the tenant-config key it
// serves) and the reserved-key resolvers the collector and the
// routing tooling read. Nothing here decides which key a row belongs to.
// TestServedValues_MatchesResolveAt is the guard that keeps it so.

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"log"
	"math"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// servedValuesCmd is the subcommand name: `da-guard served-values ...`.
const servedValuesCmd = "served-values"

// customAlertsKey is the reserved key whose rows are the tenant's custom
// alerts; its value is the list of those rows rather than one number.
const customAlertsKey = "_custom_alerts"

// servedValuesDoc is the JSON document on stdout.
type servedValuesDoc struct {
	ConfigDir string `json:"config_dir"`
	At        string `json:"at"`
	// ParseFailed is LoadDir's parseFailed: the files the exporter's load
	// skips because they do not decode. Always present ([] when none).
	ParseFailed []string                      `json:"parse_failed"`
	Tenants     map[string]servedTenantValues `json:"tenants"`
}

// servedTenantValues is one tenant's reading.
type servedTenantValues struct {
	// Values: every threshold key with a /metrics row (canonical spelling →
	// the row's value), plus the reserved keys as the exporter's resolvers
	// read them at `at`.
	Values map[string]any `json:"values"`
	// Severities: the severity label of each threshold key in Values.
	Severities map[string]string `json:"severities"`
	// Unserved: keys of the tenant's merged config with no entry in Values
	// (switched off, or served by nothing), keyed as the merged config spells
	// them, value as written.
	Unserved map[string]any `json:"unserved"`
}

type servedValuesFlags struct {
	configDir string
	at        string
}

func parseServedValuesFlags(args []string, errOut io.Writer) (*servedValuesFlags, error) {
	fs := flag.NewFlagSet(programName+" "+servedValuesCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &servedValuesFlags{}
	fs.StringVar(&f.configDir, "config-dir", "",
		"Path to the conf.d/ root. Required.")
	fs.StringVar(&f.at, "at", "",
		"Instant to resolve at, RFC3339 (e.g. 2026-07-01T03:00:00Z). Empty = now.")
	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir> [--at <RFC3339>]\n", programName, servedValuesCmd)
		fmt.Fprintf(errOut, "Print, as JSON, the values the exporter's /metrics serves per tenant.\n\n")
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  ok\n  2  caller error, a tree the exporter rejects (e.g. a tenant declared twice),\n"+
			"     or two keys producing one user_threshold series (/metrics would fail whole)\n"+
			"  3  config files the exporter cannot decode; the JSON is still written and names them in parse_failed\n")
	}
	if err := fs.Parse(args); err != nil {
		return nil, err
	}
	if fs.NArg() > 0 {
		fmt.Fprintf(errOut, "%s %s: unexpected argument %q\n", programName, servedValuesCmd, fs.Arg(0))
		return nil, errors.New("unexpected argument")
	}
	return f, nil
}

// runServedValues is the subcommand's entry point. LoadDir gets its own
// logger on errOut; the resolvers' log lines go wherever the process's `log`
// already goes (stderr in the binary).
func runServedValues(args []string, stdout, errOut io.Writer) int {
	f, err := parseServedValuesFlags(args, errOut)
	if errors.Is(err, flag.ErrHelp) {
		return exitOK
	}
	if err != nil {
		return exitCallerErr
	}
	if f.configDir == "" {
		fmt.Fprintf(errOut, "%s %s: --config-dir is required\n", programName, servedValuesCmd)
		return exitCallerErr
	}
	at := time.Now().UTC()
	if f.at != "" {
		at, err = time.Parse(time.RFC3339, f.at)
		if err != nil {
			fmt.Fprintf(errOut, "%s %s: --at must be RFC3339: %v\n", programName, servedValuesCmd, err)
			return exitCallerErr
		}
	}

	cfg, parseFailed, err := config.LoadDir(f.configDir, log.New(errOut, "", 0))
	if err != nil {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, servedValuesCmd, err)
		return exitCallerErr
	}
	tenants, err := servedValues(cfg, at)
	if err != nil {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, servedValuesCmd, err)
		return exitCallerErr
	}
	if parseFailed == nil {
		parseFailed = []string{}
	}
	b, err := json.MarshalIndent(servedValuesDoc{
		ConfigDir:   f.configDir,
		At:          at.Format(time.RFC3339),
		ParseFailed: parseFailed,
		Tenants:     tenants,
	}, "", "  ")
	if err != nil {
		fmt.Fprintf(errOut, "%s %s: encode JSON: %v\n", programName, servedValuesCmd, err)
		return exitCallerErr
	}
	if _, err := stdout.Write(append(b, '\n')); err != nil {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, servedValuesCmd, err)
		return exitCallerErr
	}
	if len(parseFailed) > 0 {
		reportParseFailed(errOut, parseFailed)
		return exitParseFailed
	}
	return exitOK
}

// servedValues reads every tenant of cfg at `at`.
func servedValues(cfg *config.ThresholdConfig, at time.Time) (map[string]servedTenantValues, error) {
	metadata := map[string]config.ResolvedMetadata{}
	for _, m := range cfg.ResolveMetadata() {
		metadata[m.Tenant] = m
	}
	dedup := map[string]string{}
	for _, d := range cfg.ResolveSeverityDedup() {
		dedup[d.Tenant] = d.Mode
	}
	routing := map[string]config.RoutingConfig{}
	for _, r := range cfg.ResolveRouting() {
		routing[r.Tenant] = r
	}
	ops := cfg.OperationalStatesAt(at)
	byTenant := ops.ByTenant(cfg)
	stateOn := map[string]map[string]bool{}
	for _, sf := range ops.StateFilters {
		if stateOn[sf.Tenant] == nil {
			stateOn[sf.Tenant] = map[string]bool{}
		}
		stateOn[sf.Tenant][sf.FilterName] = true
	}

	ownedBy, err := keyedRows(cfg, at)
	if err != nil {
		return nil, err
	}

	out := make(map[string]servedTenantValues, len(cfg.Tenants))
	for tenant, overrides := range cfg.Tenants {
		tv := servedTenantValues{
			Values:     map[string]any{},
			Severities: map[string]string{},
			Unserved:   map[string]any{},
		}

		for name, rows := range ownedBy[tenant] {
			if name == customAlertsKey {
				tv.Values[name] = customAlertRows(rows)
				continue
			}
			// A key owns one row, or a row and its #1231 legacy twin, which
			// carries the same value and severity under the old metric name.
			first := rows[0]
			for _, r := range rows[1:] {
				if !sameFloat(r.Value, first.Value) || r.Severity != first.Severity {
					return nil, fmt.Errorf("tenant %s: key %q owns rows with different values (%v %s, %v %s)",
						tenant, name, first.Value, first.Severity, r.Value, r.Severity)
				}
			}
			tv.Values[name] = jsonFloat(first.Value)
			tv.Severities[name] = first.Severity
		}

		// Reserved keys, as the exporter's own resolvers read them.
		reserved := map[string]any{}
		m := metadata[tenant]
		reserved["_metadata"] = map[string]any{
			"runbook_url": m.RunbookURL,
			"owner":       m.Owner,
			"tier":        m.Tier,
			"environment": m.Environment,
			"region":      m.Region,
			"domain":      m.Domain,
			"db_type":     m.DBType,
			"tags":        nonNilStrings(m.Tags),
			"groups":      nonNilStrings(m.Groups),
		}
		if mode, ok := dedup[tenant]; ok {
			reserved["_severity_dedup"] = mode
		} else {
			reserved["_severity_dedup"] = "disable"
		}
		reserved["_silent_mode"] = nonNilStrings(byTenant[tenant].SilentTargets)
		for name := range cfg.StateFilters {
			reserved["_state_"+name] = stateOn[tenant][name]
		}
		if rc, ok := routing[tenant]; ok {
			reserved["_routing"] = map[string]any{
				"receiver_type":   rc.ReceiverType,
				"receiver":        rc.ReceiverConfig,
				"group_by":        nonNilStrings(rc.GroupBy),
				"group_wait":      rc.GroupWait,
				"group_interval":  rc.GroupInterval,
				"repeat_interval": rc.RepeatInterval,
			}
		}
		for k, v := range reserved {
			if _, clash := tv.Values[k]; clash {
				return nil, fmt.Errorf("tenant %s: reserved key %q also owns /metrics threshold rows", tenant, k)
			}
			tv.Values[k] = v
		}

		for k, sv := range overrides {
			canon, _ := config.CanonicalKeyFor(k)
			if _, served := tv.Values[canon]; served {
				continue
			}
			tv.Unserved[k] = rawScheduledValue(sv)
		}
		out[tenant] = tv
	}
	return out, nil
}

// keyedRows resolves cfg once at `at` with ResolveAtWithKeys and groups the
// rows by tenant and by the tenant-config key the resolver paired each with.
//
// ⛔ A tree whose rows collide on one series is refused. The collector
// exports every row as user_threshold with its SeriesLabels; two rows with
// the same label set make Prometheus fail the whole scrape (HTTP 500), so
// /metrics then serves nothing at all — reporting both values as served
// would be false. The error names the two keys.
func keyedRows(cfg *config.ThresholdConfig, at time.Time) (map[string]map[string][]config.ResolvedThreshold, error) {
	keyed, _, err := cfg.ResolveAtWithKeys(at)
	if err != nil {
		return nil, err
	}
	if err := duplicateSeries(keyed); err != nil {
		return nil, err
	}
	out := map[string]map[string][]config.ResolvedThreshold{}
	for _, k := range keyed {
		if out[k.Tenant] == nil {
			out[k.Tenant] = map[string][]config.ResolvedThreshold{}
		}
		out[k.Tenant][k.Key] = append(out[k.Tenant][k.Key], k.ResolvedThreshold)
	}
	return out, nil
}

// duplicateSeries reports the first two rows that the collector would export
// as the same user_threshold series.
func duplicateSeries(keyed []config.KeyedThreshold) error {
	seen := make(map[string]string, len(keyed))
	for _, k := range keyed {
		names, values := k.SeriesLabels()
		pairs := make([]string, len(names))
		for i := range names {
			pairs[i] = names[i] + "=" + strconv.Quote(values[i])
		}
		sort.Strings(pairs)
		id := strings.Join(pairs, ",")
		if first, dup := seen[id]; dup {
			return fmt.Errorf("tenant %s: keys %q and %q both produce the series user_threshold{%s}; "+
				"the exporter's /metrics fails the whole scrape on this tree (HTTP 500), so nothing is served",
				k.Tenant, first, k.Key, id)
		}
		seen[id] = k.Key
	}
	return nil
}

// rowOrder is a stable sort key for rendering rows.
func rowOrder(r config.ResolvedThreshold) string {
	var labels []string
	for k, v := range r.CustomLabels {
		labels = append(labels, k+"="+v)
	}
	sort.Strings(labels)
	return strings.Join(append([]string{r.Metric, r.Severity, strconv.FormatFloat(r.Value, 'g', -1, 64)}, labels...), "\x00")
}

func sameFloat(a, b float64) bool {
	return a == b || (math.IsNaN(a) && math.IsNaN(b))
}

// jsonFloat is v, or its Prometheus text when JSON has no number for it.
func jsonFloat(v float64) any {
	switch {
	case math.IsNaN(v):
		return "NaN"
	case math.IsInf(v, 1):
		return "+Inf"
	case math.IsInf(v, -1):
		return "-Inf"
	}
	return v
}

// customAlertRows renders the custom-alert rows, sorted for a stable output.
func customAlertRows(rows []config.ResolvedThreshold) []map[string]any {
	sorted := append([]config.ResolvedThreshold(nil), rows...)
	sort.Slice(sorted, func(i, j int) bool { return rowOrder(sorted[i]) < rowOrder(sorted[j]) })
	out := make([]map[string]any, 0, len(sorted))
	for _, r := range sorted {
		labels := map[string]string{}
		for k, v := range r.CustomLabels {
			labels[k] = v
		}
		out = append(out, map[string]any{
			"metric":   r.Metric,
			"severity": r.Severity,
			"value":    jsonFloat(r.Value),
			"labels":   labels,
		})
	}
	return out
}

// rawScheduledValue is a merged-config value as written: the scalar text, or
// the schedule / time-box fields when it has them.
func rawScheduledValue(sv config.ScheduledValue) any {
	if len(sv.Overrides) == 0 && sv.Expiry == nil {
		return sv.Default
	}
	out := map[string]any{"default": sv.Default}
	if len(sv.Overrides) > 0 {
		ws := make([]map[string]string, 0, len(sv.Overrides))
		for _, o := range sv.Overrides {
			ws = append(ws, map[string]string{"window": o.Window, "value": o.Value})
		}
		out["overrides"] = ws
	}
	if sv.Expiry != nil {
		out["expires"] = sv.Expiry.Expires
		out["reason"] = sv.Expiry.Reason
	}
	return out
}

func nonNilStrings(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}
