// Package gitops implements commit-on-write operations for tenant config files.
//
// Design (ADR-009, ADR-011):
//   - All write operations hold a sync.Mutex to prevent concurrent git conflicts.
//   - Each write records the HEAD commit before and after to detect conflicts.
//   - Commits use the operator's email as git author for audit trail.
//   - Schema validation is run before any disk write.
//   - v2.6.0: PR-based write-back mode (ADR-011) creates feature branches
//     and pushes for external PR creation instead of committing to the main branch.
package gitops

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"log"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/vencil/tenant-api/internal/boundedcall"
	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/customalerts"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
	"github.com/vencil/threshold-exporter/pkg/pyyamlcompat"
	"github.com/vencil/threshold-exporter/pkg/tenantid"
	"gopkg.in/yaml.v3"
)

// ErrConflict is returned when the git HEAD moved during a write operation.
var ErrConflict = errors.New("conflict: repository was updated concurrently, please refresh and retry")

// ErrPendingPR is returned when a tenant already has a pending PR (PR mode only).
var ErrPendingPR = errors.New("pending PR exists for this tenant")

// ErrPrecondition reports that a caller-supplied base hash no longer describes
// the tenant file on disk: someone else wrote it between the caller's read and
// this write, so overwriting would silently drop their change. Callers match it
// with errors.Is; errors.As on *PreconditionError also yields the hash the file
// actually has, so a client can refresh against the right base.
var ErrPrecondition = errors.New("precondition failed: the tenant configuration changed since it was read")

// PreconditionError carries both sides of a failed base-hash check.
//
// Current is the hash the file has NOW; it is empty when the tenant file does
// not exist at all (deleted, or never created), which is reported the same way
// — a caller holding a base hash for a file that is gone is just as stale as
// one holding an outdated hash, and both need a fresh read before retrying.
type PreconditionError struct {
	TenantID string
	Expected string
	Current  string
}

func (e *PreconditionError) Error() string {
	current := e.Current
	if current == "" {
		current = "<no such tenant file>"
	}
	return fmt.Sprintf("%s (tenant %s: expected base %s, on disk %s)",
		ErrPrecondition.Error(), e.TenantID, e.Expected, current)
}

// Unwrap makes errors.Is(err, ErrPrecondition) hold for the typed error.
func (e *PreconditionError) Unwrap() error { return ErrPrecondition }

// ErrForgeDegraded is returned when the in-lock base fetch (TRK-318) exceeds
// TA_GIT_FETCH_TIMEOUT — the forge is unreachable or too slow to refresh the
// local base. The writer mutex is released as the caller returns and the
// handler maps this to a 503 so the client retries, rather than the write
// silently proceeding from a STALE base (which would risk rolling back a shared
// file another tenant already merged remotely — the whole TRK-318 hazard).
var ErrForgeDegraded = errors.New("forge degradation: base fetch timed out — write lock released")

// MsgUnparseableBaseNeedsPlatformWrite is the validation text for a
// whole-file write over a current tenant file that cannot be parsed, by a
// caller without platform-wide write permission (#2405; see eolGuardErrs).
const MsgUnparseableBaseNeedsPlatformWrite = "the current tenant file cannot be parsed; " +
	"replacing it through the API requires write permission on all tenants " +
	"(an RBAC rule with tenants: [\"*\"] and no org-scope, environments or domains); " +
	"otherwise repair the file in git"

// replaceUnparseableKey is the context key WithReplaceUnparseable sets.
type replaceUnparseableKey struct{}

// WithReplaceUnparseable marks ctx as carrying a caller with platform-wide
// write permission, allowing Write / WriteIfUnchanged / WritePR /
// DryRunValidate to replace a current tenant file that cannot be parsed
// (#2405). Absent the mark, such a write is refused. The permission decision is
// the handler's (rbac.PlatformUnrestricted); the Writer only reads the bit, so
// the check stays under the writer lock, on the file the write lands on.
func WithReplaceUnparseable(ctx context.Context) context.Context {
	return context.WithValue(ctx, replaceUnparseableKey{}, true)
}

func replaceUnparseableAllowed(ctx context.Context) bool {
	ok, _ := ctx.Value(replaceUnparseableKey{}).(bool)
	return ok
}

// ErrValidation wraps a schema/structural validation failure of the incoming
// YAML. It lets handlers distinguish a CLIENT error (malformed body → HTTP 400)
// from a server-side write failure (500): the direct-write path already returned
// 400 for these, but the PR-mode path previously mapped every non-retryable
// write error to 500 (#795 F1). Returned by Write / WritePR / WritePRBatch via
// fmt.Errorf("%w: …", ErrValidation, …), so errors.Is(err, ErrValidation) holds.
var ErrValidation = errors.New("validation failed")

// ErrNoChanges is returned by WritePR when the single body is a byte-identical
// no-op, and by WritePRBatch when EVERY op is one (an idempotent batch / a
// client retry): the feature branch would carry no commits beyond base, so
// pushing it and opening a PR/MR would yield a change-free PR (or a forge 422). The handler maps this to a clean "no changes"
// success — the PR-mode analogue of WriteMerged's direct-path no-op short-circuit
// (#1097 / #1102 review). Unusually for a Go error return, the accompanying
// *PRWriteResult is NON-nil in this case: it carries the per-op deprecation
// notices (#1231 F5) so the no-changes 200 keeps the migration signal, matching
// WriteMerged's "no-op still returns notices" invariant.
var ErrNoChanges = errors.New("no changes: batch produced no commits")

// ErrReservedTenantID is a defense-in-depth backstop for the tenant write
// methods: it fires when an id's {id}.yaml is a reserved conf.d control file
// (_*, .*) — i.e. an id that ValidateTenantID rejects at the handler. Every
// current write path validates the id first, so reaching this means a caller
// bypassed that gate (a programming error): the writer refuses rather than let
// a tenant write clobber platform config, mirroring MutateConfigFile's own
// filepath.Base defense on the control-file write path. See internal/confd for
// the single "what counts as a tenant file" predicate shared with the scanners.
var ErrReservedTenantID = errors.New("reserved tenant id: names a conf.d control file")

// ErrInvalidTenantID refuses a tenant write to an id the tenant-id rule
// refuses (ADR-035: pkg/tenantid, a DNS-1123 label) — the route generator
// refuses a conf.d tree that declares such an id, in every mode. Raised by
// guardTenantID, so it covers every tenant write method and the dry-runs;
// the handlers' ValidateWritableTenantID refuses the same ids first with an
// early 400, and the handlers map this one to 400 too. Reads never call
// guardTenantID's rule (confd.IsAddressableTenantID stays as it is), so an
// existing tenant with such an id can still be read.
var ErrInvalidTenantID = errors.New("invalid tenant id")

// ErrTenantDeclaredElsewhere refuses a tenant write to `<configDir>/<id>.yaml`
// (or its `.yml` spelling) when some OTHER conf.d file declares the id — a
// subdirectory file (`team/x.yaml`) or a shared top-level file whose
// `tenants:` map carries it (#2078). confd.TenantFilePathForWrite only looks
// for `<id>.yaml|.yml` at the top level, so without this guard the write
// "succeeds" and leaves one id in two files: the running exporter keeps the
// old config (the new value never takes effect) and the next restart exits
// with `config rejected (mixed-mode duplicate tenant)`. It applies whether or
// not `<id>.yaml` already exists: an existing file need not declare the id.
//
// Also returned (wrapping the *cfg.DuplicateTenantError) when the id is
// ALREADY declared by two or more files — including `<id>.yaml` itself plus
// another: the exporter rejects that tree already, so even an update of
// `<id>.yaml` is refused until the other declaration is removed.
//
// `_`-prefixed platform files are not declarations (the walker never parses
// them for tenants), so a `tenants:` block in `_extra.yaml` does NOT trip this.
//
// ⛔ The wrapped message names the other file(s) and is for the server log
// only; handlers render a fixed message (a caller restricted by RBAC must not
// learn other files' names). Handlers map it to 409.
var ErrTenantDeclaredElsewhere = errors.New("tenant is already declared by another conf.d file")

// ErrTenantTreeScan reports that the conf.d walk the ErrTenantDeclaredElsewhere
// guard depends on could not run or did not finish in time (see scanTree). The
// guard fails CLOSED: when it cannot tell whether the write would duplicate a
// declaration, nothing is written. It is a server-side condition (configDir
// unreadable, or a file whose read never returns), so handlers map it to 500
// with a fixed message.
var ErrTenantTreeScan = errors.New("cannot scan conf.d to check where the tenant is declared")

// ErrBaseRestore is returned by WritePR / WritePRBatch when, after the feature
// branch was committed (and usually pushed), returning the worktree to a clean
// base failed on both attempts (#2070). The tree is then left on the feature
// branch, and every endpoint that reads conf.d would serve the un-merged
// proposal as if it were the configuration — so the write must NOT report
// success. The handler maps it to a generic 500.
//
// ⛔ Returned errors wrap ONLY this sentinel; the underlying git error is
// rendered with %v, never %w. gitErr maps index.lock contention to
// ErrWriteOverloaded, and that sentinel (like ErrForgeDegraded) makes the
// handler answer 503 "retry" — but the push has already succeeded, so a retry
// would cut and push a second branch for the same change. The message names
// the branch and whether it was pushed, so an operator can find a branch that
// is on origin with no PR/MR opened for it.
var ErrBaseRestore = errors.New("failed to return the config worktree to the base branch after a PR-mode write")

// yamlDocumentShapeErrors is the whole-body YAML gate behind validateShape
// (#1681, #1721). It exists because Write commits the body VERBATIM while the
// struct yaml.Unmarshal in validateShape reads only the FIRST document and only
// the fields ThresholdConfig names — so any byte the struct decode skips is a
// byte that reaches git unjudged, and the exporter reads it.
//
// It walks every document as a yaml.Node and refuses, in this order:
//
//  1. Any decode error, in ANY document. The struct Unmarshal does not own
//     these: it stops after the first document, so a parse error behind an
//     end-of-document marker (`...`) is invisible to it. A gate that treated
//     that error as end-of-stream accepted the unparseable tail, and the
//     unparseable tail is where the smuggled section sat (#1721).
//  2. In the FIRST document only, a root key the struct decode and
//     cfg.CheckTenantRootKeys can both look past (see rootKeyShapeError):
//     a merge key (`<<`, checked first, with its own message), or any key
//     that is not a plain string scalar — null (`~`, `null`, an empty `? `),
//     int, bool, an alias, a complex key, a custom tag. CheckTenantRootKeys
//     decodes into map[string]any, fails on such a key and then reports
//     NOTHING, while a generic decode falls back to map[any]any and succeeds,
//     so the key's value — a `tenants:` section, a `defaults:` block — would
//     be committed unjudged. A string key other than `tenants` is NOT refused
//     here: CheckTenantRootKeys owns that and names the key. Merge keys and
//     aliases INSIDE a tenant section are ordinary YAML and stay legal.
//  3. A document that does not decode generically (`node.Decode(&any)`). The
//     struct decode never visits a root key ThresholdConfig lacks, so a bad
//     tag under one passes it, and CheckTenantRootKeys returns no errors when
//     its own decode fails. This also refuses an unconstructable tagged value
//     inside the body's own tenant section (`!!int "abc"`), which the struct
//     decode reads as a string — deliberately: those bytes would be committed.
//  4. Content after the first document — anything after it would be written
//     but never validated. An empty trailer (a bare `---`, a lone `...`, a
//     comment-only or `~` document) carries nothing and stays legal, so this
//     counts content rather than documents.
//
// Messages go to API callers: they name the rule, never a server path.
func yamlDocumentShapeErrors(yamlContent string) []string {
	dec := yaml.NewDecoder(strings.NewReader(yamlContent))
	extra := 0
	for i := 0; ; i++ {
		var node yaml.Node
		if err := dec.Decode(&node); err != nil {
			if errors.Is(err, io.EOF) {
				break
			}
			return []string{fmt.Sprintf("invalid YAML in document %d: %v", i+1, err)}
		}
		// Before the generic decode: a complex root key makes that decode fail
		// with a message about the key's value, not about the rule it broke.
		if i == 0 {
			if msg := rootKeyShapeError(&node); msg != "" {
				return []string{msg}
			}
		}
		var doc any
		if err := node.Decode(&doc); err != nil {
			return []string{fmt.Sprintf("invalid YAML in document %d: %v", i+1, err)}
		}
		if i > 0 && doc != nil {
			extra++
		}
	}
	if extra > 0 {
		return []string{fmt.Sprintf(
			"YAML has %d document(s) with content after the first — a tenant config "+
				"is a single document; anything after it would be written but never "+
				"validated", extra)}
	}
	return nil
}

