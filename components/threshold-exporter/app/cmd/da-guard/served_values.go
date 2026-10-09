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
// serves), the exporter's own /metrics registry gathered privately (which of
// those rows /metrics really carries, and whether it serves at all —
// internal/scrape) and the reserved-key resolvers the collector and the
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
	"reflect"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/prometheus/client_golang/prometheus"
	dto "github.com/prometheus/client_model/go"

	"github.com/vencil/threshold-exporter/internal/scrape"
	"github.com/vencil/threshold-exporter/pkg/config"
)

// servedValuesCmd is the subcommand name: `da-guard served-values ...`.
const servedValuesCmd = "served-values"

// customAlertsKey is the reserved key whose rows are the tenant's custom
// alerts; its value is the list of those rows rather than one number.
const customAlertsKey = "_custom_alerts"

// servedValuesDoc is the JSON document on stdout.
//
// The --config-dir argument is not echoed back: it is the caller's own
// input, no reader uses it, and a path that is not valid UTF-8 is no reason
// to refuse the tree.
type servedValuesDoc struct {
	At string `json:"at"`
	// ParseFailed is LoadDir's parseFailed: the files the exporter's load
	// skips because they do not decode. Always present ([] when none).
	ParseFailed []string `json:"parse_failed"`
	// Skipped: the files the exporter's load read but serves no tenant from
	// (config.LoadReport.NoTenant), each with the load's reason, so a reader
	// can name them without judging a file's format itself (#2115 R3).
	// Always present ([] when none).
	Skipped []skippedFile `json:"skipped"`
	// Unreadable: the config-named files the exporter's load could not stat
	// or read, and the directories below the root it could not list
	// (config.LoadReport.Unreadable), each with a closed-set reason —
	// config.UnreadableStatError, UnreadableReadError or UnreadableWalkError,
	// never the OS error text. The exporter WARNs and serves the tree without them, so
	// the tenants they hold are absent from Tenants; the exit code is 3, as
	// for ParseFailed, so a caller that does not know this field fails too
	// (#2115). A symlink to a directory is not listed. Always present ([]
	// when none). When every config file of the tree is unreadable, the
	// document still comes out, with no tenant and those files here (#2627).
	Unreadable []skippedFile                 `json:"unreadable"`
	Tenants    map[string]servedTenantValues `json:"tenants"`
	// Aliases: the exporter's alias table (config.DeprecatedKeyAliases),
	// retired base key → canonical base key, so a reader holding a key in
	// its retired spelling finds it in Values, Severities, Schedules and
	// Dropped, which are keyed by the canonical spelling. The exporter
	// canonicalizes the `<base>_critical` and `<base>{…}` spellings of a
	// retired base the same way (config.CanonicalKeyFor). Always present
	// ({} when the table is empty).
	Aliases map[string]string `json:"aliases"`
	// UnreadKeys: keys a config file writes that the exporter's load does
	// not read (config.LoadReport.RootDefaultsUnread, #2296), each
	// {file, key, reason}. Today one reason: root_defaults_unwrapped — the
	// root `_defaults.yaml` has no `defaults:` mapping, so its top-level
	// keys are not platform defaults to /metrics, while /effective shows
	// them (and names them in its not_served). Not a failure: the exit code
	// is unchanged. Always present ([] when none).
	UnreadKeys []unreadKey `json:"unread_keys"`
}

// unreadKey is one entry of servedValuesDoc.UnreadKeys.
type unreadKey struct {
	File   string `json:"file"`
	Key    string `json:"key"`
	Reason string `json:"reason"`
}

