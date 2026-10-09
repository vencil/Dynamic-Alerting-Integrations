package config

// RejectedValuesShown: which tenants are shown a subtree defaults value the
// build refused (#2065, the exporter's value_rejected count).

import (
	"hash/fnv"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// RejectedValuesShown is, per tenant, each key the tenant's effective config
// names NotServedValueRejected, and the file of the value shown — the
// verdict `/effective`, `da-guard effective` and da-guard's gate give, from
// the same resolver (effectiveResolver.resolve → servedVerdicts.notServed,
// keySources' winner attribution). Nothing is re-judged here: a tenant is
// resolved when its defaults chain holds a file of built.RejectedChainValues
// (no other tenant can be named value_rejected), and the reason is read off
// the result. Keys are spelled as effective_config writes them.
//
// scan is the scan built came from; a file a warm scan did not cache is read
// from disk and used only when its SHA-256 is the scan's, so a tenant whose
// files moved since the scan is skipped (the change schedules the next
// reload, whose commit answers it). A tenant that does not resolve is
// skipped too, as /effective fails for it. nil when there is none.
func RejectedValuesShown(scan *TreeScan, built *FlatBuild) map[string]map[string]string {
	var c RejectedShownCache
	return c.Shown(scan, built)
}

// RejectedShownCache is RejectedValuesShown across commits: a candidate
// tenant is resolved again only when an input of its resolve moved. The
// input fingerprint is the scan's SHA-256 of every file the resolve reads
// for it — the tenant file, each file of its defaults chain (in order, by
// path) and every `_` file at the root (platform `tenants:` entries,
// profiles, the root carrier) — plus the tenant id. The build's tables the
// value_rejected verdict reads (RejectedChainValues, ParseFailed of a chain
// file) are functions of those files' bytes: a chain file's refused keys
// are its values that are not threshold-shaped, recorded for any tenant that
// does not set the key itself, and the verdict reads them only where that
// file is the tenant's winning layer. The zero value is ready; safe for
// concurrent use.
type RejectedShownCache struct {
	mu      sync.Mutex
	entries map[string]rejectedShownEntry
	// Resolves counts the effective resolves Shown ran, for tests.
	Resolves int
}

type rejectedShownEntry struct {
	input uint64
	keys  map[string]string // nil: resolved, nothing value_rejected; also nil when the resolve failed
}

// Shown is RejectedValuesShown(scan, built), reusing the previous call's
// answer for a candidate whose inputs did not move.
func (c *RejectedShownCache) Shown(scan *TreeScan, built *FlatBuild) map[string]map[string]string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if scan == nil || built == nil || len(built.RejectedChainValues) == 0 || len(scan.Tenants) == 0 {
		c.entries = nil
		return nil
	}
	r := newEffectiveResolver(scan)
	var rootFiles []string
	for _, k := range scan.Keys {
		if !strings.Contains(k, "/") && isPlatformKey(k) {
			rootFiles = append(rootFiles, k)
		}
	}
	ids := make([]string, 0, len(scan.Tenants))
	for id := range scan.Tenants {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	next := make(map[string]rejectedShownEntry)
	var out map[string]map[string]string
	served := false
	for _, id := range ids {
		abs := scan.Tenants[id]
		chain := r.chain(filepath.Dir(abs))
		candidate := false
		for _, p := range chain {
			if len(built.RejectedChainValues[r.rel(p)]) > 0 {
				candidate = true
				break
			}
		}
		if !candidate {
			continue
		}
		in := c.input(scan, r, id, abs, chain, rootFiles)
		e, ok := c.entries[id]
		if !ok || e.input != in {
			if !served {
				r.readUncached = true
				r.served = newServedVerdicts(built, false)
				r.served.noUnparsed = true
				served = true
			}
			e = rejectedShownEntry{input: in, keys: resolveRejected(r, id)}
			c.Resolves++
		}
		next[id] = e
		if len(e.keys) > 0 {
			if out == nil {
				out = map[string]map[string]string{}
			}
			out[id] = e.keys
		}
	}
	c.entries = next
	return out
}

// input is the fingerprint of tenant id's resolve inputs (see the type).
func (c *RejectedShownCache) input(scan *TreeScan, r *effectiveResolver, id, abs string, chain, rootFiles []string) uint64 {
	h := fnv.New64a()
	add := func(s string) {
		_, _ = h.Write([]byte(s))
		_, _ = h.Write([]byte{0})
	}
	file := func(rel string) {
		add(rel)
		if f, ok := scan.Files[rel]; ok {
			add(f.Hash)
		} else {
			add("-")
		}
	}
	add(id)
	file(r.rel(abs))
	_, _ = h.Write([]byte{1})
	for _, p := range chain {
		file(r.rel(p))
	}
	_, _ = h.Write([]byte{2})
	for _, k := range rootFiles {
		file(k)
	}
	return h.Sum64()
}

// resolveRejected is the value_rejected keys of id's effective config.
func resolveRejected(r *effectiveResolver, id string) map[string]string {
	ec, err := r.resolve(id)
	if err != nil {
		return nil
	}
	var keys map[string]string
	for k, ns := range ec.NotServed {
		if ns.Reason != NotServedValueRejected {
			continue
		}
		if keys == nil {
			keys = map[string]string{}
		}
		keys[k] = ns.File
	}
	return keys
}