// rootKeyShapeError returns the refusal for the first root key of a decoded
// document that is not a plain string, or "" when every root key is one. A
// merge key (`<<` in any form — a mapping, an alias, or a sequence of either)
// is looked for across ALL root keys first, so it always gets its dedicated
// message whatever else the root holds.
func rootKeyShapeError(doc *yaml.Node) string {
	root := doc
	if root.Kind == yaml.DocumentNode {
		if len(root.Content) == 0 {
			return ""
		}
		root = root.Content[0]
	}
	if root.Kind != yaml.MappingNode {
		return ""
	}
	for i := 0; i+1 < len(root.Content); i += 2 {
		if root.Content[i].ShortTag() == "!!merge" {
			return "YAML uses a merge key (<<) at the root — a tenant config " +
				"may only contain a literal top-level 'tenants' block; merge keys are " +
				"allowed inside a tenant section, not at the root"
		}
	}
	for i := 0; i+1 < len(root.Content); i += 2 {
		key := root.Content[i]
		if key.Kind == yaml.ScalarNode && key.ShortTag() == "!!str" {
			continue
		}
		found := key.ShortTag()
		switch key.Kind {
		case yaml.AliasNode:
			found = "alias"
		case yaml.MappingNode, yaml.SequenceNode:
			found = "complex " + found
		}
		return fmt.Sprintf("YAML root keys must be plain strings — found a %s key; "+
			"a tenant config may only contain a top-level 'tenants' block", found)
	}
	return ""
}

// addedTenantKeys returns the sorted `tenants:` keys a body declares that are
// neither the id being written nor already declared by the file this write
// replaces. Separate from validate so the rule has one copy and a direct test.
//
// FAIL CLOSED on every path that yields no baseline — no configDir (the
// unit-test shape), a missing file (a brand-new tenant), an unreadable or
// unparseable one: each arrives here as nil or unparseable baseRaw and leaves
// the baseline empty, so every foreign key counts as added. Only a base file
// that actually parses can grandfather anything.
func addedTenantKeys(baseRaw []byte, tcfg cfg.ThresholdConfig, tenantID string) []string {
	var foreign []string
	for id := range tcfg.Tenants {
		if id != tenantID {
			foreign = append(foreign, id)
		}
	}
	// The baseline only ever REMOVES entries, so a body that declares nothing
	// but its own id has the same answer whatever the base file says. Returning
	// here keeps the ordinary single-tenant write from paying a second full
	// parse of a file that may hold thousands of sections — measured at roughly
	// 2x the whole of validate() on a 10k-section base.
	if len(foreign) == 0 {
		return nil
	}
	// No length guard: Unmarshal of nil succeeds with an empty Tenants map.
	baseline := map[string]struct{}{}
	var base cfg.ThresholdConfig
	if yaml.Unmarshal(baseRaw, &base) == nil {
		for id := range base.Tenants {
			baseline[id] = struct{}{}
		}
	}
	var added []string
	for _, id := range foreign {
		if _, grandfathered := baseline[id]; grandfathered {
			continue
		}
		added = append(added, id)
	}
	sort.Strings(added)
	return added
}

// guardTenantID rejects an id whose {id}.yaml would not be a tenant config
// file. It is the writer-side second enforcement of the same confd predicate
// the handler's ValidateTenantID uses — so no tenant write method can overwrite
// a reserved control file even if a future caller forgets to validate first
// (single-choke-point fragility is the exact bug class this change closes).
//
// It is also the one place every tenant write enforces the tenant-id rule
// (ADR-035, ErrInvalidTenantID). The rule is NOT in confd.IsAddressableTenantID:
// the read paths call that one too, and they keep accepting such ids.
func guardTenantID(tenantID string) error {
	if !confd.IsAddressableTenantID(tenantID) {
		return fmt.Errorf("%w: %q", ErrReservedTenantID, tenantID)
	}
	if !tenantid.Valid(tenantID) {
		return fmt.Errorf("%w %q: %s", ErrInvalidTenantID, tenantID, tenantid.Description)
	}
	return nil
}

// OnWriteFunc is called after a successful config write.
// tenantID is the tenant or entity that was written (tenant ID, "groups", "views", etc.)
type OnWriteFunc func(tenantID string)

// The per-command git timeout/kill-grace/fetch-timeout constants and the
// low-level git command runner (gitCmd / gitExec / gitErr / clearStaleGitLocks)
// live in gitcmd.go.

// Writer handles GitOps write-back operations.
type Writer struct {
	mu             sync.Mutex
	configDir      string        // path to conf.d/ directory (YAML files live here)
	gitDir         string        // git repository root (may differ from configDir)
	committerName  string        // cached from GIT_COMMITTER_NAME env var
	committerEmail string        // cached from GIT_COMMITTER_EMAIL env var
	onWrite        OnWriteFunc   // v2.6.0: callback for post-write notifications (e.g. SSE hub)
	onTreeRelease  func()        // #1988: after EVERY w.mu section (success or error), post-unlock; see lockTree
	gitTimeout     time.Duration // per-git-command wall-clock deadline (#630); 0 → defaultGitTimeout
	gitWaitDelay   time.Duration // cmd.WaitDelay grace after a deadline kill (#630); 0 → defaultGitKillGrace
	gitBinary      string        // git executable; "git" in prod, overridden in tests (timeout seam)
	baseBranch     string        // PR-mode base to branch from / return to (#638); "" → defaultBaseBranch
	fetchTimeout   time.Duration // in-lock base fetch deadline (TRK-318); 0 → defaultGitFetchTimeout

	// #2078 conf.d walk bound (see scanTree): 0 → defaultTreeScanTimeout.
	// stuckTreeScans counts write-path walks that outlived it and are still
	// running; stuckReadTreeScans does the same for the read path (#2153,
	// see scanTreeForRead). Two counters so that a stuck read walk never
	// fails a write.
	treeScanTimeout    time.Duration
	stuckTreeScans     atomic.Int32
	stuckReadTreeScans atomic.Int32

	// readScan is the read path's in-flight walk (#2153), nil when none is
	// running; readScanMu guards it. See scanTreeForRead.
	readScanMu sync.Mutex
	readScan   *treeScanFlight

	// treePrior is the last conf.d walk that COMPLETED and was handed back to
	// its caller (#2153): walkTree passes it to the next cfg.ScanDirTree as
	// the prior, so a file whose stat is unchanged is not read and parsed
	// again. Only walkTree stores it (see scanTree for which walks qualify);
	// atomic because walks run both under w.mu (scanTree) and without it
	// (scanTreeForRead).
	treePrior atomic.Pointer[cfg.TreeScan]

	// onTreeScan is a TEST-ONLY seam (#2153): walkTree calls it, when
	// non-nil, once per walk it starts, on the walking goroutine inside the
	// treeScanTimeout bound — so a test can hold a walk open, or make it
	// outlive the bound, without a FIFO. Per-Writer for the same reason as
	// beforeBaseRestore. Always nil in production.
	onTreeScan func()

	// onReadScanJoin is a TEST-ONLY seam (#2153): scanTreeForRead calls it,
	// when non-nil, each time a caller joins a read walk already in flight
	// instead of starting one. Always nil in production.
	onReadScanJoin func()

	// beforeBaseRestore is a TEST-ONLY seam (#2070): restoreBase calls it, when
	// non-nil, right before each checkoutBaseClean attempt (attempt is 1-based).
	// Per-Writer rather than package-level so parallel tests cannot see each
	// other's hooks. Always nil in production.
	beforeBaseRestore func(attempt int)

	// Load-shedding admission control (TRK-320). Before taking w.mu, every write
	// passes through acquireWrite(ctx): a single execution token (writeExec, cap 1)
	// serialises the one in-flight write, while writeInFlight bounds the total
	// admitted (running + queued) at maxWriteAdmit. Past that → ErrWriteOverloaded
	// (handler → 503). Queueing for the token is ctx-aware, so a client that times
	// out / disconnects WHILE QUEUED is released immediately instead of piling up
	// a goroutine and then running an orphan write once its turn finally comes.
	// nil writeExec (a struct-literal Writer in older tests) disables admission.
	writeExec     chan struct{}
	writeInFlight atomic.Int32
	maxWriteAdmit int32
}

// defaultBaseBranch is the PR-mode base branch when none is configured (#638).
const defaultBaseBranch = "main"

// NewWriter creates a Writer for the given directories.
// configDir is where tenant YAML files live; gitDir is the git repo root.
// If gitDir is empty, configDir is used as the git root.
func NewWriter(configDir, gitDir string) *Writer {
	if gitDir == "" {
		gitDir = configDir
	}
	w := &Writer{
		configDir:      configDir,
		gitDir:         gitDir,
		committerName:  os.Getenv("GIT_COMMITTER_NAME"),
		committerEmail: os.Getenv("GIT_COMMITTER_EMAIL"),
		gitTimeout:     gitTimeoutFromEnv(),
		fetchTimeout:   fetchTimeoutFromEnv(),
		gitBinary:      "git",
		maxWriteAdmit:  1 + writeQueueDepthFromEnv(), // 1 in-flight + N queued
	}
	// Single execution token = single-writer serialisation in front of w.mu, but
	// ctx-aware (TRK-320). Pre-loaded with one token.
	w.writeExec = make(chan struct{}, 1)
	w.writeExec <- struct{}{}
	return w
}

// gitTimeoutFromEnv reads TENANT_API_GIT_TIMEOUT as a Go duration, falling back
// to defaultGitTimeout when unset, unparseable, or non-positive (a clamp keeps a
// fat-fingered "0"/"-5s" from disabling the lock-release safety net).
// gitTimeoutFromEnv / fetchTimeoutFromEnv live in gitcmd.go alongside the git
// command runner whose deadlines they configure.

// SetOnWrite registers a callback to be invoked after a successful config write.
// This is used by v2.6.0 WebSocket/SSE hub to broadcast config change events.
func (w *Writer) SetOnWrite(fn OnWriteFunc) {
	w.onWrite = fn
}

// lockTree / unlockTree bracket every section that may change the conf.d
// tree: the single-writer lock (w.mu), plus the onTreeRelease callback on the
// way out (#1988 D1).
//
// ⛔ WHY ON RELEASE AND NOT ON COMMIT. onWrite fires only after a successful
// commit, but the tree changes earlier and more often than that: a PR-mode
// write checks out a feature branch, writes, pushes and returns to base
// without ever calling onWrite, and a direct write whose commit fails (or
// hits ErrConflict) has already replaced the file. A reader that caches what
// it derived from the tree must drop that cache whenever ANY of these
// sections ends — success, error or panic unwind alike — which a deferred
// unlockTree gives for free.
//
// ⛔ AFTER THE UNLOCK, NOT UNDER IT. The callback takes its reader's own lock;
// calling it while still holding w.mu would order the two locks opposite to a
// reader that holds its lock and then tries the tree lock.
func (w *Writer) lockTree() { w.mu.Lock() }

func (w *Writer) unlockTree() {
	w.mu.Unlock()
	if w.onTreeRelease != nil {
		w.onTreeRelease()
	}
}

