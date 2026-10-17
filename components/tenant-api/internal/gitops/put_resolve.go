package gitops

import (
	"context"
	"errors"
	"fmt"
	"os"

	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// PutResolution is what the tree-reading half of a tenant write decided
// (resolvePut): the file the write replaces and what it would commit there.
type PutResolution struct {
	// FilePath is the tenant's file in the tree judged.
	FilePath string
	// Existing is that file's current content; Exists is false when it does
	// not exist (Existing nil). Read only for a write with a splice: without
	// one the write does not read the file here (Existing nil, Exists false).
	Existing []byte
	Exists   bool
	// Content is what the write commits: the body, or (#3062) the splice's
	// result on Existing.
	Content string
	// Notices is validation's advisory set.
	Notices []string
}

// ErrSpliceShape marks (errors.Is) a refusal of the splice step that answers
// how the current tenant file is written (#3062): the #2405 refusal of a file
// whose sections cannot be told apart, and a SpliceFunc's own refusal that
// says so (the handler's putSpliceError: not round-trippable, the anchor
// rules for a caller the file is withheld from).
var ErrSpliceShape = errors.New("the current tenant file's shape refuses the splice")

// ErrSpliceShapeWithheld is what PreviewPut returns, instead of an
// ErrSpliceShape refusal, for a caller its withheld func says may not be
// shown the file, once nothing else would refuse the write: that caller
// learns from the preview nothing about how the file is written (#3062,
// /diff's masked preview).
var ErrSpliceShapeWithheld = errors.New("the current tenant file is withheld from this caller")

// spliceShapeText is a refusal text that is ErrSpliceShape.
type spliceShapeText string

func (s spliceShapeText) Error() string        { return string(s) }
func (s spliceShapeText) Is(target error) bool { return target == ErrSpliceShape }

// resolvePut is the part of a tenant write's refusal sequence that reads the
// tree, written ONCE (#3062): PUT's direct write runs it under the writer
// lock on the serving tree (write), its PR-mode write on the fresh base
// (prResolveOnBase), and the preview — POST /diff and POST /validate — on
// the serving tree without a lock (PreviewPut). Only the tree and its walk
// differ; the steps and their order do not:
//
//  1. resolve the tenant's file (#1673 ambiguity, #2078 placement, #2939
//     unreadable tree) on configDir with walk;
//  2. afterResolve, the caller's own step on that file (the X-DA-Base-Hash
//     precondition; PR mode's authorization on the fresh base); nil for a
//     preview;
//  3. with a splice only: read the file, write the body's declared sections
//     into it (spliceBase, #2405's bit included) and refuse a declared
//     tenant another conf.d file also declares
//     (ensureDeclaredSectionsNotElsewhere);
//  4. validate the body against the tree (validateReplacing): errs is its
//     blocking set, returned unwrapped so the dry-run can list it.
//
// err is a refusal from steps 1–3, typed as the write returns it.
//
// withheld (a preview's, nil for a write) defers step 3's ErrSpliceShape
// refusal for a caller it reports the file withheld from: steps 3 and 4 run
// on as if the body replaced the file, and only when they refuse nothing is
// the answer ErrSpliceShapeWithheld. Every other answer is then the one a
// file of any shape gets, so the shape is not told (the #2405 text in errs,
// the same bit, is dropped with it).
func (w *Writer) resolvePut(ctx context.Context, configDir, tenantID, yamlContent string,
	walk func() (*cfg.TreeScan, error), afterResolve func(filePath string) error, splice SpliceFunc,
	withheld func(existing []byte) bool,
) (res PutResolution, errs []string, err error) {
	filePath, err := w.resolveTenantFilePath(configDir, tenantID, walk)
	if err != nil {
		return res, nil, err
	}
	if afterResolve != nil {
		if err := afterResolve(filePath); err != nil {
			return res, nil, err
		}
	}
	res.FilePath = filePath
	res.Content = yamlContent
	var shape error
	if splice != nil {
		// A read error leaves existing nil: never a no-op for the writer.
		existing, rerr := readTenantFile(filePath)
		if rerr != nil && !os.IsNotExist(rerr) {
			return PutResolution{}, nil, fmt.Errorf("%w for %s: %w", errReadTenantFile, tenantID, rerr)
		}
		res.Existing, res.Exists = existing, rerr == nil
		content, err := spliceBase(ctx, existing, yamlContent, splice)
		switch {
		case err == nil:
			res.Content = content
		case errors.Is(err, ErrSpliceShape) && withheld != nil && withheld(existing):
			shape = err
		default:
			return PutResolution{}, nil, err
		}
		if err := w.ensureDeclaredSectionsNotElsewhere(walk, tenantID, yamlContent, existing, filePath); err != nil {
			return PutResolution{}, nil, err
		}
	}
	// The body is what is judged: the sections it does not declare are the
	// file's own, kept byte for byte by splice's verification, and are not
	// this write's to refuse.
	errs, res.Notices = validateReplacing(configDir, tenantID, filePath, yamlContent, replaceUnparseableAllowed(ctx), TenantNamesOf(ctx))
	if shape != nil {
		kept := errs[:0:0]
		for _, e := range errs {
			if e != MsgUnparseableBaseNeedsPlatformWrite {
				kept = append(kept, e)
			}
		}
		if len(kept) > 0 {
			return res, kept, nil
		}
		return PutResolution{}, nil, ErrSpliceShapeWithheld
	}
	return res, errs, nil
}

// PreviewPut answers, without writing, what a direct-mode WriteSpliced of
// yamlContent would do (#3062): the same refusal sequence — guardTenantID,
// the body-only pre-flight, then resolvePut — on the serving tree without
// the writer lock, the walk shared with concurrent previews
// (scanTreeForRead, through previewTenantFilePath's resolver). errs is the
// blocking validation set the write would wrap in ErrValidation (the
// pre-flight's, or else validate()'s); err a refusal before validation,
// typed as the write returns it. With neither, res is what the write would
// commit.
//
// withheld is resolvePut's: nil, or (the masked /diff) whether the file is
// withheld from the caller, whose ErrSpliceShape refusal then becomes
// ErrSpliceShapeWithheld after every other step.
//
// ctx carries what the write's would: the #2405 bit and the caller's
// TenantNames. The write is judged again, under the lock, on the tree it
// lands on.
func (w *Writer) PreviewPut(ctx context.Context, tenantID, yamlContent string, splice SpliceFunc,
	withheld func(existing []byte) bool,
) (res PutResolution, errs []string, err error) {
	if err := guardTenantID(tenantID); err != nil {
		return res, nil, err
	}
	if errs := validateBodyOnly(tenantID, yamlContent); len(errs) > 0 {
		return res, errs, nil
	}
	return w.resolvePut(ctx, w.configDir, tenantID, yamlContent, w.scanTreeForRead, nil, splice, withheld)
}
