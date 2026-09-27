package gitops

import (
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"

	cfg "github.com/vencil/threshold-exporter/pkg/config"

	"github.com/vencil/tenant-api/internal/confd"
)

// batchTree is WritePRBatch's view of conf.d for the #2078 guard (#2153):
// ONE walk per batch plus what the batch itself wrote since, instead of one
// walk per op.
//
// Why this is the same answer as walking again before every op: under w.mu
// nothing in-process changes the tree but the batch's own os.WriteFile
// calls, and each of those replaces exactly one top-level regular file
// (recordWrite falls back to a fresh walk for anything else). So the tree
// an op lands on is the walked tree with those files' declarations replaced
// by the declarations of the bytes written there — derived with the same
// decode the walker uses (cfg.ParseConfigFile, the call inside the walker's
// parseTenantDecls): a file that fails to decode, or a `_`-prefixed or
// non-scanned name, declares nothing.
//
// Locate's verdict classes are reproduced exactly: no declaring file → not
// found; one → that file; two or more → *cfg.DuplicateTenantError. Which two
// files a duplicate names can differ from a fresh walk's (walk order vs the
// walk's sorted Keys); that text reaches the server log only.
type batchTree struct {
	scan *cfg.TreeScan // nil → walk on the next op
	// written maps the walker-form path of every file this batch wrote to
	// the sorted tenant ids its new bytes declare; order is first-write order.
	written map[string][]string
	order   []string
	// declared indexes scan: tenant id → declaring files (AbsPath), built
	// on the first op that needs it after a write.
	declared map[string][]string
}

// tenantFilePath is Writer.tenantFilePath for one op of a batch.
func (b *batchTree) tenantFilePath(w *Writer, tenantID string) (string, error) {
	path, err := confd.TenantFilePathForWrite(w.configDir, tenantID)
	if err != nil {
		return "", err
	}
	if b.scan == nil {
		scan, serr := w.scanTree()
		if serr != nil {
			return "", fmt.Errorf("%w: tenant %s: %w", ErrTenantTreeScan, tenantID, serr)
		}
		*b = batchTree{scan: scan}
	}
	located, lerr := b.locate(tenantID)
	if err := declaredElsewhereVerdict(tenantID, path, b.scan.AbsRoot, located, lerr); err != nil {
		return "", err
	}
	return path, nil
}

// locate answers like TreeScan.Locate on the walked tree after this batch's
// writes.
func (b *batchTree) locate(tenantID string) (string, error) {
	if len(b.written) == 0 {
		return b.scan.Locate(tenantID)
	}
	if b.declared == nil {
		b.declared = make(map[string][]string)
		for _, k := range b.scan.Keys {
			f := b.scan.Files[k]
			for _, id := range f.TenantIDs {
				b.declared[id] = append(b.declared[id], f.AbsPath)
			}
		}
	}
	var files []string
	for _, p := range b.declared[tenantID] {
		if _, replaced := b.written[p]; !replaced {
			files = append(files, p)
		}
	}
	for _, p := range b.order {
		ids := b.written[p]
		if i := sort.SearchStrings(ids, tenantID); i < len(ids) && ids[i] == tenantID {
			files = append(files, p)
		}
	}
	switch len(files) {
	case 0:
		return "", cfg.ErrTenantNotFound
	case 1:
		return files[0], nil
	default:
		return "", &cfg.DuplicateTenantError{TenantID: tenantID, PathA: files[0], PathB: files[1]}
	}
}

// recordWrite registers that the batch wrote content to filePath (a path
// tenantFilePath returned). A path that is not a regular file after the
// write — a symlink, whose write landed in some other file — or that cannot
// be statted drops the walk, so the next op walks again.
func (b *batchTree) recordWrite(filePath string, content []byte) {
	if b.scan == nil {
		return
	}
	if fi, err := os.Lstat(filePath); err != nil || !fi.Mode().IsRegular() {
		*b = batchTree{}
		return
	}
	base := filepath.Base(filePath)
	var ids []string
	if cfg.IsScannedFileName(base) && !strings.HasPrefix(base, "_") {
		if parsed, err := cfg.ParseConfigFile(content); err == nil {
			for id := range parsed.Tenants {
				ids = append(ids, id)
			}
			sort.Strings(ids)
		}
	}
	p := filepath.Join(b.scan.AbsRoot, base)
	if b.written == nil {
		b.written = make(map[string][]string)
	}
	if _, seen := b.written[p]; !seen {
		b.order = append(b.order, p)
	}
	b.written[p] = ids
}