// lockTreeOnBase is lockTree for a direct-commit write (#1723): it takes the
// writer lock and, before the caller reads anything from the tree, runs
// leavePRBranch. On error the lock is already released and the caller returns
// the error as is; on nil the caller holds the lock and must defer unlockTree.
//
// ⛔ It must come BEFORE the first read of the tree, not merely before the
// commit. Every read a direct write makes under the lock — which file the
// tenant resolves to, the declared-elsewhere scan, validation's root defaults,
// the base-hash comparison, MutateConfigFile's transform input, WriteMerged's
// merge base — answers for whatever branch is checked out. With the check at
// commit time, a read that REFUSED on a stranded tree (409 declared-elsewhere,
// 409 precondition, a transform error) returned before the guard ran: the
// caller got the branch's answer, and the tree stayed on the branch.
//
// ⚠️ It covers reads under the lock only. A handler that reads the tree before
// calling the Writer (custom-alerts' cheap hash comparison, authorization
// lookups) can still answer from a stranded branch; the next write that
// reaches the Writer moves the tree back.
//
// WritePR / WritePRBatch do not use it: they anchor on base themselves
// (checkoutBaseClean) as their first step under the lock.
func (w *Writer) lockTreeOnBase() error {
	w.lockTree()
	if err := w.leavePRBranch(); err != nil {
		w.unlockTree()
		return err
	}
	return nil
}

// SetOnTreeRelease registers fn to run after every write section releases
// the writer lock, whether the write succeeded or not (see lockTree). Set it
// once at startup, before the server serves.
func (w *Writer) SetOnTreeRelease(fn func()) {
	w.onTreeRelease = fn
}

// TryWithTreeLock runs fn while holding the writer lock and reports true, or
// — when a write section holds the lock — does nothing and reports false. It
// never waits: a reader that must answer promptly cannot queue behind a git
// push that may run until the git timeout.
//
// What it guarantees is only that fn does not run DURING a write section. It
// does not guarantee the tree fn sees is the base branch: a PR write whose
// return to base failed leaves the tree on its feature branch after the lock
// is released (see TreeOnBase). fn may call TreeOnBase and nothing else on
// the Writer.
func (w *Writer) TryWithTreeLock(fn func()) bool {
	if !w.mu.TryLock() {
		return false
	}
	defer w.mu.Unlock()
	fn()
	return true
}

// ErrTreeNotOnBase reports that the git worktree is not on a clean base
// branch — a state a PR-mode write leaves behind when its return to base
// fails. Readers must not treat such a tree as the configuration.
var ErrTreeNotOnBase = errors.New("config worktree is not on the base branch")

// TreeOnBase reports nil when the git worktree is checked out on the base
// branch with no change the conf.d loader would read, ErrTreeNotOnBase
// (wrapped with what was found) otherwise. It is the PR-mode invariant
// between writes; direct mode commits on whatever branch the tree is on and
// has no such invariant. Call it under the tree lock (inside
// TryWithTreeLock); it only reads, so it is also harmless outside it.
//
// What counts as a change: any tracked modification, and an untracked OR
// gitignored file the loader would read (config.IsScannedPath relative to
// configDir — a gitignored *.yaml in conf.d is still read by the loader).
// Other files — a crashed writeFileAtomic's hidden temp file, anything
// outside conf.d — do not change what the loader builds.
//
// Paths: git reports them relative to the repository TOPLEVEL, which need
// not be gitDir, and configDir may be reached through a symlink; both sides
// are resolved (rev-parse --show-toplevel, EvalSymlinks) before comparing.
//
// ⛔ READ-ONLY, INCLUDING LOCKS. Every git call here runs with
// --no-optional-locks and NEVER clears a lock on failure (gitOutput): this is
// a reader, it took no lock, and a lock present in .git belongs to someone
// else. #638's stale-lock sweep is for the writer's own killed commands only.
func (w *Writer) TreeOnBase() error {
	out, err := w.gitOutput(w.gitDir, "symbolic-ref", "--short", "-q", "HEAD")
	if err != nil { // detached HEAD exits non-zero, as does a timeout
		return fmt.Errorf("%w: %v", ErrTreeNotOnBase, err)
	}
	if branch := strings.TrimSpace(string(out)); branch != w.base() {
		return fmt.Errorf("%w: HEAD is %q, base is %q", ErrTreeNotOnBase, branch, w.base())
	}
	out, err = w.gitOutput(w.gitDir, "rev-parse", "--show-toplevel")
	if err != nil {
		return fmt.Errorf("%w: %v", ErrTreeNotOnBase, err)
	}
	top, err := filepath.EvalSymlinks(strings.TrimSpace(string(out)))
	if err != nil {
		return fmt.Errorf("%w: toplevel: %v", ErrTreeNotOnBase, err)
	}
	confAbs, err := filepath.Abs(w.configDir)
	if err == nil {
		confAbs, err = filepath.EvalSymlinks(confAbs)
	}
	if err != nil {
		return fmt.Errorf("%w: config dir: %v", ErrTreeNotOnBase, err)
	}
	out, err = w.gitOutput(top, "status", "--porcelain", "-z", "--untracked-files=all", "--ignored=matching")
	if err != nil {
		return fmt.Errorf("%w: %v", ErrTreeNotOnBase, err)
	}
	for _, entry := range strings.Split(string(out), "\x00") {
		if len(entry) < 4 {
			continue // empty tail
		}
		code, path := entry[:2], entry[3:]
		if code != "??" && code != "!!" {
			return fmt.Errorf("%w: uncommitted change to %s", ErrTreeNotOnBase, path)
		}
		if rel, ok := loaderReads(confAbs, filepath.Join(top, filepath.FromSlash(path))); ok {
			return fmt.Errorf("%w: untracked config file %s", ErrTreeNotOnBase, rel)
		}
	}
	return nil
}

// loaderReads reports whether the conf.d loader rooted at confAbs would read
// abs, or — for a directory entry, which is how git reports an ignored
// directory — any file under it; rel is that file relative to confAbs.
func loaderReads(confAbs, abs string) (string, bool) {
	rel, err := filepath.Rel(confAbs, abs)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		// Outside conf.d — unless conf.d is INSIDE this (directory) entry.
		if inner, ierr := filepath.Rel(abs, confAbs); ierr != nil || strings.HasPrefix(inner, "..") {
			return "", false
		}
		rel = "."
	}
	fi, err := os.Stat(abs)
	if err != nil || !fi.IsDir() {
		return rel, rel != "." && cfg.IsScannedPath(rel)
	}
	var found string
	_ = filepath.WalkDir(abs, func(p string, d fs.DirEntry, werr error) error {
		if werr != nil || d.IsDir() || found != "" {
			return nil
		}
		if r, rerr := filepath.Rel(confAbs, p); rerr == nil && !strings.HasPrefix(r, "..") && cfg.IsScannedPath(r) {
			found = r
			return fs.SkipAll
		}
		return nil
	})
	return found, found != ""
}

// gitOutput runs a READ-ONLY git command in dir and returns its stdout. It
// passes --no-optional-locks (no opportunistic index lock), and on failure —
// a deadline kill included — it reports the error WITHOUT touching any lock:
// see gitReadErr.
func (w *Writer) gitOutput(dir string, args ...string) ([]byte, error) {
	fullArgs := append([]string{"--no-optional-locks", "-C", dir}, args...)
	cmd, ctx, cancel := w.gitCmd(fullArgs...)
	defer cancel()
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return out, w.gitReadErr(ctx, args[0], err, append(out, stderr.Bytes()...))
	}
	return out, nil
}

// Write validates, persists, and commits a tenant's config YAML.
//
// Flow (steps 6–9 are shared with writeSpecialFile via commitFileChange):
//  1. Pre-flight: validateBodyOnly — body-shaped defects only, no tree read
//  2. Admission (single-writer token), then lock the tree and, before any
//     read of it, leave a stranded PR branch (lockTreeOnBase, #1723)
//  3. Resolve the tenant's file, under the lock (#1673 / #2078)
//  4. WriteIfUnchanged only: base-hash precondition on that file
//  5. validate, under the lock — the authoritative verdict and the
//     notices returned (#1681)
//  6. Record HEAD before write
//  7. Write the file resolved in step 3
//  8. git add + git commit --author="<authorEmail>", then check the commit's
//     parent against step 6 (conflict detection)
//  9. onWrite callback (e.g. SSE broadcast)
//
// notices carries the advisory deprecation channel (#1231 1b): non-blocking
// deprecated-key alias advisories from validate(), meaningful ONLY when err is
// nil — handlers surface them in the 200 response so the author sees the
// migration signal on the very write that succeeded with an old spelling.
func (w *Writer) Write(ctx context.Context, tenantID, authorEmail, yamlContent string) (notices []string, err error) {
	return w.write(ctx, tenantID, authorEmail, yamlContent, "")
}

// WriteIfUnchanged is Write with an optimistic-concurrency precondition:
// the tenant file must still hash to baseHash at the moment of the write, or
// nothing is written and *PreconditionError (errors.Is ErrPrecondition) is
// returned.
//
// The check runs INSIDE the writer lock, on the file resolved there and before
// the body is validated and committed, so it is the authoritative one: a
// caller that also compares hashes in its handler is doing a cheap early
// rejection, not concurrency control — that comparison
// reads the file outside the lock, leaving a window in which another write can
// land between the read and the commit (the TOCTOU the custom-alerts handler
// documented as a future hardening).
//
// baseHash is compared against cfg.ComputeSourceHash of the raw on-disk bytes,
// i.e. exactly the `source_hash` GET /tenants/{id} reports. An empty baseHash
// is rejected rather than treated as "no precondition": this method exists to
// enforce one, and silently degrading to an unconditional overwrite when a
// caller's hash variable happens to be empty is the failure mode it is meant to
// prevent. Callers that genuinely want no precondition call Write.
func (w *Writer) WriteIfUnchanged(ctx context.Context, tenantID, authorEmail, yamlContent, baseHash string) (notices []string, err error) {
	if baseHash == "" {
		return nil, &PreconditionError{TenantID: tenantID, Expected: "", Current: ""}
	}
	return w.write(ctx, tenantID, authorEmail, yamlContent, baseHash)
}

