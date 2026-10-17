package gitops

import (
	"context"
	"errors"
	"fmt"
	"os"

	cfg "github.com/vencil/threshold-exporter/pkg/config"

	"github.com/vencil/tenant-api/internal/confd"
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
	// Content is what the write commits: the body, as sent (#3064: the
	// file is the tenant's alone, so the body replaces it whole).
	Content string
	// Notices is validation's advisory set.
	Notices []string
}

// ErrTenantGitManaged marks (errors.Is) a write refused because the tenant
// is git-managed (#3064): its file is not API-shaped
// (confd.TenantFileManagement) — it is shared with other tenants, declares a
// tenant other than its name, or declares tenants through YAML indirection —
// so tenant-api neither shows it nor writes it.
var ErrTenantGitManaged = errors.New("the tenant is git-managed")

// GitManagedError is a write refused for a git-managed tenant (#3064).
// Reason is confd's fixed reason code; the text names the tenant the write is
// for and nothing else of the file.
type GitManagedError struct {
	TenantID string
	Reason   string
}

func (e *GitManagedError) Error() string {
	return fmt.Sprintf("tenant %s is git-managed (%s): its config file is not one tenant-api writes "+
		"(one YAML document declaring only this tenant, under the file's own name, without anchors, aliases, "+
		"merge keys or tagged keys at the top level or directly under tenants:), so it is changed "+
		"only through git; nothing was written", e.TenantID, e.Reason)
}

// Is makes a GitManagedError ErrTenantGitManaged.
func (e *GitManagedError) Is(target error) bool { return target == ErrTenantGitManaged }

// CheckAPIManaged is the #3064 refusal for a tenant file whose current
// content is existing (nil or blank: no file yet, nothing to refuse).
func CheckAPIManaged(tenantID string, existing []byte) error {
	if reason := confd.TenantFileManagement(existing, tenantID); reason != "" {
		return &GitManagedError{TenantID: tenantID, Reason: reason}
	}
	return nil
}

// resolvePut is the part of a tenant write's refusal sequence that reads the
// tree, written ONCE (#3062): PUT's direct write runs it under the writer
// lock on the serving tree (write), its PR-mode write on the fresh base
// (prResolveOnBase), and the preview — POST /diff and POST /validate — on
// the serving tree without a lock (PreviewPut). Only the tree and its walk
// differ; the steps and their order do not:
//
//  1. resolve the tenant's file (#1673 ambiguity, #2078 placement, #2939
//     unreadable tree) on configDir with walk;
//  2. read it and refuse a git-managed tenant (#3064, CheckAPIManaged) —
//     before step 3, so the X-DA-Base-Hash precondition never answers about
//     a file tenant-api does not show (CONFLICT versus TENANT_GIT_MANAGED
//     would confirm a guessed hash of a shared file);
//  3. afterResolve, the caller's own step on that file (the X-DA-Base-Hash
//     precondition; PR mode's authorization on the fresh base); nil for a
//     preview;
//  4. validate the body against the tree (validateReplacing): errs is its
//     blocking set, returned unwrapped so the dry-run can list it.
//
// err is a refusal from steps 1–3, typed as the write returns it.
func (w *Writer) resolvePut(ctx context.Context, configDir, tenantID, yamlContent string,
	walk func() (*cfg.TreeScan, error), afterResolve func(filePath string) error,
) (res PutResolution, errs []string, err error) {
	filePath, err := w.resolveTenantFilePath(configDir, tenantID, walk)
	if err != nil {
		return res, nil, err
	}
	// A read error leaves existing nil: never a no-op for the writer.
	existing, rerr := readTenantFile(filePath)
	if rerr != nil && !os.IsNotExist(rerr) {
		return res, nil, fmt.Errorf("%w for %s: %w", errReadTenantFile, tenantID, rerr)
	}
	if err := CheckAPIManaged(tenantID, existing); err != nil {
		return res, nil, err
	}
	if afterResolve != nil {
		if err := afterResolve(filePath); err != nil {
			return res, nil, err
		}
	}
	res.FilePath = filePath
	res.Existing, res.Exists = existing, rerr == nil
	res.Content = yamlContent
	errs, res.Notices = validateReplacing(configDir, tenantID, filePath, yamlContent, replaceUnparseableAllowed(ctx), TenantNamesOf(ctx))
	return res, errs, nil
}

// PreviewPut answers, without writing, what a direct-mode Write of
// yamlContent would do: the same refusal sequence — guardTenantID, the
// body-only pre-flight, then resolvePut — on the serving tree without the
// writer lock, the walk shared with concurrent previews (scanTreeForRead,
// through previewTenantFilePath's resolver). errs is the blocking validation
// set the write would wrap in ErrValidation (the pre-flight's, or else
// validate()'s); err a refusal before validation, typed as the write returns
// it. With neither, res is what the write would commit.
//
// ctx carries what the write's would: the #2405 bit and the caller's
// TenantNames. The write is judged again, under the lock, on the tree it
// lands on.
func (w *Writer) PreviewPut(ctx context.Context, tenantID, yamlContent string) (res PutResolution, errs []string, err error) {
	if err := guardTenantID(tenantID); err != nil {
		return res, nil, err
	}
	if errs := validateBodyOnly(tenantID, yamlContent); len(errs) > 0 {
		return res, errs, nil
	}
	return w.resolvePut(ctx, w.configDir, tenantID, yamlContent, w.scanTreeForRead, nil)
}
