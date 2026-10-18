package gitops

// The generator's verdict on a write (ADR-036 §4, #2903).
//
// Every Writer path that changes a conf.d file's content asks a
// WriteValidator before it commits: under the writer lock, with the full
// new content of every file it is about to write already computed, after
// the Go checks of that path, before anything is committed. The validator is
// the generator container (internal/generator): it judges base commit +
// writes with `generate-routes --validate --strict`, and a non-nil error
// refuses the write — nothing is committed, and nothing of the write is left
// in the serving tree.
//
// The paths (the list the #2903 test walks, writer_validator_test.go):
//
//   - write (Write / WriteIfUnchanged: PUT /tenants/{id}, and
//     PUT /tenants/{id}/custom-alerts, which commits through WriteIfUnchanged)
//   - WriteMerged
//   - WriteBatch (direct-mode tenant and group batches, sync and async)
//   - WritePRChecked (WritePR; PR-mode PUT /tenants/{id})
//   - WritePRBatch (PR-mode tenant and group batches)
//
// base is the commit the write lands on: the serving HEAD under the lock in
// direct mode, the commit the feature branch is cut from in PR mode.

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// ProposedFile is one file a write is about to commit: Path relative to
// conf.d (slash-separated, as the generator's `writes[].path`), Content the
// exact bytes that will be committed, Tenants the tenants the write is for
// (what makes a finding "related" to it).
type ProposedFile struct {
	Path    string
	Content []byte
	Tenants []string
}

// WriteValidator judges the conf.d tree at base with files written over it.
// A nil error lets the write commit those exact bytes; any error refuses it,
// and is returned to the Writer's caller unchanged.
type WriteValidator interface {
	ValidateWrites(ctx context.Context, base string, files []ProposedFile) error
}

// SetValidator installs the validator every content-changing write asks
// (see the file comment). Set it once at startup, before the server serves.
//
// ⛔ IT IS REQUIRED (#1535). A Writer without one refuses every write that
// would change content, with ErrNoValidator, before anything is written:
// there is one commit path, the judged one. (There used to be a second,
// unjudged one for a nil validator — reached only by tests — that committed
// the whole index with `git commit` and kept every residue the judged path
// had been rid of.) Tests install a permissive one (gitopstest.Permissive).
func (w *Writer) SetValidator(v WriteValidator) { w.validator = v }

// ErrNoValidator is what a content-changing write is refused with when the
// Writer has no WriteValidator (SetValidator): nothing can judge it, so
// nothing is written. A wiring defect, never a client's.
var ErrNoValidator = errors.New("gitops: no write validator installed; refusing to write")

// errValidatorBase reports a write whose base commit could not be read for
// the validator: without it there is nothing to judge, so nothing is
// written.
var errValidatorBase = errors.New("cannot read the commit the write lands on")

// validateProposed asks the validator about files over base; nil when
// there is nothing to write, ErrNoValidator when no validator is installed.
// Caller holds w.mu.
func (w *Writer) validateProposed(ctx context.Context, base string, files []ProposedFile) error {
	if len(files) == 0 {
		return nil
	}
	if w.validator == nil {
		return ErrNoValidator
	}
	return w.validator.ValidateWrites(ctx, base, files)
}

// validateTenantFile is validateProposed for one tenant file under
// w.configDir, against the serving HEAD (direct mode) or the checked-out
// feature branch's HEAD — its branch point (PR mode, before anything is
// committed on it). Caller holds w.mu.
//
// base is the commit the verdict is on. The direct-mode caller hands it to commitFileChange, which commits only
// while HEAD is still base: w.mu keeps every in-process write out, but not a
// commit made from outside the process while the generator judged.
func (w *Writer) validateTenantFile(ctx context.Context, filePath, tenantID string, content []byte) (base string, err error) {
	if w.validator == nil {
		return "", ErrNoValidator
	}
	if w.beforeBaseSample != nil {
		w.beforeBaseSample()
	}
	base, err = w.currentHEAD()
	if err != nil {
		return "", fmt.Errorf("%w: %v", errValidatorBase, err)
	}
	if err := w.validateProposed(ctx, base, []ProposedFile{{
		Path: confRelKey(w.configDir, filePath), Content: content, Tenants: []string{tenantID},
	}}); err != nil {
		return "", err
	}
	return base, nil
}