// write is the shared body of Write / WriteIfUnchanged. baseHash is empty for
// an unconditional write.
func (w *Writer) write(ctx context.Context, tenantID, authorEmail, yamlContent, baseHash string) (notices []string, err error) {
	// Step 0: reserved-id backstop (defense-in-depth; see guardTenantID).
	if err := guardTenantID(tenantID); err != nil {
		return nil, err
	}
	// Step 1: PRE-FLIGHT, body only — the same validateBodyOnly WritePR's
	// Step 1 runs, and for the same reason.
	//
	// ⛔ NOTHING HERE MAY READ THE TREE. Every write queued ahead of this one
	// may change conf.d before this one runs, so a verdict taken on the tree
	// now is a verdict on a tree the write does not land on, in both
	// directions (#1681):
	//
	//   - BYPASS: a stale addedTenantKeys baseline still grandfathers a
	//     section an earlier write removed, and the write puts it back beside
	//     the id's own file — one id declared by two files.
	//   - OVER-REJECT: a stale #2078 walk (tenantFilePath) or baseline refuses
	//     a write that is legal where it lands — an id whose other declaration
	//     an earlier write is about to remove, or a WriteIfUnchanged whose base
	//     is stale and should hear ErrPrecondition, not a validation verdict.
	//
	// What is left — is this body well-formed? — cannot go stale, and
	// rejecting it here keeps a bad body from consuming the single-writer
	// token. Step 3 is the authoritative run.
	if errs := validateBodyOnly(tenantID, yamlContent); len(errs) > 0 {
		return nil, fmt.Errorf("%w: %s", ErrValidation, strings.Join(errs, "; "))
	}

	// Step 2: load-shedding admission (TRK-320) before w.mu.
	if err := w.acquireWrite(ctx); err != nil {
		return nil, err
	}
	defer w.releaseWrite()

	// #1723: the branch check runs before Step 3 reads the tree — on a stranded
	// PR branch those reads would answer for the branch (lockTreeOnBase).
	if err := w.lockTreeOnBase(); err != nil {
		return nil, err
	}
	defer w.unlockTree()

	// Step 3: AUTHORITATIVE resolve + validate, under the lock, against the
	// tree the commit below lands on. Every in-process write that changes
	// conf.d holds w.mu, which this goroutine now holds until the commit — so
	// no other write can make this verdict stale.
	//
	// #1673: resolve ONCE here and use that path for the base-hash read, for
	// validate's baseline and for the commit, so the three cannot disagree.
	filePath, err := w.tenantFilePath(tenantID)
	if err != nil {
		return nil, err
	}

	// Optimistic concurrency, under the lock and before validating: a caller
	// whose base is stale must hear ErrPrecondition (refresh and retry), not a
	// validation verdict about a file it has not seen.
	if baseHash != "" {
		existing, rerr := os.ReadFile(filePath)
		if rerr != nil && !os.IsNotExist(rerr) {
			return nil, fmt.Errorf("read current tenant file for %s: %w", tenantID, rerr)
		}
		// os.IsNotExist leaves existing nil → current stays empty → mismatch.
		var current string
		if rerr == nil {
			current = cfg.ComputeSourceHash(existing)
		}
		if current != baseHash {
			return nil, &PreconditionError{TenantID: tenantID, Expected: baseHash, Current: current}
		}
	}

	errs, notices := validateReplacing(w.configDir, tenantID, filePath, yamlContent, replaceUnparseableAllowed(ctx))
	if len(errs) > 0 {
		return nil, fmt.Errorf("%w: %s", ErrValidation, strings.Join(errs, "; "))
	}

	if err := w.commitFileChange(
		filePath,
		tenantID,
		authorEmail,
		[]byte(yamlContent),
	); err != nil {
		return nil, err
	}
	return notices, nil
}

// MergeFunc computes a tenant file's full new content from its CURRENT on-disk
// bytes (nil when the file does not exist yet — a brand-new tenant). It is
// invoked while the writer holds w.mu, so the merge base can never go stale
// between the read and the write.
type MergeFunc func(existing []byte) (string, error)

// ErrMergeNoOp is what a MergeFunc returns (errors.Is) when the edit it was
// given changes nothing in the file — e.g. a batch op that only removes keys
// from a tenant that does not exist, or that does not carry them (B2, #2341).
// It is NOT a failure: WriteMerged returns success without writing,
// WritePRBatch skips the op like a byte-identical merge (an all-no-op batch is
// ErrNoChanges), and the pre-flight passes it. Needed because the existing
// byte-compare cannot express "nothing to write" for a file that does not
// exist: any content written there creates the tenant. The existing file is
// still validated for its notices, as a byte-identical merge is (#1231 F5).
var ErrMergeNoOp = errors.New("merge changes nothing")

// ErrMergePolicyRefused is what a MergeFunc's error wraps (errors.Is) when the
// merged content would break the domain policy (B2, #2341). Like
// ErrMergeBaseNotLoadable it is a verdict on the tree it was read from, so
// WritePRBatch's pre-flight (the local tree, before checkout) tolerates it
// and the post-checkout pass — the fresh base the branch is cut from —
// decides: there it aborts the feature branch and returns the error, and
// nothing of the batch is written.
var ErrMergePolicyRefused = errors.New("merged content breaks the domain policy")

// ErrMergeBaseNotLoadable is what a MergeFunc's error wraps (errors.Is) when it
// refuses the EXISTING file as its base because the file cannot be loaded as
// a tenant config (#2373). It is a verdict on the file's content, so like
// confd.ErrAmbiguousTenantFile it is only a verdict on the tree it was read
// from: WritePRBatch's pre-flight reads the local tree before checkout and
// tolerates it (see there); the post-checkout pass decides.
var ErrMergeBaseNotLoadable = errors.New("merge base cannot be loaded as a tenant config")

// ErrMergeFailed wraps (errors.Is) every error a caller's MergeFunc returned,
// as readMerge reports it. A merge error describes the patch against the
// file's content, which the caller is meant to read; the other errors out of
// a write (reading the file, the temp file, git) describe the server and
// carry its paths. The batch handler tells the two apart with this (#1700).
// Its text is the prefix readMerge always used, so messages are unchanged.
var ErrMergeFailed = errors.New("merge tenant config")

// tenantFilePath is the writer-side answer to "which file does this tenant's
// config live in" (#1673). It returns the tenant's EXISTING file whatever its
// spelling, and DefaultTenantFileName only for a tenant that has none — so a
// write to a tenant stored as `<id>.yml` updates that file instead of creating
// a second `<id>.yaml` beside it. An ambiguous tenant (both spellings on disk)
// is refused rather than silently resolved; see confd.ErrAmbiguousTenantFile.
//
// Callers that both READ the existing file and WRITE it back must resolve ONCE
// and pass the path down, so the two halves of one flow cannot disagree.
//
// #2078: the answer must also be the ONLY file the exporter's walker would
// attribute the id to — see ErrTenantDeclaredElsewhere. This is asked on EVERY
// write, the ordinary update included: "`<id>.yaml` exists" does not mean it
// declares the id (a `tenants: {}` shell, a file that does not parse, a file
// declaring some other key), and writing the id into it while another file
// declares it hands the exporter a duplicate. Consequently a tree that ALREADY
// declares the id twice is refused even for an update of `<id>.yaml` — that
// tree is one the exporter rejects anyway, and the fix is removing the other
// declaration, not writing to either one.
//
// Every call walks the tree as it is NOW, so each op of a batch is judged
// after the ops before it have written. (Since #2153 the walk takes the
// previous one as its prior — see scanTree — so an unchanged file is not
// parsed again; the verdict is still the walker's on the current tree.)
//
// This is the WRITE path's resolver, walking with scanTree. The read-only
// callers use previewTenantFilePath.
func (w *Writer) tenantFilePath(tenantID string) (string, error) {
	return w.resolveTenantFilePath(tenantID, w.scanTree)
}

// previewTenantFilePath is tenantFilePath for the callers that only preview a
// write and take no lock — Diff and DryRunValidate: the same resolution and
// the same #2078 guard, but the walk comes from scanTreeForRead, shared with
// concurrent readers and bounded by the read path's own breaker.
//
// ⛔ NEVER FROM A WRITE PATH. Its walk may have started before the caller
// arrived (see scanTreeForRead), which a preview may accept and a write may
// not.
func (w *Writer) previewTenantFilePath(tenantID string) (string, error) {
	return w.resolveTenantFilePath(tenantID, w.scanTreeForRead)
}

// resolveTenantFilePath is the body of tenantFilePath / previewTenantFilePath;
// walk is the conf.d walk the #2078 guard judges.
func (w *Writer) resolveTenantFilePath(tenantID string, walk func() (*cfg.TreeScan, error)) (string, error) {
	path, err := confd.TenantFilePathForWrite(w.configDir, tenantID)
	if err != nil {
		return "", err
	}
	if err := w.ensureNotDeclaredElsewhereIn(walk, tenantID, path); err != nil {
		return "", err
	}
	return path, nil
}

// discardScanLogger swallows the walker's per-file WARNs: this scan is a
// yes/no question asked on a write, and the exporter already reports the same
// tree's unreadable / unparseable files on its own reload loop.
var discardScanLogger = log.New(io.Discard, "", 0)

// defaultTreeScanTimeout bounds one #2078 conf.d walk. The walker reads every
// file, and a file whose read never returns (a FIFO named *.yaml, a hung
// network mount) would otherwise hang the write — inside WriteMerged / the PR
// paths, while holding the single-writer lock, i.e. every later write too.
// Measured on a 1000-file tree a walk takes ~20 ms.
const defaultTreeScanTimeout = 5 * time.Second

// errTreeScanTimeout / errTreeScanStuck / errReadScanAborted are wrapped in
// ErrTenantTreeScan by ensureNotDeclaredElsewhereIn: the guard fails closed
// on all three. They reach the server log only. errReadScanAborted is what a
// read caller gets if the shared walk it waited on ended without a result;
// walkTree recovers the walker's panics, so it is a fail-closed default, not
// an expected outcome.
var (
	errTreeScanTimeout = errors.New("conf.d walk timed out")
	errTreeScanStuck   = errors.New("an earlier conf.d walk is still blocked; restart tenant-api to recover")
	errReadScanAborted = errors.New("the shared conf.d walk ended without a result")
)

// scanTree is the write path's conf.d walk (Write / WriteIfUnchanged,
// WriteMerged, WritePR, WritePRBatch — all under w.mu), bounded by
// treeScanTimeout. A walk that outlives it cannot be cancelled (the walker
// takes no context), so it is left running and counted in stuckTreeScans
// until it returns; while the count is non-zero, later calls fail at once.
// The mechanism is boundedcall.Run, shared with the tenant GET's bounded
// read of the root platform files (#2208).
//
// ⚠️ That bounds new walks only AFTER the first timeout. The write path's
// walks are serialised by w.mu, so it has at most one walk running. The
// read-only callers (Diff, DryRunValidate) take no lock and do NOT come
// here: they walk through scanTreeForRead, which shares one in-flight walk
// between them and counts its stuck walks in stuckReadTreeScans — so a walk
// a reader left blocked fails later reads only, never a write. A blocked
// walk ends only when the file it is reading is opened for writing or the
// process restarts: removing the file does not unblock a read already in
// progress.
//
// #2153: each walk takes treePrior as its prior — the exporter's own mtime
// fast-path, under ScanDirTree's contract unchanged: a file whose stat
// matches the prior's and is older than cfg.TreeScanMtimeGuard carries the
// prior's hash and tenant declarations unread; anything else (moved stat,
// young file, prior parse failure, no prior entry) is read, and parsed
// unless its hash equals the prior's. The walk is therefore never a cache
// with its own invalidation rule — this Writer's own writes and the PR
// paths' checkouts change a file's stat, and the walker judges that.
// ⚠️ It inherits that contract's known gap: if the prior recorded a file
// while it was still inside the guard, and the file is then rewritten with
// the same size within the same mtime tick, a walk made after the guard has
// passed carries the prior's declarations for the old bytes.
//
// ⛔ ONLY A WALK HANDED BACK TO ITS CALLER BECOMES THE PRIOR. The store
// happens in the walking goroutine, under the same mutex that decides
// abandonment, and only when the walk succeeded and was not abandoned: a
// walk that errored, or that returns after its caller already timed out and
// failed closed, never replaces the prior. (A walk that ends with a
// duplicate-tenant Conflict is a completed walk — its files are exactly what
// the next walk's fast-path reads — so it does qualify.) The store is
// last-writer-wins between concurrent callers; any completed walk is a valid
// prior, an older one only costs reads. The rule is the same on both paths:
// a read walk (scanTreeForRead) that completes in time becomes the prior
// too, and a read walk shared by several callers is one walk, stored once.
//
// ⛔ The prior is published only after ReleaseData, as ScanDirTree's
// contract asks of a retained scan: the fast-path reads Hash, Stat,
// TenantIDs and ParseFailed, and keeping every file's bytes and decoded
// config alive until the next walk would be a retention nobody asked for.
// No caller of scanTree reads TreeFile.Data or Partials. Once published the
// scan is never mutated again, so concurrent walks may read it as a prior.
func (w *Writer) scanTree() (*cfg.TreeScan, error) {
	return w.walkTree(&w.stuckTreeScans)
}

// treeScanFlight is one read-path walk and the callers waiting on it (see
// scanTreeForRead). scan and err are set once, before done is closed.
type treeScanFlight struct {
	done chan struct{}
	scan *cfg.TreeScan
	err  error
}

