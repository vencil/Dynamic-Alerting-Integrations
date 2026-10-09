package config

// RejectedShownCache: which tenants are shown a subtree defaults value the
// build refused (#2065, the exporter's value_rejected count), or a spelling
// of a threshold its layer also writes under the spelling /metrics serves
// (#2031, spelling_duplicate).

import (
	"hash/fnv"
	"path/filepath"
	"sort"
	"strings"
	"sync"
)

// RejectedShownCache keeps Shown's answer across commits: a candidate tenant
// is resolved again only when an input of its resolve moved. The input
// fingerprint is the scan's SHA-256 of every file the resolve reads
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
	keys  map[string]string // nil: resolved, nothing value_rejected
}

// Shown is, per tenant, each key the tenant's effective config
// names NotServedValueRejected or NotServedSpellingDuplicate, and the
// reason — the verdict `/effective`, `da-guard effective` and da-guard's gate
// give, from the same resolver (effectiveResolver.resolve →
// servedVerdicts.notServed, keySources' winner attribution). Nothing is
// re-judged here: a tenant is resolved when its defaults chain holds a file
// of built.RejectedChainValues (no other tenant can be named
// value_rejected), or when a file its resolve reads re-spelled a key
// (spellingCandidates; no other tenant can be named spelling_duplicate), and
// the reason is read off the result. Keys are spelled as the resolver keys
// effective_config (canonically, #2031), and a spelling_duplicate key as
// written.
//
// scan is the scan built came from; a file a warm scan did not cache is read
// from disk and used only when its SHA-256 is the scan's. A candidate whose
// resolve failed is not in the result, as /effective fails for it. nil when
// there is none.
//
// It reuses the previous call's answer for a candidate whose inputs did not
// move. Nothing is cached for a failed resolve, so the next call resolves it
// again (#2065 r4: a cached failure outlived the file changing back to the
// scanned bytes).
func (c *RejectedShownCache) Shown(scan *TreeScan, built *FlatBuild) map[string]map[string]string {
	c.mu.Lock()
	defer c.mu.Unlock()
	if scan == nil || built == nil || len(scan.Tenants) == 0 {
		c.entries = nil
		return nil
	}
	if len(built.RejectedChainValues) == 0 && !built.maySpell() {
		c.entries = nil
		return nil // the steady state: no resolver is built
	}
	r := newEffectiveResolver(scan)
	r.readUncached = true
	spelled := spellingCandidates(scan, built, r)
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
		candidate := spelled[id]
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
				r.served = newServedVerdicts(built, false)
				r.served.noUnparsed = true
				served = true
			}
			keys, err := resolveRejected(r, id)
			c.Resolves++
			if err != nil {
				continue
			}
			e = rejectedShownEntry{input: in, keys: keys}
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

// resolveRejected is the value_rejected and spelling_duplicate keys of id's
// effective config, each with its reason.
func resolveRejected(r *effectiveResolver, id string) (map[string]string, error) {
	ec, err := r.resolve(id)
	if err != nil {
		return nil, err
	}
	var keys map[string]string
	for k, ns := range ec.NotServed {
		if ns.Reason != NotServedValueRejected && ns.Reason != NotServedSpellingDuplicate {
			continue
		}
		if keys == nil {
			keys = map[string]string{}
		}
		keys[k] = ns.Reason
	}
	return keys, nil
}

// maySpell reports whether some file of the build may have re-spelled a key
// or written a threshold twice — whether spellingCandidates can name anyone.
func (b *FlatBuild) maySpell() bool {
	if len(b.respelledChain) > 0 {
		return true
	}
	for _, fc := range b.FileConfigs {
		if fc.spelled {
			return true
		}
	}
	return false
}

// spellingCandidates is the tenants of scan whose effective config reads a
// file that wrote a dimensional key in another spelling than the canonical
// one, or a threshold twice (#2031): its tenant file or a root platform file
// (the build's own decode, ThresholdConfig.spelled), or a file of its
// defaults chain that may have (FlatBuild.respelledChain). Only those can be
// shown a key as written that is not its canonical spelling, or a
// spelling_duplicate. nil when none — without a per-tenant walk when no file
// is spelled, the steady state.
func spellingCandidates(scan *TreeScan, built *FlatBuild, r *effectiveResolver) map[string]bool {
	var out map[string]bool
	add := func(id string) {
		if out == nil {
			out = map[string]bool{}
		}
		out[id] = true
	}
	for name, fc := range built.FileConfigs {
		if !fc.spelled {
			continue
		}
		if !strings.Contains(name, "/") && isPlatformKey(name) {
			for id := range scan.Tenants {
				add(id)
			}
			return out
		}
		for id := range fc.Tenants {
			if _, ok := scan.Tenants[id]; ok {
				add(id)
			}
		}
	}
	if len(built.respelledChain) == 0 {
		return out
	}
	for id, abs := range scan.Tenants {
		if out[id] {
			continue
		}
		for _, p := range r.chain(filepath.Dir(abs)) {
			if built.respelledChain[p] {
				add(id)
				break
			}
		}
	}
	return out
}

// writtenKeys is, per tenant of scan its effective config shows a key of in
// another spelling than the resolver keys it by, that tenant's
// EffectiveConfig.KeySpellings (#2031) — what `da-guard served-values` spells
// its keys with, so they read as `da-guard effective`'s do. nil when none.
func writtenKeys(scan *TreeScan, built *FlatBuild) map[string]map[string]string {
	if !built.maySpell() {
		return nil
	}
	r := newEffectiveResolver(scan)
	spelled := spellingCandidates(scan, built, r)
	var out map[string]map[string]string
	for id := range spelled {
		ec, err := r.resolve(id)
		if err != nil || len(ec.KeySpellings) == 0 {
			continue
		}
		if out == nil {
			out = map[string]map[string]string{}
		}
		out[id] = ec.KeySpellings
	}
	return out
}