// skippedFile is one entry of servedValuesDoc.Skipped or .Unreadable.
type skippedFile struct {
	File   string `json:"file"`
	Reason string `json:"reason"`
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
	// them, value as written — plus the threshold keys the tenant inherits
	// from a subtree `_defaults.yaml` that the exporter's build cannot
	// deliver (config.LoadReport.Undeliverable, #1976; reserved keys, keys
	// the exporter never serves as a row and switched-off keys are not
	// included): the build leaves them out of the tenant's map, so the merged
	// config does not carry them, but the tenant's effective config shows
	// them. Keyed as that defaults file spells them; the value is NOT as
	// written but the build's normalised rendering of the deepest level that
	// writes the key in a threshold shape (a YAML `1e6` is "1e+06").
	Unserved map[string]any `json:"unserved"`
	// Dropped: keys the resolver produced a row for but whose series the
	// exporter cannot build (client_golang rejects the label set), so
	// /metrics drops the row. Canonical key → the rejection of each dropped
	// row. Such a key is in Values only if another of its rows was kept.
	Dropped map[string][]string `json:"dropped"`
	// Schedules (#2115 (c)), only with --schedules (nil, and absent from
	// the JSON, without it — the reading costs one resolve and Gather per
	// piece of the day): the whole UTC day of every threshold key that
	// /metrics serves at some minute of it, or whose time-boxed override the
	// exporter honours (an `expires:` on a base key) — a superset of the
	// threshold keys of Values. Canonical key → its segments, read from the
	// exporter's own resolver, not from the schedule as written: see
	// addSchedules. `_custom_alerts` is not included.
	Schedules *map[string]servedSchedule `json:"schedules,omitempty"`
}

// servedSchedule is one key's day. Segments cover 00:00–24:00 UTC in order,
// with no gap, adjacent segments with the same value and severity merged.
// Expires / Expired are there only when the exporter honours an `expires:`
// on the key (config.ResolveThresholdExpiriesAt — base keys only): Expires
// as written, Expired the exporter's verdict at `at`. The segments already
// follow that verdict (an expired override serves the platform default);
// they do not follow `expires:` passing later.
type servedSchedule struct {
	Segments []scheduleSegment `json:"segments"`
	Expires  string            `json:"expires,omitempty"`
	Expired  *bool             `json:"expired,omitempty"`
}

// scheduleSegment is [From, To) of the UTC day ("HH:MM"; To of the last is
// "24:00"). Value is the served number (as in Values), or null (jsonNull):
// the key has no /metrics row in that segment (switched off, dropped, or
// nothing to serve). Severity is the row's severity label; absent when Value
// is null. Error, set only on a segment in which the exporter's /metrics
// cannot be gathered at all (HTTP 500 — nothing is served, for any key), is
// that Gather's failure; Value and Severity are then absent.
type scheduleSegment struct {
	From     string `json:"from"`
	To       string `json:"to"`
	Value    any    `json:"value,omitempty"`
	Severity string `json:"severity,omitempty"`
	Error    string `json:"error,omitempty"`
}

// jsonNull is a segment's Value when the key is not served: an explicit JSON
// null, where a nil Value (an Error segment) leaves the field out.
var jsonNull = json.RawMessage("null")

type servedValuesFlags struct {
	configDir string
	at        string
	schedules bool
}