// scanTreeForRead is the read path's conf.d walk, for the callers that only
// preview a write and take no lock (Diff, DryRunValidate — via
// previewTenantFilePath). It is scanTree's bounded walk with two differences
// (#2153):
//
//   - ONE WALK IN FLIGHT. A caller that arrives while a read walk is running
//     does not start its own: it waits for that walk and gets its result —
//     the same scan, or the same error (a timeout included), so it waits no
//     longer than the walk's own bound. Without the writer lock nothing
//     else serialises these callers, so without this each would start a
//     walk of its own, and each could block on the same file before the
//     first timeout trips the breaker.
//   - ITS OWN BREAKER. The walk counts in stuckReadTreeScans, not
//     stuckTreeScans: a read walk left blocked fails later reads at once
//     (errTreeScanStuck, which ensureNotDeclaredElsewhereIn wraps in
//     ErrTenantTreeScan) until it returns, and never fails a write.
//
// ⚠️ A caller may get a walk that started BEFORE it arrived, so a write that
// landed in between may be missing from the verdict. That is acceptable for
// a preview only: the write itself is judged again, under w.mu, on a walk of
// its own (scanTree). ⛔ Which is why the write path must never join this
// walk — a walk started before the previous write landed would judge the
// next one against a tree that is already gone.
//
// The prior contract is walkTree's, unchanged: the shared walk commits once,
// and only if it completed in time.
func (w *Writer) scanTreeForRead() (*cfg.TreeScan, error) {
	w.readScanMu.Lock()
	if f := w.readScan; f != nil {
		w.readScanMu.Unlock()
		if w.onReadScanJoin != nil {
			w.onReadScanJoin()
		}
		<-f.done
		return f.scan, f.err
	}
	f := &treeScanFlight{done: make(chan struct{}), err: errReadScanAborted}
	w.readScan = f
	w.readScanMu.Unlock()
	// Clear before close: a caller that arrives after the walk ended starts
	// a new one rather than taking this finished result.
	defer func() {
		w.readScanMu.Lock()
		w.readScan = nil
		w.readScanMu.Unlock()
		close(f.done)
	}()
	f.scan, f.err = w.walkTree(&w.stuckReadTreeScans)
	return f.scan, f.err
}

// walkTree is the bounded walk behind scanTree and scanTreeForRead; stuck is
// the breaker of the path it walks for.
func (w *Writer) walkTree(stuck *atomic.Int32) (*cfg.TreeScan, error) {
	return w.walkTreeFrom(stuck, true)
}

// walkTreeFrom is walkTree; withPrior false walks cold (no prior: every file
// read and hashed), for a caller that found the prior's carried hash no
// longer matches a file's bytes (ResolveEffective). Either walk is
// published as the next prior under the same rule.
func (w *Writer) walkTreeFrom(stuck *atomic.Int32, withPrior bool) (*cfg.TreeScan, error) {
	if stuck.Load() > 0 {
		return nil, errTreeScanStuck
	}
	timeout := w.treeScanTimeout
	if timeout <= 0 {
		timeout = defaultTreeScanTimeout
	}
	type result struct {
		scan *cfg.TreeScan
		err  error
	}
	var prior *cfg.TreeScan
	if withPrior {
		prior = w.treePrior.Load()
	}
	// boundedcall.RunCommit keeps #2153's publication rule: the commit runs
	// on the walking goroutine under the same mutex that decides abandonment,
	// and only for a walk handed back to its caller (returned, not panicked,
	// not abandoned) — so an errored, panicked or late walk never becomes the
	// prior.
	r, err := boundedcall.RunCommit(timeout, stuck, func() result {
		if w.onTreeScan != nil {
			w.onTreeScan()
		}
		// obs is a literal nil interface on purpose — see cfg.ScanObserver's
		// typed-nil trap.
		s, err := cfg.ScanDirTree(w.configDir, prior, nil, discardScanLogger)
		if err == nil {
			s.ReleaseData() // before anyone else can see s
		}
		return result{s, err}
	}, func(r result) {
		if r.err == nil {
			w.treePrior.Store(r.scan)
		}
	})
	switch {
	case errors.Is(err, boundedcall.ErrStuck):
		return nil, errTreeScanStuck
	case errors.Is(err, boundedcall.ErrTimedOut):
		return nil, fmt.Errorf("%w after %v", errTreeScanTimeout, timeout)
	case err != nil: // boundedcall.ErrPanicked: the guard fails closed on it too
		return nil, err
	}
	return r.scan, r.err
}

// ensureNotDeclaredElsewhere is the #2078 guard: target may be written only if
// the exporter's own walker — the one whose duplicate verdict makes the
// exporter refuse the tree — attributes tenantID to no file, or to target.
//
// Same walker, not a re-implementation: which files count (extensions, hidden
// entries, `_` prefixes, symlinked roots) and what counts as a declaration (a
// file the full decode accepts) cannot drift from what the exporter loads.
func (w *Writer) ensureNotDeclaredElsewhere(tenantID, target string) error {
	return w.ensureNotDeclaredElsewhereIn(w.scanTree, tenantID, target)
}

// ensureNotDeclaredElsewhereIn is ensureNotDeclaredElsewhere judged on the
// walk that walk returns — scanTree on the write path, scanTreeForRead on
// the preview path (see previewTenantFilePath).
func (w *Writer) ensureNotDeclaredElsewhereIn(walk func() (*cfg.TreeScan, error), tenantID, target string) error {
	scan, err := walk()
	if err != nil {
		return fmt.Errorf("%w: tenant %s: %w", ErrTenantTreeScan, tenantID, err)
	}
	located, lerr := scan.Locate(tenantID)
	var dup *cfg.DuplicateTenantError
	switch {
	case errors.Is(lerr, cfg.ErrTenantNotFound):
		return nil
	case errors.As(lerr, &dup):
		return fmt.Errorf("%w: tenant %s: %w", ErrTenantDeclaredElsewhere, tenantID, lerr)
	case lerr != nil:
		return fmt.Errorf("%w: tenant %s: %w", ErrTenantTreeScan, tenantID, lerr)
	}
	// Locate answers under scan.AbsRoot — absolute, and with a symlinked
	// configDir already resolved — while target is configDir-relative in
	// whatever form the operator passed. Compare in the walker's form: the
	// target is always a top-level file, so its walker path is AbsRoot/<base>.
	if filepath.Clean(located) == filepath.Join(scan.AbsRoot, filepath.Base(target)) {
		return nil
	}
	return fmt.Errorf("%w: tenant %s is declared by %s", ErrTenantDeclaredElsewhere, tenantID, located)
}

// readMerge is the half of readMergeValidate that produces the content: read
// the current file, hand it to the caller's merge. Split out so the pre-flight
// and the authoritative pass can run the SAME read+merge under DIFFERENT
// validators (#1718) without a second copy of either step.
func (w *Writer) readMerge(tenantID, filePath string, merge MergeFunc) (content string, existing []byte, err error) {
	existing, rerr := os.ReadFile(filePath)
	if rerr != nil && !os.IsNotExist(rerr) {
		return "", nil, fmt.Errorf("read current tenant file for %s: %w", tenantID, rerr)
	}
	content, merr := merge(existing)
	if merr != nil {
		return "", existing, fmt.Errorf("%w for %s: %w", ErrMergeFailed, tenantID, merr)
	}
	return content, existing, nil
}

// readMergeBodyOnly is the PRE-FLIGHT form: same read+merge, but the merged
// content is judged by validateBodyOnly.
//
// ⚠️ THE MERGE BASE IS STILL THE CALLER'S CURRENT TREE, and that is fine here
// precisely because nothing is decided on it: the authoritative pass re-reads,
// re-merges and re-validates against the checked-out base. What this must not
// do is REFUSE on tree-derived evidence, which is what validateBodyOnly rules
// out structurally (it takes no path and no configDir).
func (w *Writer) readMergeBodyOnly(tenantID, filePath string, merge MergeFunc) error {
	content, _, err := w.readMerge(tenantID, filePath, merge)
	if errors.Is(err, ErrMergeNoOp) {
		return nil // nothing to write, nothing to validate
	}
	if err != nil {
		return err
	}
	if errs := validateBodyOnly(tenantID, content); len(errs) > 0 {
		return fmt.Errorf("%w for %s: %s", ErrValidation, tenantID, strings.Join(errs, "; "))
	}
	return nil
}

// readMergeValidate is the shared read-merge-validate core behind both the
// direct (WriteMerged) and PR-mode (WritePRBatch) partial-write paths (#1097).
// It reads the current on-disk tenant file, runs merge against it, and runs the
// same schema/custom-alert/eol validator every write boundary uses. It does NOT
// persist — the caller decides how (commit-on-write vs branch commit).
//
// existing is nil on ENOENT; MergeFunc is responsible for the new-tenant case.
// A merge error means the on-disk file is unparseable/structurally wrong — the
// caller must NOT fall back to an overwrite (that is exactly the silent
// key-loss this path exists to prevent). The raw existing bytes are returned so
// the caller can detect a byte-identical (no-op) merge. notices is validate()'s
// advisory deprecation channel (#1231 1b), meaningful only when err is nil.
func (w *Writer) readMergeValidate(tenantID, filePath string, merge MergeFunc) (content string, existing []byte, notices []string, err error) {
	content, existing, err = w.readMerge(tenantID, filePath, merge)
	if errors.Is(err, ErrMergeNoOp) && len(bytes.TrimSpace(existing)) > 0 {
		// The file stays as it is: nothing is written, so nothing is refused
		// for what the file already holds (B2 round 3 — a file with no
		// section for this tenant, or a key validate rejects, used to turn
		// a successful no-op into a 400). Only the file's notices are taken
		// from validate (#1231 F5); its errors are deliberately dropped. The
		// caller sees ErrMergeNoOp with the notices.
		_, notices := validate(w.configDir, tenantID, filePath, string(existing))
		return "", existing, notices, err
	}
	if err != nil {
		return "", existing, nil, err
	}
	errs, notices := validate(w.configDir, tenantID, filePath, content)
	if len(errs) > 0 {
		return "", existing, nil, fmt.Errorf("%w for %s: %s", ErrValidation, tenantID, strings.Join(errs, "; "))
	}
	return content, existing, notices, nil
}

// WriteMerged persists a tenant config whose content is computed, UNDER the
// single-writer lock, from the current on-disk file. This is the race-free
// read-merge-write the batch patch path needs (#1097): a partial patch must
// preserve keys it did not name, and reading the merge base OUTSIDE the lock
// would let a concurrent same-tenant write be silently lost (the in-process
// conflict detector only catches EXTERNAL commits, not serialized in-process
// writes onto a stale base).
//
// Validation runs only under the lock — unlike Write(), there is no
// pre-flight, because the final content is not known until the base is read.
//
// notices is validate()'s advisory deprecation channel (#1231 1b), meaningful
// only when err is nil — note it is populated even on the no-op short-circuit
// below (the merged body still carries the deprecated spelling; the author
// should hear about it whether or not this particular patch changed bytes).
func (w *Writer) WriteMerged(ctx context.Context, tenantID, authorEmail string, merge MergeFunc) (notices []string, err error) {
	// Reserved-id backstop (defense-in-depth; see guardTenantID).
	if err := guardTenantID(tenantID); err != nil {
		return nil, err
	}
	// Load-shedding admission (TRK-320) before w.mu, same as Write.
	if err := w.acquireWrite(ctx); err != nil {
		return nil, err
	}
	defer w.releaseWrite()

	if err := w.lockTreeOnBase(); err != nil { // #1723: before the first read
		return nil, err
	}
	defer w.unlockTree()

	// #1673: one resolution for the whole flow — the file we read below and the
	// file we commit at the end must be the same one.
	filePath, err := w.tenantFilePath(tenantID)
	if err != nil {
		return nil, err
	}
	content, existing, notices, err := w.readMergeValidate(tenantID, filePath, merge)
	if errors.Is(err, ErrMergeNoOp) {
		return notices, nil // B2: the merge changes nothing — success, nothing written, notices kept
	}
	if err != nil {
		return nil, err
	}
	// No-op short-circuit (mirrors MutateConfigFile's `if next == nil`): when the
	// merge changed nothing — an idempotent patch, or a client retry after a
	// write whose response was lost — the content is byte-identical to disk, so
	// gitCommit would stage nothing and NOT advance HEAD. commitFileChange's
	// parent-based conflict check then misfires (HEAD~1 != unmoved HEAD) and
	// returns a spurious, permanently-unrecoverable ErrConflict. Treat an
	// unchanged merge as success (#1097 self-review). `existing == nil` (a new
	// tenant) never matches non-empty content, so it still commits the new file.
	if existing != nil && content == string(existing) {
		return notices, nil
	}
	if err := w.commitFileChange(
		filePath,
		tenantID,
		authorEmail,
		[]byte(content),
	); err != nil {
		return nil, err
	}
	return notices, nil
}

