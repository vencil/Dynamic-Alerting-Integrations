package config

import (
	"log"
	"os"
	"strings"

	"gopkg.in/yaml.v3"
)

// parseDefaultsBytes turns raw _defaults.yaml bytes into the same
// shape that extractDefaultsBlock(normalizeYAMLToJSON(...)) produces
// elsewhere in the codebase. Returns nil with no error for an empty
// document (legacy flat configs may have empty/whitespace-only files).
//
// This duplicates the pipeline used in computeEffectiveConfig but
// stops before the merge step — we only need the parsed dict for
// key-level diffing.
func parseDefaultsBytes(b []byte) (map[string]any, error) {
	if len(strings.TrimSpace(string(b))) == 0 {
		return map[string]any{}, nil
	}
	var doc any
	if err := yaml.Unmarshal(b, &doc); err != nil {
		return nil, err
	}
	normalized := normalizeYAMLToJSON(doc)
	block := extractDefaultsBlock(normalized)
	if block == nil {
		return map[string]any{}, nil
	}
	return block, nil
}

// ParseDefaultsBytes is the exported form of parseDefaultsBytes, for package
// main's forwarder (config_defaults_diff.go).
func ParseDefaultsBytes(b []byte) (map[string]any, error) { return parseDefaultsBytes(b) }

// ParseDefaultsFiles parses every defaults file of a scan (TreeScan.Defaults)
// with parseDefaultsBytes, keyed by the same absolute path. A file that cannot
// be read or parsed is logged and left out, so one broken defaults file cannot
// poison the rest. Moved from package main's populateHierarchyStateFrom
// (#1988); logger must be non-nil.
func ParseDefaultsFiles(defaults map[string]bool, logger *log.Logger) map[string]map[string]any {
	newParsedDefaults := make(map[string]map[string]any, len(defaults))
	for dp := range defaults {
		b, rerr := os.ReadFile(dp)
		if rerr != nil {
			logger.Printf("WARN: parsedDefaults cache: read %s: %v", dp, rerr)
			continue
		}
		parsed, perr := parseDefaultsBytes(b)
		if perr != nil {
			logger.Printf("WARN: parsedDefaults cache: parse %s: %v", dp, perr)
			continue
		}
		newParsedDefaults[dp] = parsed
	}
	return newParsedDefaults
}