// validateStaging is validateProposed for every file a batch changed in its
// staging tree, read back from there — the bytes commitStaging commits.
// On a pass it records them in st.validated, which commitStaging holds the
// staged blobs to (checkStagedAsValidated). Caller holds w.mu.
func (w *Writer) validateStaging(ctx context.Context, st *stagingTree, base string) error {
	if len(st.order) == 0 {
		return nil
	}
	if w.validator == nil {
		return ErrNoValidator
	}
	files := make([]ProposedFile, 0, len(st.order))
	validated := make(map[string][]byte, len(st.order))
	for _, rel := range st.order {
		content, err := os.ReadFile(filepath.Join(st.root, rel))
		if err != nil {
			return fmt.Errorf("read a batch file for the generator: %w", err)
		}
		path := filepath.ToSlash(rel)
		if st.rel != "." && st.rel != "" {
			path = strings.TrimPrefix(path, filepath.ToSlash(st.rel)+"/")
		}
		files = append(files, ProposedFile{Path: path, Content: content,
			Tenants: append([]string(nil), st.changed[rel]...)})
		validated[rel] = content
	}
	if err := w.validateProposed(ctx, base, files); err != nil {
		return err
	}
	st.validated = validated
	return nil
}

// ErrContentNormalized is what a write is refused with (errors.Is) when git
// would commit other bytes than the ones the generator judged: a
// .gitattributes rule (eol, text, a clean filter…) rewrote a file as it was
// staged (ADR-036 §4 "驗的和寫的必須是同一份"). Nothing is committed. A
// validator that implements NormalizationRefuser answers it in its own
// terms instead; this is the fallback.
var ErrContentNormalized = errors.New("git attributes would commit other bytes than the ones the generator judged")

// NormalizationRefuser is implemented by a WriteValidator that renders the
// refusal of a write whose staged bytes differ from the judged ones itself
// (the generator gate: a non-retryable GENERATOR_REJECTED, counted). paths
// are the files concerned, as the caller staged them.
type NormalizationRefuser interface {
	RefuseNormalized(paths []string) error
}

// checkStagedAsValidated compares the blob staged in dir's index (env:
// the temporary index's GIT_INDEX_FILE, or nil) for every path in validated
// (a path as given to `git add` in dir) with the bytes the generator
// judged, and refuses the write (see ErrContentNormalized) when one
// differs. Every commit is judged (#1535), so an empty validated is a
// caller's defect, refused rather than checking nothing. Caller holds w.mu.
func (w *Writer) checkStagedAsValidated(dir string, env []string, validated map[string][]byte) error {
	if len(validated) == 0 {
		return errors.New("gitops: commit without the bytes the generator judged")
	}
	var changed []string
	for p, want := range validated {
		rel := p
		if filepath.IsAbs(p) {
			r, err := filepath.Rel(dir, p)
			if err != nil {
				return fmt.Errorf("locate a staged file: %w", err)
			}
			rel = r
		}
		got, err := w.gitOutputEnv(dir, env, "cat-file", "blob", ":./"+filepath.ToSlash(rel))
		if err != nil {
			return fmt.Errorf("read a staged file back: %w", err)
		}
		if !bytes.Equal(got, want) {
			changed = append(changed, filepath.ToSlash(rel))
		}
	}
	if len(changed) == 0 {
		return nil
	}
	sort.Strings(changed)
	if r, ok := w.validator.(NormalizationRefuser); ok {
		return &normalizedRefusal{err: r.RefuseNormalized(changed)}
	}
	return fmt.Errorf("%w: %s", ErrContentNormalized, strings.Join(changed, ", "))
}