// Diff returns the unified diff between the current file and proposed content.
// Returns empty string if files are identical or no current file exists.
//
// It takes no lock, so it resolves the file through previewTenantFilePath:
// its #2078 walk is shared with concurrent previews (see scanTreeForRead).
func (w *Writer) Diff(tenantID, proposedContent string) (string, error) {
	current, exists, err := w.PreviewCurrent(tenantID)
	if err != nil {
		return "", err
	}
	return w.DiffTexts(tenantID, string(current), exists, proposedContent)
}

// PreviewCurrent returns the tenant's current file content for a preview, and
// whether the file exists. It resolves the file as Diff does (no lock, the
// shared #2078 walk), so its errors are Diff's.
func (w *Writer) PreviewCurrent(tenantID string) ([]byte, bool, error) {
	filePath, err := w.previewTenantFilePath(tenantID)
	if err != nil {
		return nil, false, err
	}
	existing, err := os.ReadFile(filePath)
	if os.IsNotExist(err) {
		return nil, false, nil
	}
	if err != nil {
		return nil, false, fmt.Errorf("read existing: %w", err)
	}
	return existing, true, nil
}

// DiffTexts is the unified diff from current to proposed — Diff without the
// file read, so a caller can diff two texts it prepared itself (the masked
// preview, #1560). exists=false renders proposed as a new file. Equal texts
// give "".
//
// The diff runs on two temporary files named current/<id>.yaml and
// proposed/<id>.yaml in a private directory, from inside it, so the header
// names those relative paths: no conf.d or temp-dir path reaches the caller.
func (w *Writer) DiffTexts(tenantID, current string, exists bool, proposed string) (string, error) {
	if !exists {
		// New file — show the entire proposed content as an addition
		var lines []string
		for _, line := range strings.Split(proposed, "\n") {
			lines = append(lines, "+"+line)
		}
		return strings.Join(lines, "\n"), nil
	}
	if current == proposed {
		return "", nil
	}

	dir, err := os.MkdirTemp("", "tenant-api-diff-*")
	if err != nil {
		return "", fmt.Errorf("create temp dir: %w", err)
	}
	defer func() { _ = os.RemoveAll(dir) }()
	name := tenantID + ".yaml"
	for sub, text := range map[string]string{"current": current, "proposed": proposed} {
		if err := os.Mkdir(filepath.Join(dir, sub), 0o700); err != nil {
			return "", fmt.Errorf("create temp dir: %w", err)
		}
		if err := os.WriteFile(filepath.Join(dir, sub, name), []byte(text), 0o600); err != nil {
			return "", fmt.Errorf("write temp file: %w", err)
		}
	}

	cmd, _, cancel := w.gitCmd("diff", "--no-index", "--", "current/"+name, "proposed/"+name)
	defer cancel()
	cmd.Dir = dir
	// git diff exits 1 when there are differences — that's expected, so the error
	// is intentionally discarded. A deadline-killed diff likewise returns empty
	// output here (this is a read-only advisory diff, not a write path).
	out, _ := cmd.Output()
	return string(out), nil
}

// The special-file write paths (WriteGroupsFile / WriteViewsFile /
// WriteFederationPolicyFile / WriteFederationSubsetFile and their shared
// writeSpecialFile helper) live in writer_special.go.

