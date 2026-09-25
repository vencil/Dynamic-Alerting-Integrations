package config

import (
	"log"
	"strings"
)

// parseDefaultsBytes turns raw _defaults.yaml bytes into the same
// shape that extractDefaultsBlock(normalizeYAMLToJSON(...)) produces
// elsewhere in the codebase. Returns nil with no error for an empty
// document (legacy flat configs may have empty/whitespace-only files).
//
// The parse is the merge's own first half (ParseChainDefaults, #1978),
// stopped before the merge step — we only need the parsed dict for
// key-level diffing — so a cold load that already parsed a file for the
// merge hands the same parse here (DefaultsDict) instead of a second one.
func parseDefaultsBytes(b []byte) (map[string]any, error) {
	if isBlankDefaults(b) {
		return map[string]any{}, nil
	}
	return ParseChainDefaults(b).defaultsDict()
}

// ParseDefaultsBytes is the exported form of parseDefaultsBytes, for package
// main's forwarder (config_defaults_diff.go).
func ParseDefaultsBytes(b []byte) (map[string]any, error) { return parseDefaultsBytes(b) }

// DefaultsDict is parseDefaultsBytes(b) given p = ParseChainDefaults(b)
// already made: whitespace-only bytes are an empty map without being judged
// (even when YAML rejects them, e.g. a stray tab), a syntax error is
// returned, and a document without a defaults mapping is an empty map.
func DefaultsDict(b []byte, p ChainDefaults) (map[string]any, error) {
	if isBlankDefaults(b) {
		return map[string]any{}, nil
	}
	return p.defaultsDict()
}

func isBlankDefaults(b []byte) bool { return len(strings.TrimSpace(string(b))) == 0 }

func (p ChainDefaults) defaultsDict() (map[string]any, error) {
	if p.err != nil {
		return nil, p.err
	}
	if p.block == nil {
		return map[string]any{}, nil
	}
	return p.block, nil
}

// DefaultsSource yields one defaults file's bytes and its ParseChainDefaults
// parse, or the error reading it. A cold load passes the source its merge
// already filled (#1978), so the cache below and the merged_hash chains share
// one read and one parse per file.
type DefaultsSource func(absPath string) (raw []byte, parsed ChainDefaults, err error)

// ParseDefaultsFiles parses every defaults file of a scan (TreeScan.Defaults)
// with parseDefaultsBytes' contract, keyed by the same absolute path. A file
// that cannot be read or parsed is logged and left out, so one broken
// defaults file cannot poison the rest. Moved from package main's
// populateHierarchyStateFrom (#1988); src and logger must be non-nil.
func ParseDefaultsFiles(defaults map[string]bool, src DefaultsSource, logger *log.Logger) map[string]map[string]any {
	newParsedDefaults := make(map[string]map[string]any, len(defaults))
	for dp := range defaults {
		b, pd, rerr := src(dp)
		if rerr != nil {
			logger.Printf("WARN: parsedDefaults cache: read %s: %v", dp, rerr)
			continue
		}
		parsed, perr := DefaultsDict(b, pd)
		if perr != nil {
			logger.Printf("WARN: parsedDefaults cache: parse %s: %v", dp, perr)
			continue
		}
		newParsedDefaults[dp] = parsed
	}
	return newParsedDefaults
}
