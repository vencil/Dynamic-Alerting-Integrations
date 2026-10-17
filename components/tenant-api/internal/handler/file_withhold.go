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
// whole conf.d file, data (#3050), by GET /tenants/{id}/effective: a decode
// error that can quote a value of any tenant the file declares — the root
// platform files and the defaults chain included, which legitimately hold
// several tenants. (/effective's source_hash no longer asks: since #3064 it
// is shown only for an API-shaped tenant file, tenantFileAPIManaged.)
//
// GET /tenants/{id} and the writes no longer ask (#3064): they show and
// write only an API-shaped tenant file (confd.TenantFileManagement), which
// holds the tenant's own content and nothing else, and refuse every other
// shape (managed_by: git, TENANT_GIT_MANAGED). A file
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
// or, at the top level or in `tenants:`, an alias, a merge key, a non-scalar
// key or a key with an explicit tag other than !!str (through any of them a
// tenant can be declared without its id being a plain key there: yaml.v3
// decodes `? !!binary dGVuYW50cw==` to the key "tenants"). An empty file
// declares none.
//
// ⛔ AN ID IS WHAT THE EXPORTER'S DECODER READS, not the node's text: each key
// is decoded into a string as the typed decode into map[string] does, and
// the first document's ids from cfg.ParseTenantFile itself are added, so the
// list is never smaller than the exporter's (#3050 round 2).
func declaredTenantIDs(data []byte) (ids []string, ok bool) {
	docs, err := parseAllDocs(data)
	if err != nil {
		return nil, false
	}
	seen := map[string]bool{}
	add := func(id string) {
		if !seen[id] {
			seen[id] = true
			ids = append(ids, id)
		}
	}
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
			name, plain := plainKey(k)
			if !plain {
				return nil, false
			}
			if name != "tenants" {
				continue
			}
			if isYAMLNull(v) {
				continue
			}
			if v.Kind != yaml.MappingNode {
				return nil, false
			}
			for j := 0; j+1 < len(v.Content); j += 2 {
				id, plain := plainKey(v.Content[j])
				if !plain {
					return nil, false
				}
				add(id)
			}
		}
	}
	// The authority on the first document: whatever the exporter's own
	// parse declares is declared, whatever this walk read.
	if c, err := cfg.ParseTenantFile(data); err == nil {
		for id := range c.Tenants {
			add(id)
		}
	}
	return ids, true
}

// plainKey returns the string a mapping key decodes to, and whether k names
// itself: a scalar with no explicit tag other than !!str that is not a merge
// key (`<<`, however tagged) and decodes into a string.
func plainKey(k *yaml.Node) (string, bool) {
	if k.Kind != yaml.ScalarNode || k.Tag == "!!merge" || k.Value == "<<" {
		return "", false
	}
	if k.Style&yaml.TaggedStyle != 0 && k.Tag != "!!str" {
		return "", false
	}
	var s string
	if err := k.Decode(&s); err != nil {
		return "", false
	}
	return s, true
}

// isYAMLNull reports whether n is an empty or null scalar.
func isYAMLNull(n *yaml.Node) bool {
	return n.Kind == yaml.ScalarNode && n.Tag == "!!null"
}

// confdFileShown is fileShown for the conf.d file at rel (relative to
// d.ConfigDir, as cfg.DecodeError.Path names it), read now. A file that
// cannot be read is not shown (fail closed).
func confdFileShown(r *http.Request, d *Deps, rel, own string) bool {
	data, err := confd.ReadRegularFile(filepath.Join(d.ConfigDir, filepath.FromSlash(rel)))
	if err != nil {
		return false
	}
	return fileShown(r, d, data, own)
}

// tenantFileAPIManaged reports whether the tenant's own file, as it is now,
// is API-shaped (#3064, confd.TenantFileManagement) and hashes to hash — so a
// 409 CONFLICT may carry it as current_source_hash, and /effective may show it
// as source_hash: GET /tenants/{id} shows that hash. Resolved as the write that hashed it resolves it
// (Writer.PreviewCurrent; confd.ResolveTenantFile under d.ConfigDir without a
// Writer). A file that cannot be read, or does not exist, is not (fail
// closed).
func tenantFileAPIManaged(d *Deps, tenantID, hash string) bool {
	var data []byte
	if d.Writer == nil {
		path, err := confd.ResolveTenantFile(d.ConfigDir, tenantID)
		if err != nil {
			return false
		}
		if data, err = confd.ReadRegularFile(path); err != nil {
			return false
		}
	} else {
		var exists bool
		var err error
		if data, exists, err = d.Writer.PreviewCurrent(tenantID); err != nil || !exists {
			return false
		}
	}
	return cfg.ComputeSourceHash(data) == hash && confd.TenantFileManagement(data, tenantID) == ""
}