func parseServedValuesFlags(args []string, errOut io.Writer) (*servedValuesFlags, error) {
	fs := flag.NewFlagSet(programName+" "+servedValuesCmd, flag.ContinueOnError)
	fs.SetOutput(errOut)
	f := &servedValuesFlags{}
	fs.StringVar(&f.configDir, "config-dir", "",
		"Path to the conf.d/ root. Required.")
	fs.StringVar(&f.at, "at", "",
		"Instant to resolve at, RFC3339 (e.g. 2026-07-01T03:00:00Z). Empty = now.")
	fs.BoolVar(&f.schedules, "schedules", false,
		"Also print each tenant's schedules: every threshold key over the whole UTC day.\n"+
			"Resolves the tree once per part of the day in which some schedule changes.")
	fs.Usage = func() {
		fmt.Fprintf(errOut, "Usage: %s %s --config-dir <dir> [--at <RFC3339>] [--schedules]\n", programName, servedValuesCmd)
		fmt.Fprintf(errOut, "Print, as JSON, the values the exporter's /metrics serves per tenant.\n\n")
		fs.PrintDefaults()
		fmt.Fprintf(errOut, "\nExit codes:\n  0  ok\n  2  caller error, a tree the exporter rejects (e.g. a tenant declared twice),\n"+
			"     any output string that is not valid UTF-8, or a Gather failure of the same\n"+
			"     collectors production /metrics serves (e.g. two keys producing one series)\n"+
			"  3  config files the exporter cannot decode or cannot read; the JSON is still written and\n"+
			"     names them in parse_failed / unreadable\n\n"+
			"Served means served to a UTF-8-negotiated scrape (the Prometheus 3 default); a scrape with\n"+
			"legacy or underscores escaping may see labels such as {a-b} and {a.b} collide.\n")
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

	cfg, rep, err := config.LoadDirReport(f.configDir, log.New(errOut, "", 0))
	// A tree whose every config file is unreadable is not "no .yaml files"
	// (#2627): the exporter serves nothing from it, so the document carries
	// no tenant and names the files in unreadable (exit 3), as effective and
	// the guard do on the same tree — a --config-dir the walk cannot list
	// too, named as "." (walk_error). Only a tree with no config file at all
	// keeps the refusal (exit 2).
	allUnreadable := errors.Is(err, config.ErrNoYAMLFiles) && len(rep.Unreadable) > 0
	if err != nil && !allUnreadable {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, servedValuesCmd, err)
		return exitCallerErr
	}
	if rep.RootListErr != nil {
		// The reason; the root itself is named in unreadable as "." (#2627).
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, servedValuesCmd, rep.RootListErr)
	}
	tenants := map[string]servedTenantValues{}
	if !allUnreadable {
		if err := checkUTF8(cfg); err != nil {
			fmt.Fprintf(errOut, "%s %s: %v\n", programName, servedValuesCmd, err)
			return exitCallerErr
		}
		tenants, err = servedValues(cfg, at, rep.Undeliverable, f.schedules)
		if err != nil {
			fmt.Fprintf(errOut, "%s %s: %v\n", programName, servedValuesCmd, err)
			return exitCallerErr
		}
		spellAsWritten(tenants, rep.WrittenKeys)
	}
	parseFailed := rep.ParseFailed
	if parseFailed == nil {
		parseFailed = []string{}
	}
	skipped := make([]skippedFile, 0, len(rep.NoTenant))
	for _, name := range rep.NoTenant {
		skipped = append(skipped, skippedFile{File: name, Reason: config.NoTenantReason})
	}
	unreadable := unreadableEntries(rep.Unreadable)
	unreadKeys := make([]unreadKey, 0, len(rep.RootDefaultsUnread))
	for _, u := range rep.RootDefaultsUnread {
		unreadKeys = append(unreadKeys, unreadKey{File: u.File, Key: u.Key, Reason: config.NotServedRootDefaultsUnwrapped})
	}
	doc := servedValuesDoc{
		At:          at.Format(time.RFC3339),
		ParseFailed: parseFailed,
		Skipped:     skipped,
		Unreadable:  unreadable,
		Tenants:     tenants,
		Aliases:     config.DeprecatedKeyAliases(),
		UnreadKeys:  unreadKeys,
	}
	if err := checkOutputUTF8("", reflect.ValueOf(doc)); err != nil {
		fmt.Fprintf(errOut, "%s %s: %v\n", programName, servedValuesCmd, err)
		return exitCallerErr
	}
	b, err := json.MarshalIndent(doc, "", "  ")
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
	}
	if len(unreadable) > 0 {
		reportUnreadable(errOut, programName+" "+servedValuesCmd, unreadable)
	}
	if len(parseFailed) > 0 || len(unreadable) > 0 {
		return exitParseFailed
	}
	return exitOK
}

