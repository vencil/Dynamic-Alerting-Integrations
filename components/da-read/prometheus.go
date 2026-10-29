package main

import (
	"bytes"
	"errors"
	"fmt"
	"io"
	"os"
	"reflect"
	"sort"

	"github.com/prometheus/common/promslog"
	"github.com/prometheus/prometheus/config"
	// Every service discovery Prometheus itself registers, as the server's
	// main package does (cmd/prometheus imports this package): without it,
	// config.Load refuses `kubernetes_sd_configs` and every other SD key as
	// an unknown field. ADR-036 §12 takes the image size this costs.
	_ "github.com/prometheus/prometheus/discovery/install"
	yamlv2 "go.yaml.in/yaml/v2"
)

// `da-read prometheus`: prometheus.yml as Prometheus loads it, keeping only
// the keys the text writes (ADR-036 §12).
//
// The file goes to config.Load — Prometheus' own loader (the version of the
// Prometheus the platform deploys; a version pin test holds the two equal):
// a strict go.yaml.in/yaml/v2 decode into typed structs, defaults filled in,
// the checks. The *config.Config it returns is walked with the text
// (yamlwalk.go), each value spelled by go.yaml.in/yaml/v2 through the
// types' own MarshalYAML, as Prometheus prints its config; `<<` merges are
// go-yaml v2's (written.go). Secrets are printed as their values
// (commoncfg.MarshalSecretValue, set in alertmanager.go's init).
//
// What config.Load does not cover and Prometheus' LoadFile adds is not done
// here: relative file paths stay as written (LoadFile joins them with the
// file's directory) and the agent-mode checks are not made.
//
// Two refusals are this program's, not the loader's:
//
//   - a YAML document after the first that has content: config.Load reads
//     only the first document and drops the rest without a word;
//   - a `global.external_labels` value that config.Load expands from the
//     environment (`${VAR}`, `$VAR`; `$$` is a literal `$` and is kept): the
//     value Prometheus serves comes from Prometheus' environment, which this
//     program does not have, so any value it printed would be its own.

// formatV2 spells values as Prometheus' config loader's YAML library does.
var formatV2 = yamlFormat{marshal: yamlv2.Marshal, unmarshal: yamlv2.Unmarshal}

// readPrometheus returns da-read's stdout for the prometheus.yml *data*.
func readPrometheus(data []byte) ([]byte, error) {
	cfg, err := config.Load(string(data), promslog.NewNopLogger())
	if err != nil {
		return nil, fmt.Errorf("refused by Prometheus' config loader: %v", err)
	}
	extra, err := laterDocumentV2(data)
	if err != nil {
		return nil, fmt.Errorf("refused by Prometheus' config loader: %v", err)
	}
	if extra > 0 {
		return nil, fmt.Errorf("YAML document %d of the file has content; Prometheus reads only "+
			"the first document and drops the rest without a word, so it is refused here", extra)
	}
	if names, err := externalLabelsFromEnvironment(data); err != nil {
		return nil, fmt.Errorf("refused by Prometheus' config loader: %v", err)
	} else if len(names) > 0 {
		return nil, fmt.Errorf("global.external_labels %v: Prometheus expands the value from its own "+
			"environment, which da-read does not have, so the value it serves cannot be read here", names)
	}
	doc, err := walkDocument(data, reflect.ValueOf(cfg), mergeV2, formatV2)
	if err != nil {
		return nil, err
	}
	var out bytes.Buffer
	out.WriteString(`{"documents":[`)
	out.Write(doc)
	out.WriteString(`]}`)
	return out.Bytes(), nil
}

// laterDocumentV2 is laterDocumentWithContent with the go-yaml v2 Prometheus'
// config loader decodes with (go.yaml.in/yaml/v2).
func laterDocumentV2(data []byte) (int, error) {
	dec := yamlv2.NewDecoder(bytes.NewReader(data))
	for n := 1; ; n++ {
		var v interface{}
		err := dec.Decode(&v)
		if errors.Is(err, io.EOF) {
			return 0, nil
		}
		if err != nil {
			return 0, err
		}
		if n > 1 && v != nil {
			return n, nil
		}
	}
}

// externalLabelsFromEnvironment returns the names of the external labels
// whose written value config.Load expands from the environment, sorted. It
// reads what config.Load reads (go.yaml.in/yaml/v2, which applies the same
// merges) and asks os.Expand with config.Load's mapping which values look up
// a variable.
func externalLabelsFromEnvironment(data []byte) ([]string, error) {
	var probe struct {
		Global struct {
			ExternalLabels map[string]string `yaml:"external_labels"`
		} `yaml:"global"`
	}
	if err := yamlv2.Unmarshal(data, &probe); err != nil {
		return nil, err
	}
	var names []string
	for name, value := range probe.Global.ExternalLabels {
		looksUp := false
		os.Expand(value, func(s string) string {
			if s == "$" {
				return "$"
			}
			looksUp = true
			return ""
		})
		if looksUp {
			names = append(names, name)
		}
	}
	sort.Strings(names)
	return names, nil
}