// stagedRefusal reports whether err is checkStagedAsValidated's refusal:
// ErrContentNormalized, or what the validator's RefuseNormalized returned
// (it is marked by normalizedRefusal).
func (w *Writer) stagedRefusal(err error) bool {
	var n *normalizedRefusal
	return errors.Is(err, ErrContentNormalized) || errors.As(err, &n)
}

// normalizedRefusal carries a NormalizationRefuser's error through the
// commit path unchanged (Unwrap), marked as the staged-bytes refusal.
type normalizedRefusal struct{ err error }

func (e *normalizedRefusal) Error() string { return e.err.Error() }
func (e *normalizedRefusal) Unwrap() error { return e.err }

// restoreFromHEAD puts the serving file filePath back as HEAD has it after
// a write that did not commit (removed when HEAD has no such file). It
// touches the worktree only: since #1535 a direct write builds its commit
// in a temporary index and gitCommitOnto puts the real index's entry back
// itself, so the real index holds what it held before the write — an
// operator's staged change to this very path included — and resetting it
// here would destroy that. `git restore --worktree` writes the file as a
// checkout would (smudge, eol); when it fails (an index.lock, a timeout)
// the file is written from HEAD's blob directly, which needs no lock — the
// serving file is what the exporter and every later read see.
// Caller holds w.mu.
func (w *Writer) restoreFromHEAD(filePath string) error {
	out, err := w.gitOutput(w.gitDir, "ls-tree", "--name-only", "HEAD", "--", filePath)
	if err != nil {
		return fmt.Errorf("read HEAD's entry: %w", err)
	}
	if len(bytes.TrimSpace(out)) == 0 {
		if err := os.Remove(filePath); err != nil && !os.IsNotExist(err) {
			return err
		}
		return nil
	}
	// restore takes no GIT_LITERAL_PATHSPECS (literalPathspecs' list is
	// closed): the path is made literal itself.
	rerr := w.gitIn(w.gitDir, "restore", "--source=HEAD", "--worktree", "--", literalPathspec(filePath))
	if rerr == nil {
		return nil
	}
	rerr = fmt.Errorf("restore: %w", rerr)
	rel, err := filepath.Rel(w.gitDir, filePath)
	if err != nil {
		return errors.Join(err, rerr)
	}
	blob, err := w.gitOutput(w.gitDir, "cat-file", "blob", "HEAD:./"+filepath.ToSlash(rel))
	if err != nil {
		return errors.Join(fmt.Errorf("read HEAD's blob: %w", err), rerr)
	}
	if err := writeFileAtomic(filePath, blob, 0644); err != nil {
		return errors.Join(err, rerr)
	}
	return rerr
}

// unchangedFile is the no-op rule of the single-file writes (Write and
// WriteMerged, #2903): content changes nothing only when it is
// byte-identical both to the serving file (existing; nil when there is
// none) and to HEAD's blob of it. The serving file alone can hold bytes
// HEAD does not — an uncommitted edit from outside, or a write whose file
// landed and whose commit failed (index.lock) — and answering success for
// those would report a body that is never committed. When it holds, the
// write is a success with nothing committed and the generator not asked
// (the tree it would judge is HEAD's own).
func (w *Writer) unchangedFile(filePath string, existing []byte, content string) bool {
	return existing != nil && string(existing) == content && w.headHolds(filePath, existing)
}

// headHolds reports whether HEAD's blob of filePath is exactly content
// (false when HEAD has no such file or it cannot be read).
func (w *Writer) headHolds(filePath string, content []byte) bool {
	rel, err := filepath.Rel(w.gitDir, filePath)
	if err != nil {
		return false
	}
	blob, err := w.gitOutput(w.gitDir, "cat-file", "blob", "HEAD:./"+filepath.ToSlash(rel))
	return err == nil && bytes.Equal(blob, content)
}