// checkUTF8 refuses, before anything is resolved, a tree whose tenant ids or
// keys are not valid UTF-8, naming where in the config the string sits. JSON
// cannot carry such a string: encoding/json writes U+FFFD for the bad bytes,
// so two distinct keys can come out as one and a reader silently loses one of
// them. It must run before the Gather: the exporter's collector panics on
// such a tenant id (#2266). checkOutputUTF8 is the net over everything else
// the output carries.
func checkUTF8(cfg *config.ThresholdConfig) error {
	for tenant, overrides := range cfg.Tenants {
		if !utf8.ValidString(tenant) {
			return fmt.Errorf("tenant %q: the tenant id is not valid UTF-8; JSON cannot carry it", tenant)
		}
		for k := range overrides {
			if !utf8.ValidString(k) {
				return fmt.Errorf("tenant %q: key %q is not valid UTF-8; JSON cannot carry it", tenant, k)
			}
		}
	}
	for k := range cfg.Defaults {
		if !utf8.ValidString(k) {
			return fmt.Errorf("defaults: key %q is not valid UTF-8; JSON cannot carry it", k)
		}
	}
	for name := range cfg.StateFilters {
		if !utf8.ValidString(name) {
			return fmt.Errorf("state_filters: name %q is not valid UTF-8; JSON cannot carry it", name)
		}
	}
	return nil
}

// checkOutputUTF8 walks every string the output document carries — struct
// fields, map keys and values, list items, whatever their nesting — and
// refuses the first that is not valid UTF-8, naming its path (keys and the
// string itself shown with %q). It follows the value structure, so a plain
// field added later is walked without being listed. ⚠️ Not walked: what JSON
// encodes through a custom MarshalJSON, a TextMarshaler used as a map key,
// unexported embedded structs, json.RawMessage, and fields tagged `json:"-"`
// (a skipped field is walked anyway). None of today's output types use them.
func checkOutputUTF8(path string, v reflect.Value) error {
	switch v.Kind() {
	case reflect.Interface, reflect.Pointer:
		if v.IsNil() {
			return nil
		}
		return checkOutputUTF8(path, v.Elem())
	case reflect.String:
		if !utf8.ValidString(v.String()) {
			return fmt.Errorf("%s: %q is not valid UTF-8; JSON cannot carry it", path, v.String())
		}
	case reflect.Struct:
		t := v.Type()
		for i := 0; i < v.NumField(); i++ {
			if !t.Field(i).IsExported() {
				continue
			}
			name := t.Field(i).Name
			if tag := strings.Split(t.Field(i).Tag.Get("json"), ",")[0]; tag != "" {
				name = tag
			}
			if err := checkOutputUTF8(joinPath(path, name), v.Field(i)); err != nil {
				return err
			}
		}
	case reflect.Map:
		keys := v.MapKeys()
		sort.Slice(keys, func(i, j int) bool { return fmt.Sprint(keys[i]) < fmt.Sprint(keys[j]) })
		for _, k := range keys {
			kp := fmt.Sprintf("%s[%q]", path, fmt.Sprint(k.Interface()))
			if err := checkOutputUTF8(kp+" (key)", k); err != nil {
				return err
			}
			if err := checkOutputUTF8(kp, v.MapIndex(k)); err != nil {
				return err
			}
		}
	case reflect.Slice, reflect.Array:
		for i := 0; i < v.Len(); i++ {
			if err := checkOutputUTF8(fmt.Sprintf("%s[%d]", path, i), v.Index(i)); err != nil {
				return err
			}
		}
	}
	return nil
}

func joinPath(path, name string) string {
	if path == "" {
		return name
	}
	return path + "." + name
}

