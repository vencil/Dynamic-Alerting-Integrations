package config

import (
	"crypto/sha256"
	"errors"
	"fmt"
	"log"
	"sync"

	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// ⛔ ONE VERDICT ON WHICH conf.d FILES CAN BE READ (ADR-036 §2 "Go 端的讀取者也
// 照這份判定", step 6 of §7, #3206). parsetree.Parse — the judgement
// `da-guard parse-tree` prints and the route generator reads — runs before
// any decode of a conf.d file's bytes, and a file it refuses fails that
// decode with *UnreadableFileError. So the exporter, tenant-api's readers
// through this package and da-guard refuse exactly the files the generator
// refuses, each with the same parsetree.Kind.
//
// WHERE IT RUNS: at the decode entries, the functions every plane hands one
// file's bytes to first — parseConfigFile (ParseConfigFile, ParseTenantFile),
// ParseChainDefaults, decodeTenantFile (ParseTenantDoc) and the nested
// platform-file probe (reportUnparseableNestedPlatformFile). Not at the read
// (ReadConfFile): a read hands its bytes to the decode entry of the plane that
// asked, and that decode's failure path is the handling ADR-036 asks for — a
// tenant file declares no tenant (WARN, da_config_parse_failure_total), a `_`
// platform file is dropped whole (ERROR "entire block dropped", the same
// counter) and a defaults carrier joins chain_parse_failed. Every other decode
// in this package (dropNullThresholds, keepReservedMappings, the platform and
// profile overlays, the schedule-null and key-refs walks, …) decodes bytes one
// of these entries already accepted, or skips a file they refused.
//
// The verdict is the file's, not the reader's: the same bytes are refused by
// every entry, with the same kind. What each entry decodes afterwards is
// unchanged.

// UnreadableFileError is the error a decode entry returns for bytes
// parsetree.Parse refuses. Its text names the kind, so every existing log
// line of a parse failure carries it.
type UnreadableFileError struct {
	Kind    parsetree.Kind
	Message string
}

func (e *UnreadableFileError) Error() string {
	return fmt.Sprintf("parse-tree refuses the file (kind %s): %s", e.Kind, e.Message)
}

// judgeConfFile is the verdict: nil for bytes parsetree.Parse reads, an
// *UnreadableFileError otherwise. parsetree.Judge gives that verdict without
// building the content no reader here uses.
func judgeConfFile(data []byte) error {
	return defaultVerdictCache.judge(data)
}

// unreadableFileError is the error for parse-tree's refusal bad, as parse-tree
// gives it: its Message is already bounded at the source
// (parsetree.MaxMessageBytes), so every interface carries the same text.
func unreadableFileError(bad *parsetree.Unreadable) *UnreadableFileError {
	return &UnreadableFileError{Kind: bad.Kind, Message: bad.Message}
}

// verdictCache keeps verdicts by content SHA-256 (a nil value: the file can
// be read).
//
// ⚠️ WHY. One load asks for the same bytes more than once (the walker, then
// the chain merge for every defaults carrier and the tenant merge; every
// reload re-reads the unchanged carriers), and the verdict is a pure function
// of the bytes. Measured (#3206): a 1000-file cold scan 109 ms judging every
// file, 70 ms with every verdict kept.
//
// ⚠️ BOUNDED, AND WHAT THAT COSTS. At max entries the set is dropped whole
// (logged once at DEBUG on logf) — a re-judgement, never a different answer.
// A process that meets more distinct contents than max between two drops
// (a tree of more than max files, or one rewritten that often) keeps
// dropping it, and every judgement then costs what it costs uncached (the
// 109 ms above per 1000 files); nothing else changes. ADR-036 "已知限制".
// The *UnreadableFileError values are shared and never written.
type verdictCache struct {
	max  int
	logf func(format string, args ...any)

	mu sync.Mutex
	m  map[[sha256.Size]byte]*UnreadableFileError
}

// verdictCacheMax is the process cache's bound: above the largest tree
// measured (5,000 files, ScanDirTree_Hierarchical_5000_Cold), so one load
// keeps every verdict; an entry is a key and a pointer (a message is at most
// parsetree.MaxMessageBytes).
const verdictCacheMax = 8192

// defaultVerdictCache is judgeConfFile's. Its log goes to log.Default(): the
// decode entries take no logger, and the one line is a DEBUG note, not a
// verdict.
var defaultVerdictCache = newVerdictCache(verdictCacheMax, log.Printf)

func newVerdictCache(max int, logf func(string, ...any)) *verdictCache {
	return &verdictCache{max: max, logf: logf, m: make(map[[sha256.Size]byte]*UnreadableFileError)}
}

// judge is judgeConfFile over this cache.
func (c *verdictCache) judge(data []byte) error {
	key := sha256.Sum256(data)
	c.mu.Lock()
	v, hit := c.m[key]
	c.mu.Unlock()
	if !hit {
		if bad := parsetree.Judge(data); bad != nil {
			v = unreadableFileError(bad)
		}
		c.mu.Lock()
		cleared := len(c.m) >= c.max
		if cleared {
			c.m = make(map[[sha256.Size]byte]*UnreadableFileError)
		}
		c.m[key] = v
		c.mu.Unlock()
		if cleared && c.logf != nil { // outside the lock: a logger may block
			c.logf("DEBUG: parse-tree verdict cache reached %d entries and was cleared; "+
				"files are judged again (more distinct conf.d contents than the cache holds)", c.max)
		}
	}
	if v == nil {
		return nil // not a typed nil in the error interface
	}
	return v
}

// UnreadableKindOf is the parsetree.Kind of err when a decode entry failed
// because parsetree.Parse refuses the file (anywhere in err's chain), and
// ok=false for any other error.
func UnreadableKindOf(err error) (parsetree.Kind, bool) {
	var ue *UnreadableFileError
	if errors.As(err, &ue) {
		return ue.Kind, true
	}
	return "", false
}

// observeParseFailure counts one parse failure of the file named fileBasename
// on obs (nil: nothing): da_config_parse_failure_total as before, and, when
// err is parse-tree's refusal, da_config_parse_refused_total with its kind.
func observeParseFailure(obs ScanObserver, fileBasename string, err error) {
	if obs == nil {
		return
	}
	obs.IncParseFailure(fileBasename)
	if kind, ok := UnreadableKindOf(err); ok {
		obs.IncParseRefused(fileBasename, string(kind))
	}
}
