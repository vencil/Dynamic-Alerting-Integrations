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
	"context"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/customalerts"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
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

// guardTenantID rejects an id whose {id}.yaml would not be a tenant config
// file. It is the writer-side second enforcement of the same confd predicate
// the handler's ValidateTenantID uses — so no tenant write method can overwrite
// a reserved control file even if a future caller forgets to validate first
// (single-choke-point fragility is the exact bug class this change closes).
// extraDocumentsWithContent counts the YAML documents after the first that
// decode to something non-nil. A body whose YAML is invalid returns 0 — that is
// the caller's earlier Unmarshal check to report, not this one's.
func extraDocumentsWithContent(yamlContent string) int {
	dec := yaml.NewDecoder(strings.NewReader(yamlContent))
	extra := 0
	for i := 0; ; i++ {
		var doc any
		if err := dec.Decode(&doc); err != nil {
			return extra // io.EOF, or a parse error the Unmarshal above already owns
		}
		if i > 0 && doc != nil {
			extra++
		}
	}
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

func guardTenantID(tenantID string) error {
	if !confd.IsAddressableTenantID(tenantID) {
		return fmt.Errorf("%w: %q", ErrReservedTenantID, tenantID)
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
	gitTimeout     time.Duration // per-git-command wall-clock deadline (#630); 0 → defaultGitTimeout
	gitWaitDelay   time.Duration // cmd.WaitDelay grace after a deadline kill (#630); 0 → defaultGitKillGrace
	gitBinary      string        // git executable; "git" in prod, overridden in tests (timeout seam)
	baseBranch     string        // PR-mode base to branch from / return to (#638); "" → defaultBaseBranch
	fetchTimeout   time.Duration // in-lock base fetch deadline (TRK-318); 0 → defaultGitFetchTimeout

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

// Write validates, persists, and commits a tenant's config YAML.
//
// Flow (steps 2–6 are shared with writeSpecialFile via commitFileChange):
//  1. Validate YAML schema (ParseConfig + ValidateTenantKeys)
//  2. Lock mutex
//  3. Record HEAD before write
//  4. Write file to configDir/{tenantID}.yaml
//  5. git add + git commit --author="<authorEmail>"
//  6. Check HEAD again (conflict detection)
//  7. onWrite callback (e.g. SSE broadcast)
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
// The check runs INSIDE the writer lock, immediately before the commit, so it
// is the authoritative one: a caller that also compares hashes in its handler
// is doing a cheap early rejection, not concurrency control — that comparison
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
	// #1673: resolve the tenant's file first — validate's eol guard reads it,
	// and the commit below must land on the same one. An ambiguous tenant is
	// refused here with a typed error, so callers can map it to 409 instead of
	// the 400 a validation string would produce.
	filePath, err := w.tenantFilePath(tenantID)
	if err != nil {
		return nil, err
	}
	// Step 1: validate schema before touching disk (and before taking an
	// admission slot — validation is cheap, CPU-only, and must not consume the
	// single-writer token).
	errs, notices := validate(w.configDir, tenantID, filePath, yamlContent)
	if len(errs) > 0 {
		return nil, fmt.Errorf("%w: %s", ErrValidation, strings.Join(errs, "; "))
	}

	// Step 2: load-shedding admission (TRK-320) before w.mu.
	if err := w.acquireWrite(ctx); err != nil {
		return nil, err
	}
	defer w.releaseWrite()

	w.mu.Lock()
	defer w.mu.Unlock()

	// Optimistic concurrency, under the lock and immediately before the write:
	// anything that lands between here and commitFileChange would have to hold
	// w.mu, which this goroutine has.
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
// tenantFilePath is the writer-side answer to "which file does this tenant's
// config live in" (#1673). It returns the tenant's EXISTING file whatever its
// spelling, and DefaultTenantFileName only for a tenant that has none — so a
// write to a tenant stored as `<id>.yml` updates that file instead of creating
// a second `<id>.yaml` beside it. An ambiguous tenant (both spellings on disk)
// is refused rather than silently resolved; see confd.ErrAmbiguousTenantFile.
//
// Callers that both READ the existing file and WRITE it back must resolve ONCE
// and pass the path down, so the two halves of one flow cannot disagree.
func (w *Writer) tenantFilePath(tenantID string) (string, error) {
	return confd.TenantFilePathForWrite(w.configDir, tenantID)
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
		return "", existing, fmt.Errorf("merge tenant config for %s: %w", tenantID, merr)
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
	if err != nil {
		return err
	}
	if errs := validateBodyOnly(tenantID, content); len(errs) > 0 {
		return fmt.Errorf("%w for %s: %s", ErrValidation, tenantID, strings.Join(errs, "; "))
	}
	return nil
}

func (w *Writer) readMergeValidate(tenantID, filePath string, merge MergeFunc) (content string, existing []byte, notices []string, err error) {
	content, existing, err = w.readMerge(tenantID, filePath, merge)
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
// Unlike Write(), validation runs under the lock — the final content is not
// known until the base is read. The merge + validate are CPU-only and
// sub-millisecond, so the extra time holding the write token is negligible.
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

	w.mu.Lock()
	defer w.mu.Unlock()

	// #1673: one resolution for the whole flow — the file we read below and the
	// file we commit at the end must be the same one.
	filePath, err := w.tenantFilePath(tenantID)
	if err != nil {
		return nil, err
	}
	content, existing, notices, err := w.readMergeValidate(tenantID, filePath, merge)
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
func (w *Writer) Diff(tenantID, proposedContent string) (string, error) {
	filePath, err := w.tenantFilePath(tenantID)
	if err != nil {
		return "", err
	}

	existing, err := os.ReadFile(filePath)
	if os.IsNotExist(err) {
		// New file — show the entire proposed content as an addition
		var lines []string
		for _, line := range strings.Split(proposedContent, "\n") {
			lines = append(lines, "+"+line)
		}
		return strings.Join(lines, "\n"), nil
	}
	if err != nil {
		return "", fmt.Errorf("read existing: %w", err)
	}

	if string(existing) == proposedContent {
		return "", nil
	}

	// Use git diff --no-index for a proper unified diff
	tmpFile, err := os.CreateTemp("", "tenant-api-diff-*.yaml")
	if err != nil {
		return "", fmt.Errorf("create temp file: %w", err)
	}
	defer func() { _ = os.Remove(tmpFile.Name()) }()

	if _, err := tmpFile.WriteString(proposedContent); err != nil {
		return "", fmt.Errorf("write temp file: %w", err)
	}
	_ = tmpFile.Close()

	cmd, _, cancel := w.gitCmd("diff", "--no-index", "--", filePath, tmpFile.Name())
	defer cancel()
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
func (w *Writer) commitFileChange(filePath, commitTag, authorEmail string, content []byte, trailer ...string) error {
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
//
// configDir == "" falls back to structural-only key validation (unit tests
// that exercise YAML shape without a defaults fixture).
//
// Returns two channels (#1231 1b, mirroring cfg.KeyValidation): errs is the
// blocking set every write gate turns into ErrValidation; notices is the
// advisory set (deprecated-key alias advisories) that must NEVER block a
// write — callers thread it up to the handler responses so the config author
// sees the migration signal on the write path itself, not only via GET /
// POST /validate. Structural failures (bad YAML / root keys / missing tenant
// section) return nil notices: key validation never ran.
// validateShape runs every check that reads ONLY the request body and the URL
// id, in the order validate has always run them, and hands back the parsed
// config so no caller decodes the same bytes twice.
//
// ⛔ THE SPLIT LINE IS "DOES IT READ THE WORKING TREE", NOT "DOES IT SOUND
// STATIC" (#1718). Three of validate's checks read the tree and are therefore
// only meaningful against the tree the write lands on:
//
//   - addedTenantKeys      ← os.ReadFile(tenantFilePath)
//   - ValidateTenantKeys   ← mergeTenantConfig reads <configDir>/_defaults.yaml
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
	if err := yaml.Unmarshal([]byte(yamlContent), &tcfg); err != nil {
		return tcfg, []string{"invalid YAML: " + err.Error()}
	}
	// Reject any non-`tenants` top-level key before anything else (#705).
	if rootErrs := cfg.CheckTenantRootKeys([]byte(yamlContent)); len(rootErrs) > 0 {
		return tcfg, rootErrs
	}
	if _, ok := tcfg.Tenants[tenantID]; !ok {
		return tcfg, []string{fmt.Sprintf("YAML must contain tenants.%s section", tenantID)}
	}
	// Everything after the first YAML document is bytes that nothing here reads:
	// Unmarshal above and CheckTenantRootKeys both decode ONE document and
	// report no error for the rest, while the write path commits yamlContent
	// VERBATIM — so a second document carries any `tenants:` section or root key
	// straight into git, past every gate in this function (#1681). An empty
	// trailer (a bare `---`, a comment-only document) carries nothing and stays
	// legal, so this counts content rather than documents.
	if extra := extraDocumentsWithContent(yamlContent); extra > 0 {
		return tcfg, []string{fmt.Sprintf(
			"YAML has %d document(s) with content after the first — a tenant config "+
				"is a single document; anything after it would be written but never "+
				"validated", extra)}
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
// against the tree the write lands on. A caller that has no checkout between
// the two — Write, WriteMerged — must keep calling validate directly.
func validateBodyOnly(tenantID, yamlContent string) []string {
	tcfg, errs := validateShape(tenantID, yamlContent)
	if len(errs) > 0 {
		return errs
	}
	// ADR-024 §S5 recipe validation is per-tenant and reads only the body, so it
	// belongs on this side of the line.
	return cfg.ValidateTenantCustomAlerts(tenantID, tcfg.Tenants[tenantID], cfg.MaxCustomRecipesDefault)
}

func validate(configDir, tenantID, tenantFilePath, yamlContent string) (errs, notices []string) {
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
	// runs before the single-writer token is taken.
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
	// alerts"; any other read error or a parse failure errors out rather than
	// silently skipping the guard (matches the handler's extraction fail-closed).
	if configDir != "" {
		// #1673: the path is resolved ONCE by the caller and handed down, so
		// the guard reads the same file the write will land on — and so an
		// ambiguous tenant is refused by the caller with a typed error rather
		// than being flattened into a validation string here. That read now
		// happens once at the top of this function and both stateful checks
		// share it (#1681).
		oldRaw, rerr := baseRaw, baseErr
		switch {
		case rerr == nil:
			oldAlerts, err := customalerts.Extract(string(oldRaw), tenantID)
			if err != nil {
				return append(errs, "internal error: cannot read current custom alerts: "+err.Error()), notices
			}
			newAlerts, err := customalerts.Extract(yamlContent, tenantID)
			if err != nil {
				return append(errs, "internal error: cannot read requested custom alerts: "+err.Error()), notices
			}
			errs = append(errs, customalerts.EolExpansionViolations(oldAlerts, newAlerts)...)
		case !os.IsNotExist(rerr):
			return append(errs, "internal error: cannot read current custom alerts: "+rerr.Error()), notices
		}
	}
	return errs, notices
}