// servedValues reads every tenant of cfg at `at`. undeliverable is the same
// load's LoadReport.Undeliverable; its keys go to Unserved. withSchedules
// also fills Schedules (--schedules).
func servedValues(cfg *config.ThresholdConfig, at time.Time,
	undeliverable map[string]map[string]config.ScheduledValue, withSchedules bool,
) (map[string]servedTenantValues, error) {
	ownedBy, droppedBy, res, err := keyedRows(cfg, at)
	if err != nil {
		return nil, err
	}

	// The reserved keys are read from what the collector's scrape resolved
	// (Hooks.Observe), not by calling those resolvers a second time: a second
	// call would print each of their WARN lines twice (#2374).
	metadata := map[string]config.ResolvedMetadata{}
	for _, m := range res.Metadata {
		metadata[m.Tenant] = m
	}
	dedup := map[string]string{}
	for _, d := range res.SeverityDedup {
		dedup[d.Tenant] = d.Mode
	}
	// The collector does not read _routing, so its resolver runs only here.
	routing := map[string]config.RoutingConfig{}
	for _, r := range cfg.ResolveRouting() {
		routing[r.Tenant] = r
	}
	byTenant := res.Ops.ByTenant(cfg)
	stateOn := map[string]map[string]bool{}
	for _, sf := range res.Ops.StateFilters {
		if stateOn[sf.Tenant] == nil {
			stateOn[sf.Tenant] = map[string]bool{}
		}
		stateOn[sf.Tenant][sf.FilterName] = true
	}

	out := make(map[string]servedTenantValues, len(cfg.Tenants))
	for tenant, overrides := range cfg.Tenants {
		tv := servedTenantValues{
			Values:     map[string]any{},
			Severities: map[string]string{},
			Unserved:   map[string]any{},
			Dropped:    map[string][]string{},
		}
		for name, errs := range droppedBy[tenant] {
			tv.Dropped[name] = errs
		}

		for name, rows := range ownedBy[tenant] {
			if name == customAlertsKey {
				tv.Values[name] = customAlertRows(rows)
				continue
			}
			r, err := keyReading(tenant, name, rows)
			if err != nil {
				return nil, err
			}
			tv.Values[name] = jsonFloat(r.value)
			tv.Severities[name] = r.severity
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
			canon, isAlias := config.CanonicalKeyFor(k)
			// A retired spelling whose canonical key the same tenant also
			// writes is shadowed: the canonical key's value is the one
			// served, so the alias's own value is not.
			_, canonWritten := overrides[canon]
			shadowed := isAlias && canonWritten
			if _, served := tv.Values[canon]; served && !shadowed {
				continue
			}
			tv.Unserved[k] = rawScheduledValue(sv)
		}
		// #1976: the build's own verdict (LoadReport.Undeliverable:
		// FlatBuild.Unreachable minus what undeliverableThresholds leaves
		// out), never re-judged here. Invariant, so no membership check: such
		// a key is in neither Unserved nor Values. Not in Unserved — Unserved
		// above comes from the tenant's merged map, and the build judges only
		// keys the tenant's map lacks under every spelling
		// (tenantAuthoredThreshold; a platform `tenants:` entry or a profile
		// is already in that map) and writes no refused key into it. Not in
		// Values — the non-row entries of Values are reserved keys
		// (`_metadata`, `_state_<filter>`, `_silent_mode`, …), all of which
		// IsReservedKey accepts and the filter removes; and a key reaches
		// Values as a threshold only by owning a /metrics row, which needs the
		// key, its canonical or its legacy spelling declared at the root or in
		// `optional_overrides:` — exactly what made the build refuse it.
		for k, sv := range undeliverable[tenant] {
			tv.Unserved[k] = rawScheduledValue(sv)
		}
		out[tenant] = tv
	}
	if withSchedules {
		if err := addSchedules(cfg, at, out, res.ThresholdExpiries); err != nil {
			return nil, err
		}
	}
	return out, nil
}

