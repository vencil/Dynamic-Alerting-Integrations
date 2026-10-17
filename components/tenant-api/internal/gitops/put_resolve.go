package gitops

import (
	"context"
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
	// not exist (Existing nil).
	Existing []byte
	Exists   bool
	// Content is what the write commits: the body, or (#3062) the splice's
	// result on Existing.
	Content string
	// Notices is validation's advisory set.
	Notices []string
}

// resolvePut is the part of a tenant write's refusal sequence that reads the
// tree, written ONCE (#3062): PUT's direct write runs it under the writer
// lock on the serving tree (write), its PR-mode write on the fresh base
// (prResolveOnBase), and the previews — POST /diff and POST /validate — on
// the serving tree without a lock (PreviewPut, DryRunValidate). Only the
// tree and its walk differ; the steps and their order do not:
//
//  1. resolve the tenant's file (#1673 ambiguity, #2078 placement, #2939
//     unreadable tree) on configDir with walk;
//  2. afterResolve, the caller's own step on that file (the X-DA-Base-Hash
//     precondition; PR mode's authorization on the fresh base); nil for a
//     preview;
//  3. read the file, and with a splice: write the body's declared sections
//     into it (spliceBase, #2405's bit included) and refuse a declared tenant
//     another conf.d file also declares (ensureDeclaredSectionsNotElsewhere);
//  4. validate the body against the tree (validateReplacing): errs is its
//     blocking set, returned unwrapped so the dry-run can list it.
//
// err is a refusal from steps 1–3, typed as the write returns it.
func (w *Writer) resolvePut(ctx context.Context, configDir, tenantID, yamlContent string,
	walk func() (*cfg.TreeScan, error), afterResolve func(filePath string) error, splice SpliceFunc,
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
	// A read error leaves existing nil: never a no-op for the writer.
	existing, rerr := readTenantFile(filePath)
	content := yamlContent
	if splice != nil {
		if rerr != nil && !os.IsNotExist(rerr) {
			return res, nil, fmt.Errorf("%w for %s: %w", errReadTenantFile, tenantID, rerr)
		}
		if content, err = spliceBase(ctx, existing, yamlContent, splice); err != nil {
			return res, nil, err
		}
		if err := w.ensureDeclaredSectionsNotElsewhere(walk, tenantID, yamlContent, existing, filePath); err != nil {
			return res, nil, err
		}
	}
	// The body is what is judged: the sections it does not declare are the
	// file's own, kept byte for byte by splice's verification, and are not
	// this write's to refuse.
	errs, notices := validateReplacing(configDir, tenantID, filePath, yamlContent, replaceUnparseableAllowed(ctx), TenantNamesOf(ctx))
	return PutResolution{
		FilePath: filePath,
		Existing: existing,
		Exists:   rerr == nil,
		Content:  content,
		Notices:  notices,
	}, errs, nil
}

// PreviewPut answers, without writing, what a direct-mode WriteSpliced of
// yamlContent would do (#3062): the same refusal sequence — guardTenantID,
// the body-only pre-flight, then resolvePut — on the serving tree without
// the writer lock, the walk shared with concurrent previews
// (scanTreeForRead, as DryRunValidate). errs is the blocking validation set
// the write would wrap in ErrValidation (the pre-flight's, or else
// validate()'s); err a refusal before validation, typed as the write returns
// it. With neither, res is what the write would commit.
//
// ctx carries what the write's would: the #2405 bit and the caller's
// TenantNames. The write is judged again, under the lock, on the tree it
// lands on.
func (w *Writer) PreviewPut(ctx context.Context, tenantID, yamlContent string, splice SpliceFunc) (res PutResolution, errs []string, err error) {
	if err := guardTenantID(tenantID); err != nil {
		return res, nil, err
	}
	if errs := validateBodyOnly(tenantID, yamlContent); len(errs) > 0 {
		return res, errs, nil
	}
	return w.resolvePut(ctx, w.configDir, tenantID, yamlContent, w.scanTreeForRead, nil, splice)
}
