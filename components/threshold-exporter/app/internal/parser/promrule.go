package parser

// PrometheusRule CRD parser — turns the standard kube-prometheus
// rule shape into ParsedRule records with dialect classification.
//
// Input shape (subset of the official PrometheusRule v1 spec — only
// the fields C-9 / C-10 actually consume):
//
//   apiVersion: monitoring.coreos.com/v1
//   kind: PrometheusRule
//   metadata: {...}                # ignored by parser
//   spec:
//     groups:
//       - name: <group-name>
//         interval: <duration>     # ignored — group-level, not per rule
//         rules:
//           - alert: HighCPU       # XOR with `record:`
//             expr: <PromQL/MQL>
//             for: 5m
//             labels: {...}
//             annotations: {...}
//           - record: job:cpu:avg
//             expr: avg by (job) (rate(cpu[5m]))
//
// Tolerated input variations:
//   - Bare `groups:` document (no apiVersion/kind/spec wrapper) — VM
//     operator and some legacy Prometheus deployments accept this.
//   - Multi-document YAML (`---` separators) — each document parsed
//     independently; rules are concatenated in document order.
//
// Errors vs warnings:
//   - Empty input bytes                     → fatal error
//   - Malformed YAML (decoder fails)        → fatal error
//   - Document with no `groups`             → warning, skip doc
//   - Per-rule issues (missing alert+record name, malformed expr)
//                                           → warning + best-effort
//                                             ParsedRule (Dialect set
//                                             to ambiguous when expr
//                                             doesn't parse)

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"time"

	"gopkg.in/yaml.v3"
)

// promRuleDoc is the lightweight YAML schema we decode into. We
// intentionally accept both the wrapped (`spec.groups`) and unwrapped
// (`groups:`) shapes — Decode() runs in two passes, see ParsePromRules.
type promRuleDoc struct {
	APIVersion string        `yaml:"apiVersion"`
	Kind       string        `yaml:"kind"`
	Spec       *promRuleSpec `yaml:"spec"`
	Groups     []promRuleGrp `yaml:"groups"` // legacy / unwrapped form
}

type promRuleSpec struct {
	Groups []promRuleGrp `yaml:"groups"`
}

type promRuleGrp struct {
	Name  string         `yaml:"name"`
	Rules []promRuleItem `yaml:"rules"`
}

type promRuleItem struct {
	Alert       string            `yaml:"alert"`
	Record      string            `yaml:"record"`
	Expr        string            `yaml:"expr"`
	For         string            `yaml:"for"`
	Labels      map[string]string `yaml:"labels"`
	Annotations map[string]string `yaml:"annotations"`
}

// ParsePromRules decodes one or more YAML documents containing
// PrometheusRule definitions and returns a ParseResult with one
// ParsedRule per source rule, dialect-classified.
//
//   - sourceFile is recorded into Provenance.SourceFile and used to
//     build SourceRuleID prefixes (`<sourceFile>#groups[i].rules[j]`).
//     Pass an empty string when the bytes don't have a meaningful
//     filesystem path (e.g. `/simulate`-style in-memory feeds).
//   - generatedBy is stamped into Provenance.GeneratedBy verbatim.
//     Conventional format: `da-tools@tools-vX.Y.Z parser@<git-sha>`.
//     Library callers may pass any identifier appropriate to their
//     deployment.
func ParsePromRules(yamlBytes []byte, sourceFile, generatedBy string) (*ParseResult, error) {
	if len(yamlBytes) == 0 {
		return nil, fmt.Errorf("parser: empty input")
	}

	checksum := sha256.Sum256(yamlBytes)
	result := &ParseResult{
		Provenance: Provenance{
			GeneratedBy:    generatedBy,
			SourceFile:     sourceFile,
			ParsedAt:       time.Now().UTC().Format(time.RFC3339),
			SourceChecksum: hex.EncodeToString(checksum[:]),
		},
	}

	dec := yaml.NewDecoder(bytes.NewReader(yamlBytes))
	docIdx := 0
	for {
		var doc promRuleDoc
		err := dec.Decode(&doc)
		if err != nil {
			if errIsEOF(err) {
				break
			}
			return nil, fmt.Errorf("parser: yaml decode doc[%d]: %w", docIdx, err)
		}

		groups := doc.Groups
		if len(groups) == 0 && doc.Spec != nil {
			groups = doc.Spec.Groups
		}
		if len(groups) == 0 {
			// Empty document or wrong shape — record a warning and
			// move on rather than failing the whole batch.
			result.Warnings = append(result.Warnings,
				fmt.Sprintf("doc[%d]: no `groups` (neither at root nor under `spec`); ignored", docIdx))
			docIdx++
			continue
		}

		for gi, grp := range groups {
			for ri, raw := range grp.Rules {
				pr := buildParsedRule(raw, sourceFile, gi, ri)
				if pr.Alert == "" && pr.Record == "" {
					result.Warnings = append(result.Warnings,
						fmt.Sprintf("%s: rule has neither `alert` nor `record` name", pr.SourceRuleID))
				}
				result.Rules = append(result.Rules, pr)
			}
		}
		docIdx++
	}

	return result, nil
}

// buildParsedRule populates one ParsedRule from a raw YAML rule item,
// running dialect analysis on the expression. Errors from the
// dialect analyzer become DialectAmbiguous + AnalyzeError on the
// rule (NOT a top-level failure).
func buildParsedRule(raw promRuleItem, sourceFile string, groupIdx, ruleIdx int) ParsedRule {
	pr := ParsedRule{
		Alert:        raw.Alert,
		Record:       raw.Record,
		Expr:         raw.Expr,
		For:          raw.For,
		Labels:       raw.Labels,
		Annotations:  raw.Annotations,
		SourceRuleID: fmt.Sprintf("%s#groups[%d].rules[%d]", sourceFile, groupIdx, ruleIdx),
	}

	dialect, vmOnly, parseErr := AnalyzeExpr(raw.Expr)
	pr.Dialect = dialect
	pr.VMOnlyFunctions = vmOnly
	pr.PromPortable = (dialect == DialectProm)
	if parseErr != nil {
		pr.AnalyzeError = parseErr.Error()
	}
	return pr
}

// errIsEOF returns true when the yaml.Decoder has reached end of
// stream. yaml.v3 returns io.EOF directly from Decode(), so a
// standard errors.Is check is sufficient.
func errIsEOF(err error) bool {
	return errors.Is(err, io.EOF)
}
