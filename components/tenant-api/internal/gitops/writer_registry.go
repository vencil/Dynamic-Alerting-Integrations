package gitops

// Generic read-modify-write-commit for a single conf.d state file.
//
// MutateConfigFile is the GitOps choke point a monotonic allocator needs
// (ADR-021 §AccountID): unlike the special-file writers (which take a
// caller-rendered full body and overwrite it), an allocator must read the
// CURRENT file, derive the next value from it, and commit the result —
// all while holding the single writer mutex so two concurrent onboardings
// can never read the same high-water mark and allocate the same id.
//
// It deliberately reuses the same w.mu + admission gate + HEAD
// conflict-detection as every other write, so the registry file lives on
// the identical commit-on-write GitOps trail as _groups.yaml / _views.yaml
// (no external stateful DB — ADR-021 / ADR-009).

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
)

// MutateConfigFile applies transform to conf.d/<filename> under the writer
// mutex and commits the result. filename is a bare name (e.g.
// "_account_registry.yaml"); entityType labels the commit + onWrite event.
//
// transform reads the current bytes of the file (nil when the file does not
// exist yet) and returns the bytes to commit. Returning (nil, nil) signals
// "no change" — MutateConfigFile then skips the write entirely so no empty
// commit is produced. A non-nil error aborts the mutation with nothing
// written. The transform runs INSIDE the writer mutex against freshly-read
// bytes, so a counter it advances is a race-free compare-and-swap, not a
// TOCTOU.
//
// The parameter is the UNNAMED func type (not a defined alias) on purpose:
// it makes *Writer satisfy a consumer-side interface (account.RegistryWriter)
// that declares the same unnamed signature — a defined type there would not
// match (Go requires identical method signatures for interface satisfaction).
//
// Flow:
//  1. admission gate (TRK-320) — shed load before taking the mutex
//  2. w.mu — serialise against every other write (the single-writer rule)
//  3. read the current file (ENOENT → nil, a first-write)
//  4. transform(current) → next  (the allocation happens here, in-lock)
//  5. (next == nil) → no-op, release without committing
//  6. commitFileChange — write + git add/commit + HEAD conflict detect
//
// A transform error and ErrConflict propagate to the caller unchanged.
//
// Commit-failure semantics: commitFileChange writes the file BEFORE the git
// commit, so if the commit fails the on-disk file is left ahead of HEAD. The
// next MutateConfigFile reads that uncommitted file, so an advanced value
// (e.g. an account-id allocation) persists on disk even though the caller saw
// an error. This is intentional and shared with every special-file writer:
// re-reads are idempotent (the same tenant returns the same id), so it is a
// stuck-but-consistent file, never a monotonicity violation.
func (w *Writer) MutateConfigFile(ctx context.Context, filename, entityType, authorEmail string, transform func(current []byte) (next []byte, err error)) error {
	// Step 1: load-shedding admission (TRK-320) before w.mu — a registry
	// write is git-bound and must queue behind the same single-writer token
	// as tenant writes, not bypass it.
	if err := w.acquireWrite(ctx); err != nil {
		return err
	}
	defer w.releaseWrite()

	w.mu.Lock()
	defer w.mu.Unlock()

	// Defence-in-depth: callers pass a bare constant filename, but clamp to a
	// basename so the file can never escape configDir even if a future caller
	// passes a path-bearing name — turns the "bare name" convention into an
	// enforced invariant.
	filename = filepath.Base(filename)
	path := filepath.Join(w.configDir, filename)
	current, err := os.ReadFile(path)
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("read %s: %w", filename, err)
	}
	if errors.Is(err, os.ErrNotExist) {
		current = nil
	}

	next, err := transform(current)
	if err != nil {
		return err
	}
	if next == nil {
		// Transform reported no change — skip the write so an idempotent
		// EnsureAccountID on an already-allocated tenant produces no commit.
		return nil
	}

	return w.commitFileChange(path, entityType, authorEmail, next)
}
