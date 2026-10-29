package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"reflect"
	"strings"

	"github.com/prometheus/common/model"
	"github.com/prometheus/common/promslog"
	"github.com/prometheus/prometheus/model/rulefmt"
	"github.com/prometheus/prometheus/promql/parser"
	"go.yaml.in/yaml/v3"
)

// `da-read prometheus-rules`: a Prometheus rule file as Prometheus' rule
// manager loads it, keeping only the keys the text writes (ADR-036 §12).
//
// The file goes to rulefmt.Parse with the arguments Prometheus v3.15 passes
// when it loads rule files (github.com/prometheus/prometheus v0.315.0):
//
//   - rules/manager.go Manager.Update → LoadGroups(..., ignoreUnknownFields
//     false, ...) → FileLoader.Load → rulefmt.ParseFile → rulefmt.Parse: an
//     unknown key is refused (the yaml.v3 decoder's KnownFields(true));
//   - the name validation scheme is ManagerOptions.NameValidationScheme,
//     which cmd/prometheus/main.go sets to the prometheus.yml's
//     global.metric_name_validation_scheme, UTF-8 unless it says otherwise
//     (config.DefaultGlobalConfig; rules.NewManager also takes an unset one
//     as UTF-8). This program reads the rule file alone, so it judges with
//     UTF-8: a deployment whose prometheus.yml asks for `legacy` judges label
//     names more strictly than this;
//   - the PromQL parser is cmd/prometheus/main.go's
//     parser.NewParser(cfg.parserOpts): parser.Options{ExperimentalDurationExpr:
//     true} with no feature flag (the deployed Prometheus,
//     k8s/03-monitoring/deployment-prometheus.yaml, passes no
//     --enable-feature), so experimental PromQL functions are refused.
//
// rulefmt decodes with go.yaml.in/yaml/v3, so the walk (yamlwalk.go) spells
// values with it and composes `<<` merges by v3's rules (written.go).
// rulefmt reads only the first YAML document and logs a warning for the
// rest; Prometheus loads only the first, so a later document with content
// (anything but `---`, comments or an explicit null) is refused here.

// formatV3 spells values as rulefmt's YAML library does.
var formatV3 = yamlFormat{marshal: yaml.Marshal, unmarshal: yaml.Unmarshal}

// ruleNameValidation and ruleParser are what Prometheus v3.15 loads rule
// files with when nothing overrides them (see above).
var (
	ruleNameValidation = model.UTF8Validation
	ruleParser         = parser.NewParser(parser.Options{ExperimentalDurationExpr: true})
)

// readRuleFile returns the rule file *data* as Prometheus loads it: one JSON
// document, the rule groups with only the keys the text writes.
func readRuleFile(data []byte) ([]byte, error) {
	groups, errs := rulefmt.Parse(data, false, ruleNameValidation, ruleParser, promslog.NewNopLogger())
	if len(errs) > 0 {
		msgs := make([]string, len(errs))
		for i, e := range errs {
			msgs[i] = e.Error()
		}
		return nil, fmt.Errorf("refused by Prometheus' rule file loader: %s", strings.Join(msgs, "; "))
	}
	extra, err := laterDocumentV3(data)
	if err != nil {
		return nil, fmt.Errorf("refused by Prometheus' rule file loader: %v", err)
	}
	if extra > 0 {
		return nil, fmt.Errorf("YAML document %d of the file has content; Prometheus loads only "+
			"the first document of a rule file and drops the rest, so it is refused here", extra)
	}
	return walkDocument(data, reflect.ValueOf(groups), mergeV3, formatV3)
}

// laterDocumentV3 is laterDocumentWithContent with rulefmt's go-yaml v3,
// composing each document into a node: a decode into interface{} would
// refuse a mapping with two `<<` keys, which rulefmt's struct decode takes.
func laterDocumentV3(data []byte) (int, error) {
	dec := yaml.NewDecoder(bytes.NewReader(data))
	for n := 1; ; n++ {
		var doc yaml.Node
		err := dec.Decode(&doc)
		if errors.Is(err, io.EOF) {
			return 0, nil
		}
		if err != nil {
			return 0, err
		}
		if n > 1 && len(doc.Content) > 0 && !isNull(doc.Content[0]) {
			return n, nil
		}
	}
}