// spellAsWritten re-keys each tenant's maps with its keys as written
// (LoadReport.WrittenKeys, #2031): the exporter's config keys a dimensional
// key by its canonical spelling, and `da-guard effective` shows it as the
// layer that supplied it wrote it — a reader joins the two documents by key.
// The base keeps this document's canonical #1231 spelling (config.WrittenKey).
func spellAsWritten(tenants map[string]servedTenantValues, written map[string]map[string]string) {
	for tenant, tv := range tenants {
		w := written[tenant]
		if len(w) == 0 {
			continue
		}
		name := func(k string) string { return config.WrittenKey(w, k) }
		tv.Values = renamed(tv.Values, name)
		tv.Severities = renamed(tv.Severities, name)
		tv.Unserved = renamed(tv.Unserved, name)
		tv.Dropped = renamed(tv.Dropped, name)
		if tv.Schedules != nil {
			s := renamed(*tv.Schedules, name)
			tv.Schedules = &s
		}
		tenants[tenant] = tv
	}
}

func renamed[V any](m map[string]V, name func(string) string) map[string]V {
	out := make(map[string]V, len(m))
	for k, v := range m {
		out[name(k)] = v
	}
	return out
}

// keyedRows gathers, at `at`, the registry the exporter's /metrics serves —
// the same collector and config metrics, wired by the same scrape.Register —
// and groups the user_threshold rows it keeps by tenant and key. Rows the
// collector drops are returned apart, with client_golang's reason.
//
// ⛔ /metrics decides, not this file. A Gather error in any family means the
// exporter's scrape fails as a whole (HTTP 500) and nothing is served, so it
// is an error here too, with client_golang's text. The user_threshold rows
// are resolved with ResolveAtWithKeys (the collector's Resolve hook), so each
// row the collector reports on carries its key.
//
// It also returns the reserved-key readings that scrape resolved
// (Hooks.Observe), for servedValues to read instead of resolving them again.
//
// Not registered: the Go runtime collector the exporter adds — it reads no
// config, so no tree can make it fail. The config metrics are a fresh set,
// registered so a name the collector emits that collides with one of them
// fails here as it would on /metrics; the collector writes nothing to them.
func keyedRows(cfg *config.ThresholdConfig, at time.Time) (
	served map[string]map[string][]config.ResolvedThreshold, dropped map[string]map[string][]string,
	reserved scrape.Reserved, err error,
) {
	var keyed []config.KeyedThreshold
	observed := 0
	var keyErr error
	var results []emitResult
	metrics := scrape.NewConfigMetrics()
	collector := scrape.NewCollectorWithHooks(staticSource{cfg}, scrape.Hooks{
		Now: func() time.Time { return at },
		Resolve: func(c *config.ThresholdConfig, now time.Time) ([]config.ResolvedThreshold, config.ResolveStats) {
			var stats config.ResolveStats
			keyed, stats, keyErr = c.ResolveAtWithKeys(now)
			rows := make([]config.ResolvedThreshold, len(keyed))
			for i, k := range keyed {
				rows[i] = k.ResolvedThreshold
			}
			results = make([]emitResult, len(rows))
			return rows, stats
		},
		Report: func(i int, m prometheus.Metric, err error) {
			results[i] = emitResult{reported: true, metric: m, err: err}
		},
		Observe: func(r scrape.Reserved) {
			reserved = r
			observed++
		},
	})
	reg := prometheus.NewRegistry()
	scrape.Register(reg, collector, metrics)
	_, gerr := reg.Gather()
	if keyErr != nil {
		return nil, nil, scrape.Reserved{}, keyErr
	}
	if gerr != nil {
		return nil, nil, scrape.Reserved{}, fmt.Errorf("the exporter's /metrics cannot be gathered for this tree, so its scrape fails "+
			"as a whole (HTTP 500) and nothing is served%s: %v", sameSeriesKeys(keyed, results), gerr)
	}
	if observed != 1 {
		return nil, nil, scrape.Reserved{}, fmt.Errorf("internal: the collector reported its reserved-key readings %d times, want 1", observed)
	}
	served, dropped, err = groupRows(keyed, results)
	if err != nil {
		return nil, nil, scrape.Reserved{}, err
	}
	return served, dropped, reserved, nil
}

