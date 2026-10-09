package config

import (
	"errors"
	"fmt"
	"log"
	"os"
	"strings"
)

// RejectDuplicateTenant is the issue-#127 hard reject shared by every full
// load: a tenant declared in two files is a misconfiguration, and the
// caller propagates the typed error without committing any state. The
// walker records the conflict on the scan instead of failing the walk, so
// the flat products of a rejected tree are still available for diagnosis;
// this is the one place that turns the record back into the error callers
// unwrap with errors.As(&DuplicateTenantError{}).
//
// Historically the hierarchical scan was a SECOND walk that could also
// fail on its own (its non-duplicate errors were logged as
// "WARN: hierarchical scan during <loader> failed" and ignored, because
// hierarchical mode is opt-in). With one walk there is no second failure
// to tolerate: a tree the flat plane cannot read is a hard error for the
// load, as it always was.
func RejectDuplicateTenant(scan *TreeScan) error {
	if scan.Conflict != nil {
		return fmt.Errorf("config rejected (mixed-mode duplicate tenant): %w", scan.Conflict)
	}
	return nil
}

// LoadDir is a cold, stateless load of a conf.d directory: the ThresholdConfig
// the exporter's ConfigManager.Load() commits for the same tree (#1988). It
// runs the exporter's own steps in the exporter's order — one ScanDirTree with
// no prior, RejectDuplicateTenant, the empty-tree refusal, the subtree defaults
// chain and ParseDefaultsFiles the hierarchy plane derives from that scan, then
// BuildFlatConfig — so a reader outside package main gets the collector's
// config instead of a re-derivation of it.
//
// Directory mode only: the exporter's single-file mode (a path to one YAML
// file) is not a conf.d tree and is not served here.
//
// logger receives the walker's and the build's WARN/ERROR lines; nil discards
// them, because a library reader called per request would otherwise repeat
// them into its caller's log (see discardLogger). Nothing is counted: there is
// no ScanObserver.
//
// parseFailed is the scan keys (root-relative slash paths, sorted) of the files
// the load skipped because they did not parse — FlatBuild.ParseFailed, the
// files the exporter counts on da_config_parse_failure_total and logs. Such a
// file is skipped, not fatal, exactly as in the exporter, so err stays nil;
// without this list a caller could not tell "this tenant's file is broken"
// from "there is no such tenant" (#1988 W1). nil when every file parsed.
//
// ⚠️ Cold means every file is read and decoded on every call. A caller serving
// requests should cache the result.
func LoadDir(dir string, logger *log.Logger) (cfg *ThresholdConfig, parseFailed []string, err error) {
	cfg, rep, err := LoadDirReport(dir, logger)
	return cfg, rep.ParseFailed, err
}

// ErrNoYAMLFiles is LoadDirReport's refusal of a tree the walk kept no
// config file from: the exporter refuses to load it. When the walk dropped
// files it could not read (every config file of the tree is unreadable), the
// error comes with a LoadReport whose Unreadable names them, so a caller can
// tell that tree from one with no config file at all (#2627). A root the walk
// cannot list is refused with it too, Unreadable holding RootUnreadable.
var ErrNoYAMLFiles = errors.New("no .yaml files found")

// NoTenantReason is why a file in LoadReport.NoTenant contributes no tenant.
const NoTenantReason = "declares no tenant: a file whose name does not start with `_` " +
	"is read only through its `tenants:` mapping, and this one has none (or an empty one)"

// LoadReport is what LoadDirReport says about the files of the tree besides
// the config it built.
type LoadReport struct {
	// ParseFailed is LoadDir's parseFailed.
	ParseFailed []string
	// NoTenant is the scan keys (root-relative slash paths, sorted) of the
	// files whose name does not start with `_` that the walker parsed and
	// found no tenant in (TreeFile.TenantIDs empty, not ParseFailed): a
	// flat-format file with no `tenants:` wrapper, an empty file, or
	// `tenants: {}`. The exporter serves no tenant from such a file and logs
	// nothing about it (#2115 R3). nil when there is none.
	NoTenant []string
	// Unreadable is TreeScan.Unreadable: the config-named entries the walk
	// dropped because their stat or read failed (a dangling symlink, a file
	// the process may not read) and the directories below the root it could
	// not list (everything under them is lost), each with a closed-set
	// reason. The exporter
	// logs a WARN for each and serves the rest of the tree without it — the
	// load still succeeds (#2115). A symlink to a directory is not listed.
	// nil when there is none.
	Unreadable []UnreadableFile
	// RootListErr is set only beside ErrNoYAMLFiles, when the root itself
	// cannot be read: `cannot list configDir` with the walk's reason
	// (Unreadable is then RootUnreadable), or `stat configDir` when the
	// process may not stat it (RootStatUnreadable), for the caller to print
	// (#2627). The walk's own WARN for the root is not logged then: this
	// error carries the same reason.
	RootListErr error
	// Undeliverable is the build's FlatBuild.UnreachableValues filtered by
	// undeliverableThresholds (reserved keys, keys resolveBaseRows never
	// serves and switched-off keys left out): tenantID → each key the tenant inherits
	// from a subtree `_defaults.yaml` that the root `_defaults.yaml` and
	// `optional_overrides:` do not declare, with UnreachableValues' value
	// (the deepest threshold-shaped one, normalised). The exporter serves no
	// series for such a key, logs an ERROR and counts the tenant on
	// da_config_subtree_undeliverable_tenants; the key is in no tenant map of
	// the config. nil when there is none (#1976).
	Undeliverable map[string]map[string]ScheduledValue
	// RootDefaultsUnread is the build's FlatBuild.RootDefaultsUnread: when
	// the root `_defaults.yaml` has no `defaults:` mapping, its top-level
	// keys that act when merged (ActsWhenMerged), so /metrics does not carry
	// them while /effective shows them (#2296). nil when there is none.
	RootDefaultsUnread []UnreadKey
	// WrittenKeys maps a tenant to its EffectiveConfig.KeySpellings (#2031):
	// the config keys every dimensional key by its canonical spelling, and a
	// reader that names keys as the author wrote them — as `da-guard
	// effective` does — spells them through it (EffectiveConfig.WrittenKey's
	// rule). Only tenants with such a key; nil when there is none.
	WrittenKeys map[string]map[string]string
}

