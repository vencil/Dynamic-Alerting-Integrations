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
	"path"
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

// sampleJudgedBase reads HEAD as the commit a single-tenant write is judged
// on and commits onto: the serving HEAD (direct mode) or the checked-out
// feature branch's HEAD — its branch point (PR mode, before anything is
// committed on it). Caller holds w.mu.
//
// ⛔ THE DIRECT WRITES CALL IT BEFORE THEY READ THE TENANT FILE (#1535 item
// 6), as WriteBatch samples headBefore before it copies the serving tree.
// Sampled after the reads, a commit made from outside the process between
// the read (the base_hash precondition, the merge base) and the sample
// became the judged base itself: the compare-and-swap then passed and the
// write overwrote that commit with 200. Sampled first, such a commit moves
// HEAD off the judged base and commitFileChange refuses with ErrConflict.
//
// What this does NOT cover: the reads are of the serving worktree, and the
// worktree is not compared with the judged base's blob — an uncommitted
// edit there, or a HEAD advanced without updating the worktree, is read as
// the current file (a deliberate choice, #1535 item 6). The no-op rules
// (unchangedFile, the batch's) do compare with the base's blob.
func (w *Writer) sampleJudgedBase() (string, error) {
	base, err := w.currentHEAD()
	if err != nil {
		return "", fmt.Errorf("%w: %v", errValidatorBase, err)
	}
	if w.afterBaseSample != nil {
		w.afterBaseSample()
	}
	return base, nil
}

// CheckHEAD reports whether the Writer's git dir has a readable HEAD — the
// commit every tenant write samples first (sampleJudgedBase). When it does
// not (conf.d is not a git repository, or HEAD is broken), every write
// fails there with a server error, before any verdict on its body. For a
// startup warning.
func (w *Writer) CheckHEAD() error {
	_, err := w.currentHEAD()
	return err
}

// validateTenantFile is validateProposed for one tenant file under
// w.configDir, against base — the commit sampleJudgedBase returned. Caller
// holds w.mu.
//
// The direct-mode caller hands base to commitFileChange, which commits only
// while HEAD is still base: w.mu keeps every in-process write out, but not a
// commit made from outside the process since the sample.
func (w *Writer) validateTenantFile(ctx context.Context, base, filePath, tenantID string, content []byte) error {
	if w.afterTenantRead != nil {
		w.afterTenantRead()
	}
	if w.validator == nil {
		return ErrNoValidator
	}
	return w.validateProposed(ctx, base, []ProposedFile{{
		Path: confRelKey(w.configDir, filePath), Content: content, Tenants: []string{tenantID},
	}})
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
// byte-identical to the serving file (existing; nil when there is none)
// and base (the judged base, HEAD) stores exactly that as its blob of it
// (storesAs: attribute-aware). The
// serving file alone can hold bytes HEAD does not — an uncommitted edit
// from outside, or a write whose file landed and whose commit failed
// (index.lock) — and answering success for those would report a body that
// is never committed. When it holds, the write is a success with nothing
// committed and the generator not asked (the tree it would judge is
// base's own). The batch's no-op rule is the same one (applyBatchOp).
func (w *Writer) unchangedFile(base, filePath string, existing []byte, content string) bool {
	return existing != nil && string(existing) == content && w.storesAs(w.gitDir, base, filePath, existing)
}

// storesAs reports whether content, committed at filePath (a path in the
// worktree at dir), would be stored as exactly the blob rev holds for the
// file a read of filePath sees (revFile: a symlink followed) — false when
// rev has no such file or either side cannot be read.
//
// ⛔ ATTRIBUTE-AWARE, NOT A BYTE COMPARE (#1535 item 6): with a
// .gitattributes rule (`text eol=crlf`, a clean filter…) the worktree
// holds other bytes than the blob, and a byte compare called an unchanged
// file changed — the write then went on to stage it, and the staged blob
// differed from the judged bytes (checkStagedAsValidated refused it).
// `hash-object --path` (without -w: nothing is written to the object
// store) applies the attributes of that path, as staging it would.
func (w *Writer) storesAs(dir, rev, filePath string, content []byte) bool {
	top, rel, oid, err := w.revFile(dir, rev, filePath)
	if err != nil || oid == "" {
		return false
	}
	cmd, _, cancel := w.gitCmd("--no-optional-locks", "-C", top, "hash-object", "--path="+rel, "--stdin")
	defer cancel()
	cmd.Stdin = bytes.NewReader(content)
	got, err := cmd.Output()
	return err == nil && strings.TrimSpace(string(got)) == oid
}

// revBlob is rev's blob of the file a read of filePath (a path in the
// worktree at dir) sees (revFile), nil when rev has no such file. Any
// failure to tell is an error, never "no such file".
func (w *Writer) revBlob(dir, rev, filePath string) ([]byte, error) {
	_, _, oid, err := w.revFile(dir, rev, filePath)
	if err != nil || oid == "" {
		return nil, err
	}
	blob, err := w.gitOutput(dir, "cat-file", "blob", oid)
	if err != nil {
		return nil, fmt.Errorf("read %s's blob: %w", rev, err)
	}
	return blob, nil
}

// maxRevSymlinkHops bounds revFile's symlink chain (a loop is "no file").
const maxRevSymlinkHops = 8

// revFile finds, in rev, the regular file a read of filePath (a path in
// the worktree at dir) would see: filePath's own entry, or — when rev
// holds a symlink there (a tenant file linked to a shared one) — the entry
// it points to, followed as the worktree follows it. top is the
// repository's top level and rel that file's path from it; oid is "" when
// rev has no such regular file (missing, a link leaving the repository or
// looping, a directory). An error is a failure to tell.
func (w *Writer) revFile(dir, rev, filePath string) (top, rel, oid string, err error) {
	out, err := w.gitOutput(dir, "rev-parse", "--show-toplevel")
	if err != nil {
		return "", "", "", fmt.Errorf("locate the repository: %w", err)
	}
	top = strings.TrimSpace(string(out))
	r, err := filepath.Rel(top, filePath)
	if err != nil || r == ".." || strings.HasPrefix(r, ".."+string(filepath.Separator)) {
		return top, "", "", nil
	}
	rel = filepath.ToSlash(r)
	for hop := 0; hop <= maxRevSymlinkHops; hop++ {
		listed, err := w.gitOutput(top, "ls-tree", "-z", "--full-tree", rev, "--", rel)
		if err != nil {
			return top, rel, "", fmt.Errorf("read %s's entry: %w", rev, err)
		}
		meta, _, found := strings.Cut(strings.TrimSuffix(string(listed), "\x00"), "\t")
		fields := strings.Fields(meta)
		if !found || len(fields) != 3 || fields[1] != "blob" {
			return top, rel, "", nil
		}
		if fields[0] != "120000" {
			return top, rel, fields[2], nil
		}
		target, err := w.gitOutput(top, "cat-file", "blob", fields[2])
		if err != nil {
			return top, rel, "", fmt.Errorf("read %s's link: %w", rev, err)
		}
		next := path.Clean(path.Join(path.Dir(rel), string(target)))
		if path.IsAbs(string(target)) || next == ".." || strings.HasPrefix(next, "../") {
			return top, rel, "", nil
		}
		rel = next
	}
	return top, rel, "", nil
}
