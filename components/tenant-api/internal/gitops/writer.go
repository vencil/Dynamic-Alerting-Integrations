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

// ErrNoChanges is returned by WritePRBatch when EVERY op is a byte-identical
// no-op (an idempotent batch / a client retry): the feature branch would carry
// no commits beyond base, so pushing it and opening a PR/MR would yield a
// change-free PR (or a forge 422). The handler maps this to a clean "no changes"
// success — the PR-mode analogue of WriteMerged's direct-path no-op short-circuit
// (#1097 / #1102 review).
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
func guardTenantID(tenantID string) error {
	if !confd.IsTenantConfigFile(tenantID + ".yaml") {
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

// SetBaseBranch sets the PR-mode base branch the Writer branches from and returns
// to (#638). Wired from the TA_GIT_BASE_BRANCH flag in cmd/server. An empty value
// is ignored so the defaultBaseBranch fallback stands. Forge-neutral: this is the
// LOCAL git base, independent of the forge's PR target branch.
func (w *Writer) SetBaseBranch(b string) {
	if b != "" {
		w.baseBranch = b
	}
}

// base returns the configured base branch, or defaultBaseBranch when unset — so a
// zero-value Writer (e.g. constructed in tests via NewWriter) still has a base.
func (w *Writer) base() string {
	if w.baseBranch == "" {
		return defaultBaseBranch
	}
	return w.baseBranch
}

// checkoutBaseClean force-resets to a pristine base branch (#638). A plain
// `git checkout <base>` REFUSES when the working tree is dirty — which a write
// killed AFTER os.WriteFile but BEFORE its commit completes leaves behind (a
// modified-uncommitted tracked file). That would wedge EVERY subsequent PR write
// at the anchoring checkout — a global brick, and on a PVC-backed conf.d it
// survives pod restarts (a death-loop). So we first `reset --hard` (drop index +
// worktree changes) then `checkout -f` (switch even past a dirty tree). Safe to
// discard: in PR mode no uncommitted change here is ever legitimate — each write
// writes-then-commits on its own feature branch, returning to base when done.
func (w *Writer) checkoutBaseClean(base string) error {
	if err := w.gitExec("reset", "--hard", "HEAD"); err != nil {
		return fmt.Errorf("reset to clean state: %w", err)
	}
	if err := w.gitExec("checkout", "-f", base); err != nil {
		return fmt.Errorf("checkout base %q: %w", base, err)
	}
	return nil
}

// resolveFreshBaseRef fetches origin/<base> (in-lock, TRK-318) and returns the
// ref the new PR branch should be CUT FROM: "origin/<base>" once the fetch has
// populated it, else the local <base> as a best-effort fallback. The caller MUST
// already be on a clean base (via checkoutBaseClean) and MUST hold w.mu.
//
// WHY this exists: the local base ref is only synced at pod startup by the
// git-clone initContainer. A long-lived pod's base therefore STALLS after a
// remote merge — so a later PR branched from it silently rolls back a shared
// file (_groups.yaml / _views.yaml / _federation_policy.yaml) that another
// tenant already merged. Fetching here, inside the lock and immediately before
// branching, closes that window atomically (B1, not the TOCTOU-prone lock-outside
// B2 — ADR-023 §B).
//
// WHY return a ref instead of `reset --hard origin/<base>`: a hard reset would
// move the LOCAL base ref and revert the working tree — silently discarding any
// commit made directly on the local base. The special-file writes
// (WriteGroupsFile / WriteViewsFile / WriteFederationPolicyFile) commit straight
// to the current branch via commitFileChange even in PR mode, so they CAN leave
// such local-base commits. Branching the new feature branch from origin/<base>
// gets the same fresh anchor without touching the local base or its working tree.
//
// Failure semantics:
//   - No origin remote (dev/local, unit tests): nothing to be stale against, the
//     local base IS the source of truth → return the local base, no fetch.
//   - Fetch TIMEOUT: forge degradation → return ErrForgeDegraded so the caller
//     releases the lock and the handler returns 503. NEVER proceed on a stale base.
//   - Fetch non-timeout error (origin/<base> not pushed yet, transient remote
//     blip): the hazard TRK-318 guards is the never-fetches case; a one-off error
//     self-corrects on the next write, so warn and fall back to the local base
//     rather than bricking every write on a flaky remote.
func (w *Writer) resolveFreshBaseRef(base string) (string, error) {
	if !w.hasOriginRemote() {
		return base, nil
	}
	if err := w.fetchOriginBase(base); err != nil {
		return "", err // ErrForgeDegraded on timeout (only hard-fail path)
	}
	// Branch from the freshest remote-tracking ref when it exists; otherwise
	// (freshly-init'd bare remote with nothing pushed) fall back to local base.
	if w.originRefExists(base) {
		return "origin/" + base, nil
	}
	return base, nil
}

// fetchOriginBase runs `git fetch --prune origin <base>` bounded by the SEPARATE
// fetchTimeout (TRK-318). A deadline kill is the forge-degradation signal: sweep
// any locks the SIGKILL'd fetch left and return ErrForgeDegraded (fail loud). A
// non-deadline error is logged and swallowed (proceed on local base).
func (w *Writer) fetchOriginBase(base string) error {
	timeout := w.fetchTimeout
	if timeout <= 0 {
		timeout = defaultGitFetchTimeout
	}
	cmd, ctx, cancel := w.gitCmdWithTimeout(timeout, "-C", w.gitDir, "fetch", "--prune", "origin", base)
	defer cancel()
	out, err := cmd.CombinedOutput()
	if err == nil {
		return nil
	}
	if errors.Is(ctx.Err(), context.DeadlineExceeded) {
		// SIGKILL'd fetch leaves no signal cleanup; clear stale locks so the
		// released mutex hands the next write a clean repo (mirrors gitErr's #638
		// self-heal). Then fail loud — the caller turns this into a 503.
		w.clearStaleGitLocks()
		slog.Warn("gitops: base fetch timed out — write lock released (TRK-318)",
			"base", base, "timeout", timeout)
		return fmt.Errorf("git fetch origin %s timed out after %s: %w", base, timeout, ErrForgeDegraded)
	}
	slog.Warn("gitops: base fetch failed — proceeding on local base (TRK-318)",
		"base", base, "error", err, "output", strings.TrimSpace(string(out)))
	return nil
}

// hasOriginRemote reports whether an "origin" remote is configured. Used to skip
// the base fetch in dev/local/unit-test setups that have no forge.
func (w *Writer) hasOriginRemote() bool {
	cmd, _, cancel := w.gitCmd("-C", w.gitDir, "remote", "get-url", "origin")
	defer cancel()
	return cmd.Run() == nil
}

// originRefExists reports whether the remote-tracking ref refs/remotes/origin/<base>
// exists locally (i.e. a successful fetch/clone has populated it at least once).
func (w *Writer) originRefExists(base string) bool {
	cmd, _, cancel := w.gitCmd("-C", w.gitDir, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/"+base)
	defer cancel()
	return cmd.Run() == nil
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
func (w *Writer) Write(ctx context.Context, tenantID, authorEmail, yamlContent string) error {
	// Step 0: reserved-id backstop (defense-in-depth; see guardTenantID).
	if err := guardTenantID(tenantID); err != nil {
		return err
	}
	// Step 1: validate schema before touching disk (and before taking an
	// admission slot — validation is cheap, CPU-only, and must not consume the
	// single-writer token).
	if errs := validate(w.configDir, tenantID, yamlContent); len(errs) > 0 {
		return fmt.Errorf("%w: %s", ErrValidation, strings.Join(errs, "; "))
	}

	// Step 2: load-shedding admission (TRK-320) before w.mu.
	if err := w.acquireWrite(ctx); err != nil {
		return err
	}
	defer w.releaseWrite()

	w.mu.Lock()
	defer w.mu.Unlock()

	return w.commitFileChange(
		filepath.Join(w.configDir, tenantID+".yaml"),
		tenantID,
		authorEmail,
		[]byte(yamlContent),
	)
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
// the caller can detect a byte-identical (no-op) merge.
func (w *Writer) readMergeValidate(tenantID string, merge MergeFunc) (content string, existing []byte, err error) {
	existing, rerr := os.ReadFile(filepath.Join(w.configDir, tenantID+".yaml"))
	if rerr != nil && !os.IsNotExist(rerr) {
		return "", nil, fmt.Errorf("read current tenant file for %s: %w", tenantID, rerr)
	}
	content, merr := merge(existing)
	if merr != nil {
		return "", existing, fmt.Errorf("merge tenant config for %s: %w", tenantID, merr)
	}
	if errs := validate(w.configDir, tenantID, content); len(errs) > 0 {
		return "", existing, fmt.Errorf("%w for %s: %s", ErrValidation, tenantID, strings.Join(errs, "; "))
	}
	return content, existing, nil
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
func (w *Writer) WriteMerged(ctx context.Context, tenantID, authorEmail string, merge MergeFunc) error {
	// Reserved-id backstop (defense-in-depth; see guardTenantID).
	if err := guardTenantID(tenantID); err != nil {
		return err
	}
	// Load-shedding admission (TRK-320) before w.mu, same as Write.
	if err := w.acquireWrite(ctx); err != nil {
		return err
	}
	defer w.releaseWrite()

	w.mu.Lock()
	defer w.mu.Unlock()

	content, existing, err := w.readMergeValidate(tenantID, merge)
	if err != nil {
		return err
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
		return nil
	}
	return w.commitFileChange(
		filepath.Join(w.configDir, tenantID+".yaml"),
		tenantID,
		authorEmail,
		[]byte(content),
	)
}

// Diff returns the unified diff between the current file and proposed content.
// Returns empty string if files are identical or no current file exists.
func (w *Writer) Diff(tenantID, proposedContent string) (string, error) {
	filePath := filepath.Join(w.configDir, tenantID+".yaml")

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

	if err := w.gitCommit(filePath, commitTag, authorEmail, trailer...); err != nil {
		slog.Warn("gitops: commit failed", "commit_tag", commitTag, "error", err)
		return fmt.Errorf("git commit: %w", err)
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

// currentHEAD returns the current HEAD commit hash of the git repository.
func (w *Writer) currentHEAD() (string, error) {
	cmd, ctx, cancel := w.gitCmd("-C", w.gitDir, "rev-parse", "HEAD")
	defer cancel()
	out, err := cmd.Output()
	if err != nil {
		return "", w.gitErr(ctx, "rev-parse HEAD", err, out)
	}
	return strings.TrimSpace(string(out)), nil
}

// commitParent returns the parent commit hash of HEAD (i.e. HEAD~1).
func (w *Writer) commitParent() (string, error) {
	cmd, ctx, cancel := w.gitCmd("-C", w.gitDir, "rev-parse", "HEAD~1")
	defer cancel()
	out, err := cmd.Output()
	if err != nil {
		return "", w.gitErr(ctx, "rev-parse HEAD~1", err, out)
	}
	return strings.TrimSpace(string(out)), nil
}

// gitCommit stages filePath and creates a commit with the operator's email as author.
//
// Committer identity is sourced from the GIT_COMMITTER_NAME / GIT_COMMITTER_EMAIL
// environment variables (set in the K8s Deployment). This keeps the audit trail clean:
//   - author  = the human operator (from X-Forwarded-Email via oauth2-proxy)
//   - committer = the service account (da-portal@dynamic-alerting.local)
func (w *Writer) gitCommit(filePath, tenantID, authorEmail string, trailer ...string) error {
	// Stage the file
	addCmd, addCtx, addCancel := w.gitCmd("-C", w.gitDir, "add", filePath)
	defer addCancel()
	if out, err := addCmd.CombinedOutput(); err != nil {
		return w.gitErr(addCtx, "add", err, out)
	}

	// Check if there's actually something to commit
	statusCmd, _, statusCancel := w.gitCmd("-C", w.gitDir, "diff", "--cached", "--quiet")
	defer statusCancel()
	if err := statusCmd.Run(); err == nil {
		// Exit 0 means no changes staged — nothing to commit
		return nil
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
		return w.gitErr(commitCtx, "commit", err, out)
	}
	return nil
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
func validate(configDir, tenantID, yamlContent string) []string {
	var tcfg cfg.ThresholdConfig
	if err := yaml.Unmarshal([]byte(yamlContent), &tcfg); err != nil {
		return []string{"invalid YAML: " + err.Error()}
	}
	// Reject any non-`tenants` top-level key before anything else (#705).
	if rootErrs := cfg.CheckTenantRootKeys([]byte(yamlContent)); len(rootErrs) > 0 {
		return rootErrs
	}
	if _, ok := tcfg.Tenants[tenantID]; !ok {
		return []string{fmt.Sprintf("YAML must contain tenants.%s section", tenantID)}
	}
	var keyErrs []string
	if configDir == "" {
		keyErrs = tcfg.ValidateTenantKeys()
	} else {
		// Reuse the body we already decoded into tcfg above instead of handing
		// raw bytes to MergeTenantWithRootDefaults, which would Unmarshal the
		// same yamlContent a third time (#708). tcfg.Tenants[tenantID] is proven
		// present by the check above, so the byte variant's flat-KV fallback —
		// the only behavior the parsed sibling omits — is unreachable here.
		merged := cfg.MergeParsedTenantWithRootDefaults(configDir, tcfg)
		keyErrs = merged.ValidateTenantKeys()
	}
	// S5 shift-left preflight (ADR-024 §S5): validate the tenant's OWN `_custom_alerts`
	// recipes in-process (Go-native, no promtool/Python). Stateless per-tenant —
	// cross-inheritance collisions + compiler template bugs stay the CI compiler's
	// authority. Runs on the raw body (tcfg), not the merged config: the PUT body is
	// a full overlay, so it carries the tenant's complete own recipe set.
	caViol := cfg.ValidateTenantCustomAlerts(tenantID, tcfg.Tenants[tenantID], cfg.MaxCustomRecipesDefault)
	errs := append(keyErrs, caViol...)

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
		oldRaw, rerr := os.ReadFile(filepath.Join(configDir, tenantID+".yaml"))
		switch {
		case rerr == nil:
			oldAlerts, err := customalerts.Extract(string(oldRaw), tenantID)
			if err != nil {
				return append(errs, "internal error: cannot read current custom alerts: "+err.Error())
			}
			newAlerts, err := customalerts.Extract(yamlContent, tenantID)
			if err != nil {
				return append(errs, "internal error: cannot read requested custom alerts: "+err.Error())
			}
			errs = append(errs, customalerts.EolExpansionViolations(oldAlerts, newAlerts)...)
		case !os.IsNotExist(rerr):
			return append(errs, "internal error: cannot read current custom alerts: "+rerr.Error())
		}
	}
	return errs
}