// groupRows groups the rows of one Gather by tenant and key: those the
// collector kept, and apart, the rejection of each it dropped.
func groupRows(keyed []config.KeyedThreshold, results []emitResult) (
	served map[string]map[string][]config.ResolvedThreshold, dropped map[string]map[string][]string, err error,
) {
	if len(results) != len(keyed) {
		return nil, nil, fmt.Errorf("internal: the collector resolved %d rows, %d have a verdict", len(keyed), len(results))
	}
	served = map[string]map[string][]config.ResolvedThreshold{}
	dropped = map[string]map[string][]string{}
	for i, k := range keyed {
		switch r := results[i]; {
		case !r.reported:
			return nil, nil, fmt.Errorf("internal: the collector gave no verdict on row %d (key %q)", i, k.Key)
		case r.err != nil:
			if dropped[k.Tenant] == nil {
				dropped[k.Tenant] = map[string][]string{}
			}
			dropped[k.Tenant][k.Key] = append(dropped[k.Tenant][k.Key], r.err.Error())
		default:
			if served[k.Tenant] == nil {
				served[k.Tenant] = map[string][]config.ResolvedThreshold{}
			}
			served[k.Tenant][k.Key] = append(served[k.Tenant][k.Key], k.ResolvedThreshold)
		}
	}
	return served, dropped, nil
}

// reading is what one threshold key serves: its rows' value and severity.
type reading struct {
	value    float64
	severity string
}

// keyReading is the value and severity of the rows a key owns: one row, or a
// row and its #1231 legacy twin, which carries the same value and severity
// under the old metric name.
func keyReading(tenant, name string, rows []config.ResolvedThreshold) (reading, error) {
	first := rows[0]
	for _, r := range rows[1:] {
		if !sameFloat(r.Value, first.Value) || r.Severity != first.Severity {
			return reading{}, fmt.Errorf("tenant %s: key %q owns rows with different values (%v %s, %v %s)",
				tenant, name, first.Value, first.Severity, r.Value, r.Severity)
		}
	}
	return reading{value: first.Value, severity: first.Severity}, nil
}

// staticSource serves one loaded config to the collector. ConfigInfo is the
// zero value: the exporter fills it from its deployment (flags / git), not
// from the tree.
type staticSource struct{ cfg *config.ThresholdConfig }

func (s staticSource) GetConfig() *config.ThresholdConfig { return s.cfg }
func (s staticSource) GetConfigInfo() config.ConfigInfo   { return config.ConfigInfo{} }

// emitResult is the collector's verdict on one row.
type emitResult struct {
	reported bool
	metric   prometheus.Metric
	err      error
}

// sameSeriesKeys names, for the Gather error message only, the keys of rows
// whose built metrics carry the same label set. Gather has already decided
// the tree fails; this just points at the config keys behind it.
func sameSeriesKeys(keyed []config.KeyedThreshold, results []emitResult) string {
	seen := map[string]string{}
	var named []string
	for i, r := range results {
		if r.metric == nil {
			continue
		}
		var pb dto.Metric
		if r.metric.Write(&pb) != nil {
			continue
		}
		var pairs []string
		for _, lp := range pb.GetLabel() {
			pairs = append(pairs, lp.GetName()+"="+strconv.Quote(lp.GetValue()))
		}
		sort.Strings(pairs)
		id := keyed[i].Tenant + "\x00" + strings.Join(pairs, ",")
		if first, dup := seen[id]; dup {
			named = append(named, fmt.Sprintf("tenant %s: keys %q and %q give one series user_threshold{%s}",
				keyed[i].Tenant, first, keyed[i].Key, strings.Join(pairs, ",")))
			continue
		}
		seen[id] = keyed[i].Key
	}
	if len(named) == 0 {
		return ""
	}
	return " (" + strings.Join(named, "; ") + ")"
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
