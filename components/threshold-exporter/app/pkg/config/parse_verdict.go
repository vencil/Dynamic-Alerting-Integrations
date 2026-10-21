package config

import (
	"crypto/sha256"
	"errors"
	"fmt"
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
//
// ⚠️ CACHED BY CONTENT. One load asks it more than once for the same bytes
// (the walker, then the chain merge for every defaults carrier and the tenant
// merge; every reload re-reads the unchanged carriers), and the verdict is a
// pure function of the bytes, so it is kept by their SHA-256 — measured
// cheaper than a second judgement by two orders of magnitude on a conf.d
// file. The set is bounded (verdictCacheMax): when full it is dropped whole,
// which costs a re-judgement, never a different answer.
func judgeConfFile(data []byte) error {
	key := sha256.Sum256(data)
	verdictCache.mu.Lock()
	v, hit := verdictCache.m[key]
	verdictCache.mu.Unlock()
	if !hit {
		if bad := parsetree.Judge(data); bad != nil {
			v = &UnreadableFileError{Kind: bad.Kind, Message: bad.Message}
		}
		verdictCache.mu.Lock()
		if len(verdictCache.m) >= verdictCacheMax {
			verdictCache.m = make(map[[sha256.Size]byte]*UnreadableFileError)
		}
		verdictCache.m[key] = v
		verdictCache.mu.Unlock()
	}
	if v == nil {
		return nil // not a typed nil in the error interface
	}
	return v
}

// verdictCacheMax bounds verdictCache: past it the set is dropped whole.
// Above the largest tree measured (5,000 files, ScanDirTree_Hierarchical_5000)
// so one load keeps every verdict; each entry is a key and a pointer.
const verdictCacheMax = 8192

// verdictCache is judgeConfFile's verdicts by content SHA-256 (nil = the file
// can be read). The *UnreadableFileError values are shared and never written.
var verdictCache = struct {
	mu sync.Mutex
	m  map[[sha256.Size]byte]*UnreadableFileError
}{m: make(map[[sha256.Size]byte]*UnreadableFileError)}

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
