package handler

import (
	"net/http"
	"path/filepath"

	"gopkg.in/yaml.v3"

	"github.com/vencil/tenant-api/internal/confd"
	"github.com/vencil/tenant-api/internal/gitops"
	"github.com/vencil/tenant-api/internal/rbac"
	cfg "github.com/vencil/threshold-exporter/pkg/config"
)

// fileShown reports whether the caller of r may be shown what is read off a
// whole conf.d file, data (#3050): its text (GET raw_yaml, the /diff
// preview), anything decoded from it that an alias or a decode error can
// carry from elsewhere in it (custom_alerts, the /effective decode error),
// and its source hash (which confirms a guess of the file offline). A file
// can declare several tenants, so it is shown only when the caller may read
// every tenant it declares — read by the one rule a message to the caller
// may name a tenant by, tenantNamesFor (#3039, #1530). own is the tenant the
// request is about, whose read the route's middleware already judged: it is
// not asked again (a second AllowedInOrgRead would count a second would-deny
// in shadow mode).
//
// ⛔ NO "ONLY MY SECTION". The file is shown whole or not at all: a re-encode
// of one section is not the file (anchors, aliases and block scalars do not
// survive it faithfully, and its hash is no longer source_hash).
func fileShown(r *http.Request, d *Deps, data []byte, own string) bool {
	p := rbac.RequestPrincipal(r)
	return fileTenantsShown(data, own, tenantNamesFor(d.RBAC, d.TenantOrg, p), func() bool {
		return d.RBAC == nil || d.RBAC.PlatformUnrestricted(p, rbac.PermRead)
	})
}

// fileTenantsShown is fileShown's decision on its inputs. names is the
// caller's tenantNamesFor; nil shows nothing (fail closed, as
// gitops.TenantNames.Shown). When the declared tenants cannot be listed
// with certainty (declaredTenantIDs), only a caller who may read every
// tenant — unrestricted: no RBAC, or a rule granting read on "*" with no
// org-scope, environments or domains (rbac.PlatformUnrestricted) — is shown
// the file. Asked lazily, so a file whose tenants are listed costs no rule
// walk beyond names.
func fileTenantsShown(data []byte, own string, names gitops.TenantNames, unrestricted func() bool) bool {
	if names == nil {
		return false
	}
	ids, ok := declaredTenantIDs(data)
	if !ok {
		return unrestricted()
	}
	for _, id := range ids {
		if id != own && !names.Shown(id) {
			return false
		}
	}
	return true
}

// declaredTenantIDs lists the keys of every document's top-level `tenants:`
// mapping — a superset of what threshold-exporter decodes, which reads only
// the first document — each id once. ok is false when the list cannot be
// stated with certainty: not YAML, a document whose root is not a mapping,
// or an alias, a merge key or a non-scalar key at the top level or in
// `tenants:` (through any of them a tenant can be declared without its id
// being a plain key there). An empty file declares none.
func declaredTenantIDs(data []byte) (ids []string, ok bool) {
	docs, err := parseAllDocs(data)
	if err != nil {
		return nil, false
	}
	seen := map[string]bool{}
	for _, doc := range docs {
		if doc.Kind != yaml.DocumentNode || len(doc.Content) == 0 {
			continue
		}
		root := doc.Content[0]
		if isYAMLNull(root) {
			continue
		}
		if root.Kind != yaml.MappingNode {
			return nil, false
		}
		for i := 0; i+1 < len(root.Content); i += 2 {
			k, v := root.Content[i], root.Content[i+1]
			if !isPlainKey(k) {
				return nil, false
			}
			if k.Value != "tenants" {
				continue
			}
			if isYAMLNull(v) {
				continue
			}
			if v.Kind != yaml.MappingNode {
				return nil, false
			}
			for j := 0; j+1 < len(v.Content); j += 2 {
				tk := v.Content[j]
				if !isPlainKey(tk) {
					return nil, false
				}
				if !seen[tk.Value] {
					seen[tk.Value] = true
					ids = append(ids, tk.Value)
				}
			}
		}
	}
	return ids, true
}

// isPlainKey reports whether k is a mapping key that names itself: a scalar
// that is not a merge key (`<<`, however tagged).
func isPlainKey(k *yaml.Node) bool {
	return k.Kind == yaml.ScalarNode && k.Tag != "!!merge" && k.Value != "<<"
}

// isYAMLNull reports whether n is an empty or null scalar.
func isYAMLNull(n *yaml.Node) bool {
	return n.Kind == yaml.ScalarNode && n.Tag == "!!null"
}

// confdFileShown is fileShown for the conf.d file at rel (relative to
// d.ConfigDir, as cfg.EffectiveConfig.SourceFile and cfg.DecodeError.Path
// name it).
func confdFileShown(r *http.Request, d *Deps, rel, own, hash string) bool {
	return pathShown(r, d, filepath.Join(d.ConfigDir, filepath.FromSlash(rel)), own, hash)
}

// tenantFileShown is fileShown for the tenant's own file as it is now,
// resolved as the write that hashed it resolves it (Writer.PreviewCurrent;
// confd.ResolveTenantFile under d.ConfigDir without a Writer). hash, when not
// empty, must be the source hash of the bytes read (see pathShown). A file
// that cannot be read, or does not exist, is not shown (fail closed).
func tenantFileShown(r *http.Request, d *Deps, tenantID, hash string) bool {
	if d.Writer == nil {
		path, err := confd.ResolveTenantFile(d.ConfigDir, tenantID)
		if err != nil {
			return false
		}
		return pathShown(r, d, path, tenantID, hash)
	}
	data, exists, err := d.Writer.PreviewCurrent(tenantID)
	if err != nil || !exists {
		return false
	}
	return bytesShown(r, d, data, tenantID, hash)
}

// pathShown is fileShown for the file at path, read now. A file that cannot
// be read is not shown (fail closed). hash, when not empty, must be the
// source hash of the bytes read: the decision is then about the very bytes
// that hash describes, not a later version of the file.
func pathShown(r *http.Request, d *Deps, path, own, hash string) bool {
	data, err := confd.ReadRegularFile(path)
	if err != nil {
		return false
	}
	return bytesShown(r, d, data, own, hash)
}

// bytesShown is fileShown, first requiring that hash (when not empty) is the
// source hash of data.
func bytesShown(r *http.Request, d *Deps, data []byte, own, hash string) bool {
	if hash != "" && cfg.ComputeSourceHash(data) != hash {
		return false
	}
	return fileShown(r, d, data, own)
}
