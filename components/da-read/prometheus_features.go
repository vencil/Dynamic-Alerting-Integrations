package main

import (
	"encoding/json"
	"fmt"
	"sort"
	"strings"

	"github.com/prometheus/prometheus/promql/parser"
)

// `da-read prometheus-features -`: how each Prometheus `--enable-feature`
// option bears on what da-read reads (#4151).
//
// da-read reads rules offline with the PromQL parser Prometheus loads rule
// files with (prometheus_rules.go). That parser is built from the
// --enable-feature options the deployed Prometheus passes, so da-read must
// be built from the same ones — and it cannot read them at run time: it is
// handed a customer's rule file, not the manifest that runs the Prometheus.
// So the options are written here once (deployedFeatures), and a test that
// reads every Prometheus the repo runs (tests/ops/
// test_da_read_prometheus_features.py) holds them to what those pass, asking
// this subcommand how each one it finds bears on da-read.
//
// Standard input is a JSON array of --enable-feature values, each split on
// commas the way Prometheus splits them; stdout is
//
//	{"documents": [{"features": {"<option>": "<bearing>", …},
//	                "parser": ["<option>", …]}]}
//
// with "parser" the options deployedFeatures builds ruleParser from, and
// each <bearing> one of:
//
//	parser        sets a parser.Options field: what the parser accepts.
//	not-read      changes no expression and no label da-read reads —
//	              storage, scraping, the web UI, sample values, which series
//	              are stored, or an option Prometheus now ignores.
//	not-modelled  changes what da-read reads but da-read does not model it
//	              (which labels a series or a query result carries): a
//	              Prometheus run with it judges label flow differently.
//	unknown       not an option of the Prometheus release linked here.

// featureBearing is what one --enable-feature option of the linked
// Prometheus (cmd/prometheus/main.go setFeatureListOptions, v0.316.0) does
// to what da-read reads. ⛔ Every option that function handles has an entry:
// TestFeatureTableIsPrometheusOwn reads that function's switch and fails on
// an option missing here, one here it does not handle, or a "parser" entry
// whose field is not the one Prometheus sets.
type featureBearing struct {
	bearing string
	// field is the parser.Options field a "parser" option sets.
	field string
}

const (
	bearingParser      = "parser"
	bearingNotRead     = "not-read"
	bearingNotModelled = "not-modelled"
	bearingUnknown     = "unknown"
)

var featureTable = map[string]featureBearing{
	"promql-experimental-functions": {bearingParser, "EnableExperimentalFunctions"},
	"promql-binop-fill-modifiers":   {bearingParser, "EnableBinopFillModifiers"},
	"promql-unquoted-utf8-names":    {bearingParser, "EnableUnquotedUTF8Names"},

	// The engine drops __name__ later, so which results carry it changes.
	"promql-delayed-name-removal": {bearing: bearingNotModelled},
	// Series and query results carry __type__ and __unit__.
	"type-and-unit-labels": {bearing: bearingNotModelled},

	// Permanently enabled or ignored by this release.
	"promql-duration-expr":            {bearing: bearingNotRead},
	"promql-extended-range-selectors": {bearing: bearingNotRead},
	"native-histograms":               {bearing: bearingNotRead},
	"ooo-native-histograms":           {bearing: bearingNotRead},
	// Sample values and evaluation, not expressions or labels.
	"use-start-timestamps":  {bearing: bearingNotRead},
	"promql-per-step-stats": {bearing: bearingNotRead},
	"concurrent-rule-eval":  {bearing: bearingNotRead},
	"query-cost":            {bearing: bearingNotRead},
	// Storage, scraping, ingestion and the web UI.
	"exemplar-storage":                 {bearing: bearingNotRead},
	"memory-snapshot-on-shutdown":      {bearing: bearingNotRead},
	"extra-scrape-metrics":             {bearing: bearingNotRead},
	"metadata-wal-records":             {bearing: bearingNotRead},
	"auto-reload-config":               {bearing: bearingNotRead},
	"created-timestamp-zero-ingestion": {bearing: bearingNotRead},
	"xor2-encoding":                    {bearing: bearingNotRead},
	"histograms-st-encoding":           {bearing: bearingNotRead},
	"st-synthesis":                     {bearing: bearingNotRead},
	"st-storage":                       {bearing: bearingNotRead},
	"delayed-compaction":               {bearing: bearingNotRead},
	"old-ui":                           {bearing: bearingNotRead},
	"otlp-deltatocumulative":           {bearing: bearingNotRead},
	"otlp-native-delta-ingestion":      {bearing: bearingNotRead},
	"openmetrics2":                     {bearing: bearingNotRead},
	"zstd-scrape":                      {bearing: bearingNotRead},
	"use-uncached-io":                  {bearing: bearingNotRead},
	"fast-startup":                     {bearing: bearingNotRead},
	"search-api":                       {bearing: bearingNotRead},
}

// deployedFeatures is the --enable-feature options every Prometheus the
// platform runs passes that set a parser option — none today
// (k8s/03-monitoring/deployment-prometheus.yaml and the compose stacks pass
// no --enable-feature). ⛔ Only "parser" options belong here: one that is
// "not-modelled" cannot be honoured by da-read, and parserOptions refuses it.
var deployedFeatures []string

// parserOptions is the parser.Options Prometheus builds from options.
func parserOptions(options []string) (parser.Options, error) {
	var opts parser.Options
	for _, o := range options {
		fb, ok := featureTable[o]
		if !ok || fb.bearing != bearingParser {
			return parser.Options{}, fmt.Errorf("--enable-feature=%s does not set a parser option", o)
		}
		switch fb.field {
		case "EnableExperimentalFunctions":
			opts.EnableExperimentalFunctions = true
		case "EnableBinopFillModifiers":
			opts.EnableBinopFillModifiers = true
		case "EnableUnquotedUTF8Names":
			opts.EnableUnquotedUTF8Names = true
		default:
			return parser.Options{}, fmt.Errorf("--enable-feature=%s: no parser.Options field %q", o, fb.field)
		}
	}
	return opts, nil
}

// mustParserOptions is parserOptions for the package-level ruleParser.
func mustParserOptions(options []string) parser.Options {
	opts, err := parserOptions(options)
	if err != nil {
		panic(err)
	}
	return opts
}

// readPrometheusFeatures answers `prometheus-features` for values.
func readPrometheusFeatures(values []string) ([]byte, error) {
	features := map[string]string{}
	for _, v := range values {
		for _, o := range strings.Split(v, ",") {
			fb, ok := featureTable[o]
			if !ok {
				features[o] = bearingUnknown
				continue
			}
			features[o] = fb.bearing
		}
	}
	deployed := append([]string{}, deployedFeatures...)
	sort.Strings(deployed)
	return json.Marshal(struct {
		Documents []any `json:"documents"`
	}{Documents: []any{struct {
		Features map[string]string `json:"features"`
		Parser   []string          `json:"parser"`
	}{features, deployed}}})
}