// LoadDirReport is LoadDir, also naming the files that contribute no tenant
// (LoadReport.NoTenant), the files the walk could not stat or read
// (LoadReport.Unreadable) and the inherited subtree keys the build could not
// deliver (LoadReport.Undeliverable). It adds no verdict of its own: each is
// read off the walker's or the build's own result on the same cold scan.
//
// A tree with no config file the walk could keep is refused (ErrNoYAMLFiles),
// as the exporter refuses it; the report then carries only Unreadable — the
// files the walk dropped, when every config file is unreadable, or
// RootUnreadable alone when the walk could not list the root at all, the
// error then reading `cannot list configDir` with the walk's reason, or
// RootStatUnreadable when the root cannot even be stat'ed for a reason other
// than a wrong path (StatErrIsWrongPath) (#2627).
func LoadDirReport(dir string, logger *log.Logger) (cfg *ThresholdConfig, rep LoadReport, err error) {
	if logger == nil {
		logger = discardLogger
	}
	absRoot := AbsScanRoot(dir)
	if _, serr := os.Stat(absRoot); serr != nil && !StatErrIsWrongPath(serr) {
		statErr := fmt.Errorf("stat configDir %q: %w", dir, serr)
		return nil, LoadReport{Unreadable: []UnreadableFile{RootStatUnreadable}, RootListErr: statErr},
			fmt.Errorf("%w: %w", statErr, ErrNoYAMLFiles)
	}
	scanLogger, rootWarn := withRootWalkWarnHeld(logger, absRoot)
	scan, err := ScanDirTree(dir, nil, nil, scanLogger)
	// The root's walk WARN is dropped only when RootListErr carries it below
	// (the caller prints that); otherwise — e.g. the root's listing failed
	// part-way, so the walk kept files — it is logged as the walker wrote it.
	reported := rootListReported(scan, err)
	rootWarn.release(reported)
	if err != nil {
		return nil, LoadReport{}, err
	}
	if err := RejectDuplicateTenant(scan); err != nil {
		return nil, LoadReport{}, err
	}
	if reported {
		listErr := fmt.Errorf("cannot list configDir %q: %w", dir, scan.RootWalkErr)
		return nil, LoadReport{Unreadable: []UnreadableFile{RootUnreadable}, RootListErr: listErr},
			fmt.Errorf("%w: %w", listErr, ErrNoYAMLFiles)
	}
	if len(scan.Files) == 0 {
		return nil, LoadReport{Unreadable: scan.Unreadable}, fmt.Errorf("%w in %s", ErrNoYAMLFiles, dir)
	}
	built, err := loadDirBuild(scan, dir, logger, nil)
	if err != nil {
		return nil, LoadReport{}, err
	}
	rep.ParseFailed = built.ParseFailed
	rep.Unreadable = scan.Unreadable
	rep.Undeliverable = undeliverableThresholds(built.UnreachableValues)
	rep.RootDefaultsUnread = built.RootDefaultsUnread
	rep.NoTenant = noTenantKeys(scan)
	rep.WrittenKeys = writtenKeys(scan, &built)
	return &built.Config, rep, nil
}

// noTenantKeys is LoadReport.NoTenant read off a scan: the keys (sorted) of
// the files whose name does not start with `_` that the walker parsed and
// found no tenant in. One function for LoadDirReport and EffectiveTree
// (ScopedTenants.NoTenant), so `da-guard served-values` and `da-guard
// effective` name the same files (#2115). nil when there is none.
func noTenantKeys(scan *TreeScan) []string {
	var out []string
	for _, k := range scan.Keys { // sorted
		f := scan.Files[k]
		if !isPlatformKey(k) && !f.ParseFailed && len(f.TenantIDs) == 0 {
			out = append(out, k)
		}
	}
	return out
}

