package main

// tenant_subset.go — `--tenant`, the subset mode of served-values and
// effective (ADR-038 §5, ADR-036 §3): the whole tree is loaded as always —
// every platform file, every file's parse verdict, the duplicate-tenant
// refusal — and only the named tenants are computed and printed. The
// tenant-api generator container judges one write by the tenants it
// touches; computing every tenant of a large tree for that does not fit its
// budget.
//
// What a tenant's entry says in a subset document is what it says in the
// whole-tree document, with one difference: whether /metrics can be
// gathered is judged over the named tenants only. A series carries its
// tenant's label, so a failure to gather is always one tenant's; another
// tenant's failure (which takes the whole /metrics down in production) is
// not seen, nor are the error segments it puts in every tenant's
// --schedules. A caller judging a write names every tenant the written
// file declares, before and after the write.

import (
	"errors"
	"sort"
	"strings"

	"github.com/vencil/threshold-exporter/pkg/config"
)

// tenantList is a repeatable --tenant.
type tenantList []string

func (l *tenantList) String() string { return strings.Join(*l, ",") }
func (l *tenantList) Set(v string) error {
	if v == "" {
		return errors.New("must not be empty")
	}
	*l = append(*l, v)
	return nil
}

// tenantSubset is the subset fields of a document, both present exactly in
// subset mode: Requested, the --tenant values sorted and deduplicated, and
// Absent, those the tree has no such tenant for — the tree does not declare
// it, or declares it only in a file the load skips or cannot read, or where
// the walk does not read (a hidden file, a root platform file's entry the
// exporter drops) — never a da-guard failure ([] when none). Every
// requested tenant is in the document's tenants or in Absent.
type tenantSubset struct {
	Requested *[]string `json:"tenants_requested,omitempty"`
	Absent    *[]string `json:"tenants_absent,omitempty"`
}

// requested is the --tenant values sorted and deduplicated; nil when none
// was given (the whole tree).
func (l tenantList) requested() []string {
	if len(l) == 0 {
		return nil
	}
	seen := map[string]bool{}
	out := make([]string, 0, len(l))
	for _, t := range l {
		if !seen[t] {
			seen[t] = true
			out = append(out, t)
		}
	}
	sort.Strings(out)
	return out
}

// subset is the document fields for the requested tenants, given the
// tenants computed: Absent is every requested tenant not among them.
func subset(requested []string, computed func(string) bool) tenantSubset {
	if requested == nil {
		return tenantSubset{}
	}
	req := append([]string(nil), requested...)
	absent := []string{}
	for _, t := range requested {
		if !computed(t) {
			absent = append(absent, t)
		}
	}
	return tenantSubset{Requested: &req, Absent: &absent}
}

// subsetConfig is cfg holding only the requested tenants it declares
// (onlyTenants: every platform field is cfg's own); cfg itself when
// requested is nil.
func subsetConfig(cfg *config.ThresholdConfig, requested []string) *config.ThresholdConfig {
	if requested == nil {
		return cfg
	}
	present := make([]string, 0, len(requested))
	for _, t := range requested {
		if _, ok := cfg.Tenants[t]; ok {
			present = append(present, t)
		}
	}
	return onlyTenants(cfg, present)
}