// writeFileAtomic replaces filePath with content via a temp file in the
// same directory plus os.Rename, mirroring federation/token.store.save.
//
// Why not os.WriteFile: it truncates in place, so anything reading the
// file concurrently can observe a half-written, unparseable document.
// That window is reachable in-process — a config manager's Reload()
// runs OUTSIDE w.mu, so it can read _groups.yaml while another request
// holds the lock mid-write — and out-of-process, since the
// threshold-exporter's Directory Scanner reads conf.d on its own clock.
// A torn read surfaces as "parse error" and is exactly the mid-flight
// reload failure the hot-reload managers keep serving last-good for.
//
// The temp name must not end in .yaml: conf.d/*.yaml is globbed as the
// tenant set, and a transiently-visible temp would register as a bogus
// tenant. It also gets perm explicitly — os.CreateTemp makes 0600 and
// the exporter reads these files as a different UID.
//
// No fsync: the goal here is atomicity against concurrent readers, not
// crash durability. The commit that immediately follows is what makes
// the change durable, and git restores the worktree from it.
//
// Atomicity of the swap is a POSIX rename(2) guarantee — the runtime is
// a Linux container. On Windows os.Rename onto a path another handle has
// open fails with a sharing violation instead; that only affects
// dev-host tooling, never the deployed service.
func writeFileAtomic(filePath string, content []byte, perm os.FileMode) error {
	tmp, err := os.CreateTemp(filepath.Dir(filePath), filepath.Base(filePath)+".*.tmp")
	if err != nil {
		return fmt.Errorf("create temp file: %w", err)
	}
	tmpName := tmp.Name()
	// No-op once the rename succeeds; cleans up on every error path.
	defer func() { _ = os.Remove(tmpName) }()

	if _, err := tmp.Write(content); err != nil {
		_ = tmp.Close()
		return fmt.Errorf("write temp file: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return fmt.Errorf("close temp file: %w", err)
	}
	if err := os.Chmod(tmpName, perm); err != nil {
		return fmt.Errorf("chmod temp file: %w", err)
	}
	if err := os.Rename(tmpName, filePath); err != nil {
		return fmt.Errorf("rename temp file: %w", err)
	}
	return nil
}

// commitFileChange is the shared write+commit+conflict-detect+notify
// flow used by both Write (tenant YAML) and writeSpecialFile
// (_groups.yaml / _views.yaml). Caller MUST hold w.mu before calling.
//
// `commitTag` identifies what's being committed in log lines, the
// commit message subject (via gitCommit), and the onWrite callback
// argument. For tenant writes it's the tenant ID; for special files
// it's the entity type ("groups" / "views").
//
// Returns ErrConflict if the recorded HEAD before the write differs
// from our commit's parent (someone else pushed between our read and
// our write). Non-git environments skip conflict detection but still
// return commit errors verbatim.
//
// Callers take the lock with lockTreeOnBase (#1723), which is where the
// branch check that matters runs — before the caller's first read.
func (w *Writer) commitFileChange(filePath, commitTag, authorEmail string, content []byte, trailer ...string) error {
	// #1723 backstop only. Every current caller already ran this check in
	// lockTreeOnBase, so here it is one symbolic-ref that finds base. It
	// stays so a future caller that takes plain lockTree still cannot commit
	// onto a stranded PR branch — though such a caller's reads would already
	// have answered for that branch; use lockTreeOnBase.
	if err := w.leavePRBranch(); err != nil {
		return err
	}
	headBefore, err := w.currentHEAD()
	if err != nil {
		// Proceed without conflict detection in non-git environments.
		slog.Warn("gitops: could not read HEAD before write",
			"commit_tag", commitTag, "error", err)
	}

	if err := writeFileAtomic(filePath, content, 0644); err != nil {
		return fmt.Errorf("write file: %w", err)
	}

	committed, err := w.gitCommit(filePath, commitTag, authorEmail, trailer...)
	if err != nil {
		slog.Warn("gitops: commit failed", "commit_tag", commitTag, "error", err)
		return fmt.Errorf("git commit: %w", err)
	}

	// Nothing was staged — the file on disk already matched HEAD, so the
	// caller's desired state is what the repository holds and no commit was
	// created. The parent check below only makes sense when we DID commit:
	// with HEAD unmoved, HEAD~1 is our own predecessor rather than the commit
	// we branched from, so it never equals headBefore and would report a
	// permanent, unrecoverable ErrConflict for what is really an idempotent
	// re-write (the same misfire WriteMerged's no-op short-circuit documents
	// and sidesteps — this closes it for every direct-commit caller).
	if !committed {
		slog.Info("gitops: no changes to commit", "commit_tag", commitTag)
		return nil
	}

	if headBefore != "" {
		parent, err := w.commitParent()
		if err == nil && parent != headBefore {
			slog.Warn("gitops: external commit detected",
				"commit_tag", commitTag,
				"expected_parent", headBefore[:8],
				"actual_parent", parent[:8])
			return ErrConflict
		}
	}

	slog.Info("gitops: committed", "commit_tag", commitTag, "author", authorEmail)

	// v2.6.0: Notify via callback (e.g. SSE hub broadcast).
	if w.onWrite != nil {
		w.onWrite(commitTag)
	}

	return nil
}

// revParse resolves a git rev (e.g. "HEAD", "HEAD~1") to its commit hash.
func (w *Writer) revParse(ref string) (string, error) {
	cmd, ctx, cancel := w.gitCmd("-C", w.gitDir, "rev-parse", ref)
	defer cancel()
	out, err := cmd.Output()
	if err != nil {
		return "", w.gitErr(ctx, "rev-parse "+ref, err, out)
	}
	return strings.TrimSpace(string(out)), nil
}

// currentHEAD returns the current HEAD commit hash of the git repository.
func (w *Writer) currentHEAD() (string, error) {
	return w.revParse("HEAD")
}

// commitParent returns the parent commit hash of HEAD (i.e. HEAD~1).
func (w *Writer) commitParent() (string, error) {
	return w.revParse("HEAD~1")
}

// gitCommit stages filePath and creates a commit with the operator's email as author.
//
// committed reports whether a commit was actually created: staging a file whose
// content already equals HEAD's leaves nothing to commit, which is a success
// (the desired state is already in the repository) but leaves HEAD where it
// was. Callers that reason about HEAD movement must branch on this rather than
// assume a nil error means a new commit exists.
//
// Committer identity is sourced from the GIT_COMMITTER_NAME / GIT_COMMITTER_EMAIL
// environment variables (set in the K8s Deployment). This keeps the audit trail clean:
//   - author  = the human operator (from X-Forwarded-Email via oauth2-proxy)
//   - committer = the service account (da-portal@dynamic-alerting.local)
func (w *Writer) gitCommit(filePath, tenantID, authorEmail string, trailer ...string) (committed bool, err error) {
	// Stage the file
	addCmd, addCtx, addCancel := w.gitCmd("-C", w.gitDir, "add", filePath)
	defer addCancel()
	if out, err := addCmd.CombinedOutput(); err != nil {
		return false, w.gitErr(addCtx, "add", err, out)
	}

	// Check if there's actually something to commit
	statusCmd, _, statusCancel := w.gitCmd("-C", w.gitDir, "diff", "--cached", "--quiet")
	defer statusCancel()
	if err := statusCmd.Run(); err == nil {
		// Exit 0 means no changes staged — nothing to commit
		return false, nil
	}

	msg := fmt.Sprintf("tenant/%s: update via portal\n\nTimestamp: %s\nSource: da-portal/tenant-manager",
		tenantID, time.Now().UTC().Format(time.RFC3339))
	// Optional trailer — appended to the message body so an audit
	// annotation (e.g. an admission-validator --force bypass) is
	// permanently bound to the commit, not just an ephemeral log line.
	if len(trailer) > 0 && trailer[0] != "" {
		msg += "\n\n" + trailer[0]
	}

	// author name defaults to email prefix when no display name is available
	authorName := authorEmail
	if at := strings.Index(authorEmail, "@"); at > 0 {
		authorName = authorEmail[:at]
	}
	author := fmt.Sprintf("%s <%s>", authorName, authorEmail)

	// Committer identity: cached from env vars injected by K8s Deployment.
	// Fall back to author identity if not set (dev/local mode).
	committerName := w.committerName
	committerEmail := w.committerEmail
	if committerName == "" {
		committerName = authorName
	}
	if committerEmail == "" {
		committerEmail = authorEmail
	}

	commitCmd, commitCtx, commitCancel := w.gitCmd("-C", w.gitDir,
		"-c", "user.name="+committerName,
		"-c", "user.email="+committerEmail,
		"commit",
		"--author="+author,
		"-m", msg,
	)
	defer commitCancel()
	if out, err := commitCmd.CombinedOutput(); err != nil {
		return false, w.gitErr(commitCtx, "commit", err, out)
	}
	return true, nil
}

// PR-mode write-back (PRWriteResult / WritePR / WritePRBatch / PRBatchOp) lives
// in writer_pr.go.

// defaultMaxTenantDocBytes is the pre-parse ceiling on ONE tenant document
// (#1722). Both sides of the choice are derived by tests rather than restated
// here as numbers: TestRealTenantFilesAreNowhereNearTheCap walks the repo for the
// largest real tenant config and asserts a two-sided band around this constant,
// and TestCapClearsTheRecipeCeiling projects a full cfg.MaxCustomRecipesDefault
// body from the densest real file. For parse cost in seconds, run
// BenchmarkValidateAtCap on the hardware you care about.
//
// Override with TA_MAX_TENANT_DOC_BYTES. Deliberately SEPARATE from the handler's
// TA_MAX_BODY_BYTES: that bounds what a request may transfer, this bounds what
// the write path parses while holding the lock — and on the merged paths those
// are not the same document.
const defaultMaxTenantDocBytes int64 = 64 << 10

// maxTenantDocBytes is resolved once at package init. CheckTenantDocSize is on
// the hot path of every write and takes no receiver, so the env read cannot hang
// off a Writer; a package var keeps the check to one comparison.
//
// ⛔ IT DOES NOT WARN FROM HERE. Package-var initialisation runs before main's
// configureLogger, so a slog.Warn at this point is emitted by the DEFAULT text
// handler and ignores TA_LOG_LEVEL — i.e. a fat-fingered value would be
// invisible to the platform's JSON log pipeline, which is the one place an
// operator would look. The malformed flag rides out through
// TenantDocBytesFromEnv instead, and main logs it alongside the sibling knobs.
var maxTenantDocBytes, _ = TenantDocBytesFromEnv(os.Getenv("TA_MAX_TENANT_DOC_BYTES"))

// MaxTenantDocBytes reports the resolved per-document ceiling, for callers that
// need to describe it (the dry-run endpoint's error text, startup logging).
func MaxTenantDocBytes() int64 { return maxTenantDocBytes }

// DefaultTenantDocBytes reports the COMPILED-IN default, ignoring the env
// override.
//
// ⛔ THE DISTINCTION IS LOAD-BEARING FOR THE DRIFT GUARDS. They compare what
// helm and the README document against what the binary ships, and the shipped
// value is the default — not whatever TA_MAX_TENANT_DOC_BYTES happens to be in
// the shell running the tests. Comparing against MaxTenantDocBytes() made any
// developer or CI job that exercised the override fail with a message asserting
// "the code's default is N", which was not the default at all.
func DefaultTenantDocBytes() int64 { return defaultMaxTenantDocBytes }

// TenantDocBytesFromEnv parses TA_MAX_TENANT_DOC_BYTES as a positive byte count,
// falling back to defaultMaxTenantDocBytes when unset, unparseable, or
// non-positive, and reporting malformed so the CALLER can warn once the logger
// exists. Mirrors handler.MaxBodyBytesFromEnv, including its reasons: a
// full-string parse (a numeric PREFIX like "65536x" silently meant a wrong cap
// in #795 F4) and treating "0" as malformed rather than honouring it, since a
// zero cap would reject every write and is always a fat-finger.
func TenantDocBytesFromEnv(envValue string) (n int64, malformed bool) {
	v := strings.TrimSpace(envValue)
	if v == "" {
		return defaultMaxTenantDocBytes, false
	}
	parsed, err := strconv.ParseInt(v, 10, 64)
	if err != nil || parsed <= 0 {
		return defaultMaxTenantDocBytes, true
	}
	return parsed, false
}

// CheckTenantDocSize is the pre-parse size gate on one tenant document (#1722).
//
// It was exported (#1722) because POST /tenants/{id}/validate then re-assembled
// validate()'s checks by hand and had to call this gate itself. Since #2124 the
// dry-run gets its verdict from DryRunValidate, which runs validate() — and so
// this gate — itself; the handler no longer calls it. It stays exported for the
// handler-side symmetry test that measures a fixture against the cap.
//
// ⚠️ WHAT IT MEASURES DEPENDS ON THE CALLER, so the message must not assert one.
// The merged document reaches it from exactly two places — readMergeBodyOnly and
// readMergeValidate, i.e. the PATCH paths. Every other arrival carries the
// caller's own bytes: Write / WriteIfUnchanged, BOTH of WritePR's validations
// (it takes a yamlContent string, never a MergeFunc), and the dry-run endpoint.
func CheckTenantDocSize(yamlContent string) []string {
	n := int64(len(yamlContent))
	if n <= maxTenantDocBytes {
		return nil
	}
	return []string{fmt.Sprintf(
		"tenant document is %d bytes, over the %d-byte limit — this measures the "+
			"document being validated. On a PATCH (the batch endpoints) that is "+
			"the WHOLE merged file rather than the keys you sent, so a small patch "+
			"onto a large shared conf.d file lands here too; on a full-body write "+
			"or a dry-run it is exactly what you sent. Parsing cost grows faster "+
			"than size and this runs while the single write lock is held, so an "+
			"oversize one delays every other tenant's write. Split the file or "+
			"raise TA_MAX_TENANT_DOC_BYTES",
		n, maxTenantDocBytes)}
}

// validateShape runs every check that reads ONLY the request body and the URL
// id, in the order validate has always run them, and hands back the parsed
// config so no caller decodes the same bytes twice.
//
// ⛔ THE SPLIT LINE IS "DOES IT READ THE WORKING TREE", NOT "DOES IT SOUND
// STATIC" (#1718). Three of validate's checks read the tree and are therefore
// only meaningful against the tree the write lands on:
//
//   - addedTenantKeys      ← os.ReadFile(tenantFilePath)
//   - ValidateTenantKeys   ← mergeTenantConfig reads <configDir>'s root defaults
//     carrier (the one the exporter's chain selects, #1674) and the root
//     platform files' per-tenant `tenants:` entries (#2208)
//   - the eol-expansion guard ← the same baseRaw as addedTenantKeys
//
// The middle one is the trap: "key validation" reads like a pure body check and
// is not — it merges the platform defaults off disk. Both it and addedTenantKeys
// were MEASURED to reject legitimate writes when the caller's tree is stale
// (#1718); classifying by name rather than by what the code reads would have
// left the second one behind.
//
// Everything here short-circuits, exactly as before — the first failure is the
// only one reported.
func validateShape(tenantID, yamlContent string) (cfg.ThresholdConfig, []string) {
	var tcfg cfg.ThresholdConfig
	// PRE-PARSE SIZE GATE (#1722). Must be the FIRST thing here and must come
	// before yaml.Unmarshal, because the cost it bounds is the parse itself:
	// yaml.v3 is superlinear in the number of keys in ONE mapping, and every
	// caller below parses yamlContent four times (Unmarshal here,
	// FindDuplicateKeyIn, CheckTenantRootKeys, yamlDocumentShapeErrors).
	//
	// ⛔ WHY THIS IS NOT MERELY A NICE-TO-HAVE. Since #1718 the authoritative
	// validate() runs INSIDE the single-writer token, so this cost is no longer
	// paid by the sender alone — it is head-of-line blocking for every other
	// tenant's write. The gate is in bytes, checked before any parse, so
	// rejecting costs a length compare. For cost figures on your own hardware,
	// run BenchmarkValidateAtCap; none are quoted here, for the reason the
	// #1722 CHANGELOG entry gives.
	//
	if errs := CheckTenantDocSize(yamlContent); len(errs) > 0 {
		return tcfg, errs
	}
	if err := yaml.Unmarshal([]byte(yamlContent), &tcfg); err != nil {
		return tcfg, []string{"invalid YAML: " + err.Error()}
	}
	// A key the route generator counts as written twice and the Unmarshal
	// above does not (an alias key beside its anchor; two `<<` only in a
	// mapping that typed decode skips — inside `tenants:` it refuses them
	// itself): the generator's StrictLoader refuses the whole file (#2295), so
	// it is refused here as a plain repeated key is by the Unmarshal above.
	if d := pyyamlcompat.FindDuplicateKeyIn([]byte(yamlContent)); d != nil {
		return tcfg, []string{"invalid YAML: " + d.Error()}
	}
	// Reject any non-`tenants` top-level key before anything else (#705).
	if rootErrs := cfg.CheckTenantRootKeys([]byte(yamlContent)); len(rootErrs) > 0 {
		return tcfg, rootErrs
	}
	if _, ok := tcfg.Tenants[tenantID]; !ok {
		return tcfg, []string{fmt.Sprintf("YAML must contain tenants.%s section", tenantID)}
	}
	// The write path commits yamlContent VERBATIM, but the Unmarshal and
	// CheckTenantRootKeys above each see less than that: both decode ONE
	// document, the struct decode skips root keys ThresholdConfig lacks, both
	// resolve a root merge key (`<<`) away, and CheckTenantRootKeys reports
	// nothing for a root key that is not a string (`~:`). So a second document,
	// a parse error behind `...`, a bad tag under an unknown root key, a root
	// merge key or a non-string root key could each carry a `tenants:` section
	// straight into git past every gate in this function (#1681, #1721). A successful Unmarshal above does
	// NOT mean the rest of the body parses — it stopped reading after the first
	// document — so this gate owns every decode error it meets.
	if shapeErrs := yamlDocumentShapeErrors(yamlContent); len(shapeErrs) > 0 {
		return tcfg, shapeErrs
	}
	// The write plane addresses a file by tenant id, but the exporter takes
	// tenant ids from the file's `tenants:` KEYS — so without this the two
	// planes disagree about who the file declares. CheckTenantRootKeys above
	// only walks the ROOT map; a second `tenants.<other>` block passes it, and
	// both remaining gates (RequireOrgWrite, Policy.CheckWrite via
	// extractPatchKeys) read the URL id alone and never see it (#1681).
	// This function joins tenantID into a path below, and it is reachable from
	// callers that have not run the id past guardTenantID (WriteMerged's merge
	// step, and any future one). Re-asserting it here costs a string compare and
	// keeps the containment check in the same function as the path it protects.
	if err := guardTenantID(tenantID); err != nil {
		return tcfg, []string{err.Error()}
	}
	return tcfg, nil
}

// validateBodyOnly is the PRE-FLIGHT validator: every check that can be decided
// from the request body alone, and not one that reads the working tree.
//
// It exists so a pre-flight running against a possibly-stale tree cannot REFUSE
// a write on evidence it has no right to (#1718): a tenant sharing a flat file
// was locked out of its own section until the pod restarted, and a tenant using
// a metric key the platform had just added was told the key does not exist.
//
// ⛔ IT CANNOT SILENTLY GROW A TREE READ: it takes no configDir and no file
// path, so the compiler rejects one. That is the whole reason the parameters
// were dropped rather than passed and ignored.
//
// ⚠️ It is NOT a weaker validate — it is a DIFFERENT question ("is this body
// well-formed?"). The authoritative answer still comes from validate, run
// against the tree the write lands on: every write path runs validate under
// the lock before it commits.
func validateBodyOnly(tenantID, yamlContent string) []string {
	tcfg, errs := validateShape(tenantID, yamlContent)
	if len(errs) > 0 {
		return errs
	}
	// ADR-024 §S5 recipe validation is per-tenant and reads only the body, so it
	// belongs on this side of the line.
	return cfg.ValidateTenantCustomAlerts(tenantID, tcfg.Tenants[tenantID], cfg.MaxCustomRecipesDefault)
}

// validate checks an incoming tenant YAML body before it is written.
//
// yamlContent is the tenant-only document the portal sends (the real
// conf.d/{id}.yaml shape — "Only 'tenants' block"):
//
//	tenants:
//	  <tenantID>:
//	    key: value
//
// Three stages:
//  1. Root-key contract — the body may carry ONLY a top-level `tenants` block
//     (cfg.CheckTenantRootKeys, mirroring tenant-config.schema.json's
//     additionalProperties:false). A stray `defaults:` / `state_filters:` /
//     `profiles:` (or a typo) is rejected, so the write never persists a file
//     that violates conf.d's "Only 'tenants' block" invariant (#705). The same
//     check runs in POST /{id}/validate so the dry-run and the write agree.
//  2. Structural — run on the RAW body: it must be valid YAML and declare the
//     target tenant. Kept separate from the merge so a body missing
//     tenants.{id} is rejected outright rather than silently synthesised by
//     MergeTenantWithRootDefaults' flat-KV fallback.
//  3. Key validation — the _defaults.yaml at configDir is merged in BEFORE
//     ValidateTenantKeys, so a tenant-only body's metric keys resolve against
//     the inherited platform defaults. Without this merge, ValidateTenantKeys
//     sees an empty Defaults map and flags EVERY metric key as "unknown key
//     not in defaults", blocking the write — even though GET /{id}, GET
//     /{id}/effective and POST /{id}/validate all merge defaults and accept
//     the same body (ADR-024 PR4 / #704 write-vs-read asymmetry). It also
//     makes ADR-024 version declarations (e.g. container_cpu{version="v2"})
//     pass without the tenant having to inline `defaults:` into the body.
//     The merge also carries the root platform files' `tenants:` entries for
//     the tenant (#2208) and the profiles those files define, expanded for
//     the tenant as /metrics does (#1385) — but only the tenant's OWN keys
//     are judged for errs: a problem in a platform file's entry, or in the
//     part of the elected profile that reaches the tenant, never blocks
//     this write and comes back as a notice naming that file (and profile)
//     (cfg.TenantMerge). A `_profile` no root platform file defines is
//     still an err, as it always was.
//
// configDir == "" falls back to structural-only key validation (unit tests
// that exercise YAML shape without a defaults fixture).
//
// Returns two channels (#1231 1b, mirroring cfg.KeyValidation): errs is the
// blocking set every write gate turns into ErrValidation; notices is the
// advisory set (deprecated-key alias advisories, problems in a root
// platform file's entry for the tenant, #2208, and in the part of its
// elected profile that reaches it, #1385) that must NEVER block a
// write — callers thread it up to the handler responses so the config author
// sees the migration signal on the write path itself, not only via GET /
// POST /validate. Structural failures (bad YAML / root keys / missing tenant
// section) return nil notices: key validation never ran.
func validate(configDir, tenantID, tenantFilePath, yamlContent string) (errs, notices []string) {
	return validateReplacing(configDir, tenantID, tenantFilePath, yamlContent, false)
}

// validateReplacing is validate with the #2405 permission bit for replacing a
// current file that does not parse (see eolGuardErrs). validate — the merge
// paths' entry — passes false: a merge needs a base it can read, so the bit
// only means something to the whole-file write paths, which pass
// replaceUnparseableAllowed(ctx).
func validateReplacing(configDir, tenantID, tenantFilePath, yamlContent string, replaceUnparseable bool) (errs, notices []string) {
	// ⛔ THE ORDER OF THE REMAINING CHECKS IS UNCHANGED, DELIBERATELY. Hoisting
	// ValidateTenantCustomAlerts up into validateShape would have let a recipe
	// violation short-circuit ahead of addedTenantKeys — i.e. a body that both
	// smuggles a foreign section AND has a bad recipe would stop reporting the
	// foreign section, quietly replacing the #1681 gate's message with another.
	// So this path keeps running the two separately, in the original sequence.
	tcfg, shapeErrs := validateShape(tenantID, yamlContent)
	if len(shapeErrs) > 0 {
		return shapeErrs, nil
	}
	// Read the file this write replaces ONCE. Two stateful checks need it — the
	// added-section gate below and the eol-expansion guard at the end — and on a
	// large flat conf.d file a second full read+parse doubles a validate() that
	// may run while the single-writer lock is held.
	//
	// ⛔ The path comes from the caller (#1673), never from tenantID + ".yaml":
	// a tenant whose file is spelled `.yml` would otherwise get an empty
	// baseline here, and since the baseline fails closed that would refuse
	// every write to a flat file it had legitimately been sharing.
	// ⛔ Guarded on configDir, NOT on tenantFilePath: with configDir="" the
	// resolver still yields a non-empty relative path, so an unconditional read
	// would take a baseline from the CWD and grandfather foreign sections open.
	// Pinned by TestValidateFailsClosedWhenConfigDirIsEmpty.
	var baseRaw []byte
	var baseErr error
	if configDir != "" {
		baseRaw, baseErr = os.ReadFile(tenantFilePath)
	}
	// DELTA, not absolute: a flat conf.d file may legitimately declare several
	// tenants and the exporter serves them, so sections already in the file
	// stay editable and only sections this write ADDS are refused. Every form
	// of the attack is an addition. Residual, deliberately accepted: whoever
	// already has a file naming another tenant keeps that reach — reaching that
	// state needs an operator, not a request.
	if added := addedTenantKeys(baseRaw, tcfg, tenantID); len(added) > 0 {
		return []string{fmt.Sprintf(
			"YAML adds tenant section(s) %v this file does not already declare — a "+
				"tenant config may only add tenants.%s; write the others through "+
				"their own endpoint", added, tenantID)}, nil
	}
	// #1231 c2: the write gate consumes KeyValidation.Errors ONLY — Notices
	// (deprecated-key alias advisories) must never block a write; they ride
	// the second return value instead (1b author-facing wiring).
	var kv cfg.KeyValidation
	if configDir == "" {
		kv = tcfg.ValidateTenantKeys()
	} else {
		// Reuse the body we already decoded into tcfg above instead of handing
		// raw bytes to MergeTenantWithRootDefaults, which would Unmarshal the
		// same yamlContent a third time (#708). tcfg.Tenants[tenantID] is proven
		// present by the check above, so the byte variant's flat-KV fallback —
		// the only behavior the parsed sibling omits — is unreachable here.
		merged := cfg.MergeParsedTenantWithRootDefaults(configDir, tcfg)
		kv = merged.ValidateTenantKeys()
	}
	keyErrs := kv.Errors
	notices = kv.Notices
	// S5 shift-left preflight (ADR-024 §S5): validate the tenant's OWN `_custom_alerts`
	// recipes in-process (Go-native, no promtool/Python). Stateless per-tenant —
	// cross-inheritance collisions + compiler template bugs stay the CI compiler's
	// authority. Runs on the raw body (tcfg), not the merged config: the PUT body is
	// a full overlay, so it carries the tenant's complete own recipe set.
	caViol := cfg.ValidateTenantCustomAlerts(tenantID, tcfg.Tenants[tenantID], cfg.MaxCustomRecipesDefault)
	errs = append(keyErrs, caViol...)

	// B2-wide eol-expansion guard (ADR-024 §8) at the SHARED write choke point, so
	// PutTenant + batch full-config writes are covered, not just the /custom-alerts
	// endpoint. Unlike the checks above this is STATEFUL: it reads the current
	// on-disk tenant file — still the OLD state, since validate runs before the
	// write commits — to compute the per-eol-recipe delta. Skipped when configDir
	// is unset (unit-test shape mode). FAIL CLOSED: only a MISSING tenant file
	// (ENOENT, a brand-new tenant with no existing eol usage) means "no current
	// alerts"; any other read error errors out rather than silently skipping the
	// guard. A file that reads but does not PARSE is replaceable only with
	// platform-wide write permission (#2405) — see eolGuardErrs.
	if configDir != "" {
		// #1673: the path is resolved ONCE by the caller and handed down, so
		// the guard reads the same file the write will land on — and so an
		// ambiguous tenant is refused by the caller with a typed error rather
		// than being flattened into a validation string here. That read now
		// happens once at the top of this function and both stateful checks
		// share it (#1681).
		if ferr := eolGuardErrs(baseRaw, baseErr, yamlContent, tenantID, replaceUnparseable, customalerts.EolExpansionViolations); len(ferr) > 0 {
			return append(errs, ferr...), notices
		}
	}
	return errs, notices
}

// eolGuardErrs is validate's eol-expansion guard (ADR-024 §8): the write may
// not grow any end-of-life recipe's instance count over what the file it
// replaces already has. violations is customalerts.EolExpansionViolations in
// production; it is a parameter only so a test can mark a recipe eol (the
// embedded status map has none) without swapping a process global.
//
// ⛔ #2405 — AN UNPARSEABLE CURRENT FILE IS REPLACEABLE ONLY WITH PLATFORM-WIDE
// WRITE PERMISSION. The guard used to refuse the whole write when the current
// file would not parse (a YAML syntax error, `tenants:` that is not a mapping,
// duplicate keys, a top-level list). That made the whole-file PUT — the one
// endpoint that REPLACES rather than merges, i.e. the repair path — unable to
// repair exactly the files that need it. Nothing in such a file can be read,
// including which tenants it declares, so replacing it is a decision for a
// caller whose write permission covers every tenant: replaceUnparseable is
// that bit (the handler sets it from rbac.PlatformUnrestricted via
// WithReplaceUnparseable). Without it the write is refused, as before #2405.
//
// With it, the baseline is UNKNOWN, and unknown counts as ZERO:
// EolExpansionViolations counts every eol instance the body has above the
// baseline, so with an empty baseline ANY eol recipe in the body is refused
// and a body without one passes. What must never happen is the opposite
// reading — "cannot tell, so do not check" — which would let a broken file
// launder new eol usage in. Pinned by
// TestEolGuard_UnparseableBaseForbidsEveryEolRecipe.
//
// Only a PARSE failure takes that path. A file that exists but cannot be READ
// (permissions, I/O) still refuses the write: nothing about it is known, and
// the write would land on the same unreadable path.
func eolGuardErrs(baseRaw []byte, baseErr error, yamlContent, tenantID string, replaceUnparseable bool,
	violations func(current, next []map[string]any) []string) []string {
	var oldAlerts []map[string]any
	switch {
	case baseErr == nil:
		cur, err := customalerts.Extract(string(baseRaw), tenantID)
		switch {
		case err == nil:
			oldAlerts = cur
		case !replaceUnparseable:
			return []string{MsgUnparseableBaseNeedsPlatformWrite}
		}
		// err != nil with the bit: baseline unknown ⇒ oldAlerts stays empty.
	case !os.IsNotExist(baseErr):
		return []string{"internal error: cannot read current custom alerts: " + pathlessErrText(baseErr)}
	}
	newAlerts, err := customalerts.Extract(yamlContent, tenantID)
	if err != nil {
		return []string{"internal error: cannot read requested custom alerts: " + err.Error()}
	}
	return violations(oldAlerts, newAlerts)
}