// rootListReported is LoadDirReport's one condition for "the root could not
// be listed, and RootListErr reports it": the scan succeeded, kept no file,
// and the walk failed on the root itself. It decides both that RootListErr is
// set and that the walker's root WARN is dropped, so the two cannot drift: a
// root whose listing failed part-way (files kept) is not reported this way,
// and its WARN is logged (#2627).
func rootListReported(scan *TreeScan, scanErr error) bool {
	return scanErr == nil && scan != nil && len(scan.Files) == 0 && scan.RootWalkErr != nil
}

// withRootWalkWarnHeld is logger with the walker's WARN for the root
// directory itself (`WARN: walk error at <absRoot>: …`) held back until the
// caller knows whether RootListErr will carry the same reason (#2627): the
// returned filter's release(true) drops it (the caller prints RootListErr),
// release(false) logs it — after the walk's other lines. Every other line
// goes to logger at once, unchanged, through its own prefix and flags.
func withRootWalkWarnHeld(logger *log.Logger, absRoot string) (*log.Logger, *rootWarnFilter) {
	f := &rootWarnFilter{dst: logger, hold: "WARN: walk error at " + absRoot + ": "}
	if logger == discardLogger {
		return logger, f
	}
	return log.New(f, "", 0), f
}

// rootWarnFilter is withRootWalkWarnHeld's writer.
type rootWarnFilter struct {
	dst  *log.Logger
	hold string
	held []string
}

func (f *rootWarnFilter) Write(p []byte) (int, error) {
	line := strings.TrimSuffix(string(p), "\n")
	if strings.HasPrefix(line, f.hold) {
		f.held = append(f.held, line)
		return len(p), nil
	}
	if err := f.dst.Output(2, line); err != nil {
		return 0, err
	}
	return len(p), nil
}

// release ends the hold: drop discards the held lines (the reason is
// reported another way), otherwise they are logged.
func (f *rootWarnFilter) release(drop bool) {
	if !drop {
		for _, line := range f.held {
			_ = f.dst.Output(2, line)
		}
	}
	f.held = nil
}

// loadDirBuild is LoadDir's build step over a scan it already has: the
// subtree defaults chain and ParseDefaultsFiles derived from that scan, then
// BuildFlatConfig. Split out so ScopeEffective can take the exporter's
// parse-failure verdict (FlatBuild.ParseFailed) from its own scan instead of
// re-deriving "which files are broken" (#2179).
//
// A scan with a duplicate-tenant Conflict has no inheritance graph (the
// exporter rejects such a tree before this step; ScopeEffective does not
// when the duplicate is outside its scope), so no subtree chain applies.
// Per-file parse verdicts do not depend on the chain.
//
// profileLogf is FlatBuildInput.ProfileLogf (nil = logger.Printf).
func loadDirBuild(scan *TreeScan, dir string, logger *log.Logger, profileLogf func(format string, args ...any)) (FlatBuild, error) {
	return BuildFlatConfig(scan, loadDirBuildInput(scan, dir, logger, profileLogf))
}

// loadDirBuildInput is loadDirBuild's input: the subtree defaults chain and
// ParseDefaultsFiles derived from the scan. Split out so ScopeEffective reads
// the subtree chain the build was given (subtreeReservedKeys, #2388) rather
// than deriving its own.
func loadDirBuildInput(scan *TreeScan, dir string, logger *log.Logger, profileLogf func(format string, args ...any)) FlatBuildInput {
	// Mirrors populateHierarchyStateFrom: a tree with neither a defaults file
	// nor a tenant installs no hierarchy state, so no subtree chain applies.
	var tenantDefaults map[string][]string
	var parsedDefaults map[string]map[string]any
	if len(scan.Defaults) > 0 || len(scan.Tenants) > 0 {
		if g := scan.InheritanceGraph(); g != nil {
			tenantDefaults = g.TenantDefaults
		}
		parsedDefaults = ParseDefaultsFiles(scan.Defaults, scanDefaultsSource(scan), logger)
	}

	return FlatBuildInput{
		Root:           dir,
		TenantDefaults: tenantDefaults,
		ParsedDefaults: parsedDefaults,
		Logger:         logger,
		ProfileLogf:    profileLogf,
	}
}

// scanDefaultsSource serves ParseDefaultsFiles from the bytes this scan
// already read (a cold scan has no priors, so every file it read carries
// Data), falling back to the disk for a file it did not. Each file is parsed
// once, as the cold load does (#1978).
func scanDefaultsSource(scan *TreeScan) DefaultsSource {
	data := make(map[string][]byte, len(scan.Files))
	for _, f := range scan.Files {
		if f.Data != nil {
			data[f.AbsPath] = f.Data
		}
	}
	return func(absPath string) ([]byte, ChainDefaults, error) {
		b, ok := data[absPath]
		if !ok {
			var err error
			if b, err = os.ReadFile(absPath); err != nil {
				return nil, ChainDefaults{}, err
			}
		}
		return b, ParseChainDefaults(b), nil
	}
}
