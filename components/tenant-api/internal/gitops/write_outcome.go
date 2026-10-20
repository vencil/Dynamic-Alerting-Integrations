package gitops

// What a failed direct write left on the serving branch (#3173). A write
// that returns an error is in one of three states, and every caller answers
// from the state, not from the error's cause:
//
//   - NOT COMMITTED: nothing reached the branch, so the caller may retry.
//     The request's context stopping the write is always this: the writer
//     polls it at checkpoints only and never kills git with it. A git
//     command killed by its own deadline (ErrGitTimeout) is this too, except
//     at the ref update — and every step before the ref update (add,
//     write-tree, commit-tree, the clean check, the staging commit) moves no
//     ref, so a timeout there is not committed by construction.
//   - COMMITTED: the branch holds the write. A single tenant write returns
//     nil; a batch that is published but not finished returns
//     *BatchCommittedError.
//   - UNKNOWN (*WriteOutcomeUnknownError): the ref update reported an error
//     — a timeout, or any other failure — and the branch could not be read
//     back to see where it is. Only then.
//
// At the ref update the writer decides by reading the branch back and
// comparing with what it knew before the update (refAfterFailedUpdate):
// the compare-and-swap's new commit is committed, the old base is not
// committed, anything else is a conflict (not committed).

import (
	"errors"
	"fmt"
	"strings"
)

// ErrGitTimeout is what a git command killed by its own per-command
// deadline (TENANT_API_GIT_TIMEOUT) wraps (#3173). It is NOT
// context.DeadlineExceeded: that one is the request's (or async task's)
// deadline, which the writer only meets at a checkpoint, before anything is
// written. Whether a timed-out write was committed is decided by the writer
// (see the file comment), never by this sentinel alone.
var ErrGitTimeout = errors.New("git command timed out")

// ErrWriteOutcomeUnknown is what *WriteOutcomeUnknownError unwraps to.
var ErrWriteOutcomeUnknown = errors.New("whether the write was committed is unknown")

// WriteOutcomeUnknownError reports a direct write whose ref update failed
// and whose branch could not be read back afterwards: the write may be on
// the branch or not. It unwraps to ErrWriteOutcomeUnknown ONLY — like
// PushError, the git error is kept in Cause and rendered with %v, never
// wrapped: a cause that wraps ErrGitTimeout or ErrWriteOverloaded would
// otherwise let a caller answer "nothing was written" or "retry shortly".
// A batch's publish marker is kept, so the next write (or startup) brings
// the serving worktree up to the branch if the batch did land
// (RecoverBatchPublish).
type WriteOutcomeUnknownError struct {
	Commit string // the commit the ref update tried to publish
	Cause  error
}

func (e *WriteOutcomeUnknownError) Error() string {
	return fmt.Sprintf("%v (commit %s): %v", ErrWriteOutcomeUnknown, e.Commit, e.Cause)
}

// Unwrap makes errors.Is(err, ErrWriteOutcomeUnknown) hold, and nothing else.
func (e *WriteOutcomeUnknownError) Unwrap() error { return ErrWriteOutcomeUnknown }

// WriteOutcome is the state a direct write's returned error leaves the
// serving branch in (see the file comment).
type WriteOutcome int

const (
	// WriteNotCommitted: nothing of the write is on the branch.
	WriteNotCommitted WriteOutcome = iota
	// WriteCommitted: the write is on the branch.
	WriteCommitted
	// WriteUnknown: the write may or may not be on the branch.
	WriteUnknown
)

// OutcomeOf classifies the error a direct write (Write, WriteMerged,
// WriteBatch, …) returned. A PR-mode write publishes nothing to the
// serving branch, so every error it returns is WriteNotCommitted here — its
// push failure has its own type (PushError).
func OutcomeOf(err error) WriteOutcome {
	var committed *BatchCommittedError
	switch {
	case err == nil, errors.As(err, &committed):
		return WriteCommitted
	case errors.Is(err, ErrWriteOutcomeUnknown):
		return WriteUnknown
	}
	return WriteNotCommitted
}

// headBranchRef is the branch HEAD points at ("refs/heads/main"), or ""
// when HEAD is detached or cannot be read. A ref update samples it BEFORE
// it runs, so that refAfterFailedUpdate can read the branch itself when
// HEAD cannot be read afterwards — a HEAD that points somewhere else by
// then names the wrong branch.
func (w *Writer) headBranchRef() string {
	out, err := w.gitOutput(w.gitDir, "symbolic-ref", "-q", "HEAD")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(string(out))
}

// refAfterFailedUpdate reads where the serving branch is after an
// `update-ref HEAD` that reported an error: HEAD first, and — when HEAD
// cannot be read — the branch ref branchRef (headBranchRef, sampled before
// the update) directly, around the HEAD symref (#3173). An error means
// neither could be read: whether the update took effect is unknown.
func (w *Writer) refAfterFailedUpdate(branchRef string) (string, error) {
	now, herr := w.currentHEAD()
	if herr == nil {
		return now, nil
	}
	if branchRef == "" {
		return "", herr
	}
	out, err := w.gitOutput(w.gitDir, "rev-parse", "--verify", "-q", branchRef)
	if err != nil {
		return "", errors.Join(herr, fmt.Errorf("read %s: %w", branchRef, err))
	}
	return strings.TrimSpace(string(out)), nil
}
