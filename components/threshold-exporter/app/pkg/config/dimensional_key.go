package config

// Dimensional keys in one spelling (#2031).
//
// `redis_queue_length{queue="a", priority="high"}` and
// `redis_queue_length{priority='high',queue="a"}` are one threshold: the
// parser (parseKeyWithLabels) reads both into the same label maps, so both
// serve the same user_threshold series. Every map of the config, though, is
// keyed by the key's TEXT, and so is every merge. Two spellings of one
// threshold — in one file, or in two layers (a subtree `_defaults.yaml` and
// the tenant file, a platform `tenants:` entry, a profile) — stayed two keys,
// each served its own row, and the second row had the first one's label set:
// client_golang refused the whole Gather and /metrics answered HTTP 500 for
// every tenant.
//
// The fix is at the decode: every layer's map is re-keyed to the canonical
// spelling (normalizeKeys) before anything merges it, so the merges, which
// already compare by text, compare the threshold. What the author wrote is
// kept beside it (keySpellings.written) and given back on every output
// (EffectiveConfig.KeySpellings, the winning layer's text).
//
// ⛔ ONLY WHERE THE PARSE IS LOSSLESS. The canonical form is the parse
// rendered back, so it may only replace a key the parser reads exactly as
// written. The parser splits on every comma and trims quotes greedily: it
// reads `x{q=~"A,B"}` as `x{q=~"A"}`, and `x{q="'a'"}` as `x{q="a"}`. Such a
// key is left as written (canonicalDimensionalKey answers false), so two
// keys whose texts mean different thresholds are never merged into one; the
// series they may still share is the gate's metrics_not_gatherable.
//
// The canonical spelling: label names sorted, `name="value"` or
// `name=~"value"`, separated by `, ` — the spelling the docs use.

import (
	"sort"
	"strings"
	"unicode"
)

// dimMatcher is one label matcher of a dimensional key.
type dimMatcher struct{ name, op, value string }

// dimParts splits a threshold dimensional key into its base and the text
// between the braces, as keyWithLabelsRe does. ok is false for a key that is
// not dimensional, and for a reserved (`_`-prefixed) key: only threshold keys
// are re-spelled.
func dimParts(key string) (base, inner string, ok bool) {
	if key == "" || key[0] == '_' || key[len(key)-1] != '}' {
		return "", "", false
	}
	i := strings.IndexByte(key, '{')
	// keyWithLabelsRe's `.` matches no newline: such a key is not
	// dimensional to the parser.
	if i <= 0 || i+1 >= len(key)-1 || strings.IndexByte(key, '\n') >= 0 {
		return "", "", false
	}
	for j := 0; j < i; j++ {
		if !isWordByte(key[j]) {
			return "", "", false
		}
	}
	return key[:i], key[i+1 : len(key)-1], true
}

// isWordByte reports whether c is in [a-zA-Z0-9_].
func isWordByte(c byte) bool {
	return c == '_' || c >= '0' && c <= '9' || c >= 'a' && c <= 'z' || c >= 'A' && c <= 'Z'
}

// parseDimMatcher reads one comma-separated matcher strictly: a label name,
// `=` or `=~`, and a value in double quotes, in single quotes or bare, with
// no quote inside it. ok is false for anything else, and for a matcher
// parseLabelsStringWithOp reads differently (a value that starts or ends
// with the other quote, an `=` matcher holding `=~`). tight reports the
// canonical text (`name="value"`, no space).
func parseDimMatcher(frag string) (m dimMatcher, tight, ok bool) {
	t := strings.TrimSpace(frag)
	tight = t == frag
	n := 0
	for n < len(t) && isWordByte(t[n]) && (n > 0 || t[n] < '0' || t[n] > '9') {
		n++
	}
	if n == 0 {
		return dimMatcher{}, false, false
	}
	m.name = t[:n]
	rest := t[n:]
	if r := strings.TrimLeft(rest, " \t"); r != rest {
		tight, rest = false, r
	}
	switch {
	case strings.HasPrefix(rest, "=~"):
		m.op, rest = "=~", rest[2:]
	case strings.HasPrefix(rest, "="):
		m.op, rest = "=", rest[1:]
		if strings.Contains(rest, "=~") {
			return dimMatcher{}, false, false // the parser takes that `=~` as the operator
		}
	default:
		return dimMatcher{}, false, false
	}
	if r := strings.TrimLeft(rest, " \t"); r != rest {
		tight, rest = false, r
	}
	if strings.TrimSpace(rest) != rest {
		return dimMatcher{}, false, false // other whitespace: the parser trims it
	}
	switch {
	case len(rest) >= 2 && (rest[0] == '"' || rest[0] == '\'') && rest[len(rest)-1] == rest[0]:
		m.value = rest[1 : len(rest)-1]
		if strings.IndexByte(m.value, rest[0]) >= 0 {
			return dimMatcher{}, false, false
		}
		tight = tight && rest[0] == '"'
	case !strings.ContainsAny(rest, "\"'") && strings.IndexFunc(rest, unicode.IsSpace) < 0:
		m.value, tight = rest, false
	default:
		return dimMatcher{}, false, false
	}
	// parseLabelsStringWithOp trims every leading and trailing quote of
	// either kind: the value it reads must be the one written.
	if strings.Trim(rest, `"'`) != m.value {
		return dimMatcher{}, false, false
	}
	return m, tight, true
}

// canonicalDimensionalKey is key in the canonical spelling (see the file
// comment) and true, when key is a threshold dimensional key the parser
// reads losslessly; key itself (no allocation) when it already is canonical.
// (key, false) for any other key, which stays as written.
func canonicalDimensionalKey(key string) (string, bool) {
	base, inner, ok := dimParts(key)
	if !ok {
		return key, false
	}
	// Already canonical: names strictly ascending, tight matchers, `, `
	// between them. No allocation on this path.
	canon := true
	prev := ""
	for i, rest := 0, inner; ; i++ {
		frag := rest
		next := strings.IndexByte(rest, ',')
		if next >= 0 {
			frag, rest = rest[:next], rest[next+1:]
		}
		if i > 0 {
			if len(frag) < 2 || frag[0] != ' ' || frag[1] == ' ' {
				canon = false
			} else {
				frag = frag[1:]
			}
		}
		m, tight, ok := parseDimMatcher(frag)
		if !ok {
			if canon {
				return key, false
			}
			break // judged below
		}
		if !tight || m.name <= prev {
			canon = false
		}
		prev = m.name
		if next < 0 {
			break
		}
	}
	if canon {
		return key, true
	}
	frags := strings.Split(inner, ",")
	ms := make([]dimMatcher, 0, len(frags))
	seen := make(map[string]bool, len(frags))
	for _, f := range frags {
		m, _, ok := parseDimMatcher(f)
		// A value holding `"` has no double-quoted spelling.
		if !ok || seen[m.name] || strings.IndexByte(m.value, '"') >= 0 {
			return key, false
		}
		seen[m.name] = true
		ms = append(ms, m)
	}
	sort.Slice(ms, func(i, j int) bool { return ms[i].name < ms[j].name })
	var b strings.Builder
	b.Grow(len(key) + 2*len(ms))
	b.WriteString(base)
	b.WriteByte('{')
	for i, m := range ms {
		if i > 0 {
			b.WriteString(", ")
		}
		b.WriteString(m.name)
		b.WriteString(m.op)
		b.WriteByte('"')
		b.WriteString(m.value)
		b.WriteByte('"')
	}
	b.WriteByte('}')
	return b.String(), true
}

// keySpellings is what one layer's map wrote beside the canonical spelling.
// nil (the steady state) when it wrote every key canonically and no
// threshold twice.
type keySpellings struct {
	// written: canonical key → the spelling the layer wrote, for the keys
	// it did not write canonically.
	written map[string]string
	// dups: the key the layer serves → the other spellings of the same
	// threshold the layer also wrote (as written, sorted). /metrics does not
	// serve them.
	dups map[string][]string
}

// writtenOf is the spelling the layer wrote key in: key itself unless the
// layer re-spelled it.
func (s *keySpellings) writtenOf(key string) string {
	if s != nil {
		if w, ok := s.written[key]; ok {
			return w
		}
	}
	return key
}

// normalizeKeys is one layer's map with every dimensional key in its
// canonical spelling, and what that changed (nil: nothing). m itself, with
// no allocation, when no key needs re-spelling — the steady state.
//
// Two spellings of one threshold in the same map: the spelling that already
// is canonical wins, else the smallest text; a spelling that writes nothing
// (null, nullThreshold) never wins over one that writes, and is not a
// duplicate either (#2418's rule: a null beside another spelling writes
// nothing). The losers are recorded in dups, and so is the #1231 retired
// spelling of a threshold the map also writes canonically — which stays in
// the map: canonical-wins dedup further down serves the canonical one, as it
// always has.
func normalizeKeys[V any](m map[string]V) (map[string]V, *keySpellings) {
	out := m
	var s *keySpellings
	respell := false
	for k := range m {
		if c, ok := canonicalDimensionalKey(k); ok && c != k {
			respell = true
			break
		}
	}
	if respell {
		groups := make(map[string][]string, len(m))
		for k := range m {
			c, _ := canonicalDimensionalKey(k)
			groups[c] = append(groups[c], k)
		}
		out = make(map[string]V, len(groups))
		s = &keySpellings{}
		for c, ks := range groups {
			win := ks[0]
			if len(ks) > 1 {
				win = spellingWinner(c, ks, m)
				for _, k := range ks {
					if k != win && !nullThreshold(c, any(m[k])) {
						s.addDup(c, k)
					}
				}
			}
			out[c] = m[win]
			if win != c {
				if s.written == nil {
					s.written = make(map[string]string)
				}
				s.written[c] = win
			}
		}
	}
	for k, v := range out {
		if !touchesAlias(k) {
			continue
		}
		canon, isAlias := canonicalKeyFor(k)
		if !isAlias || nullThreshold(k, any(v)) {
			continue
		}
		if cv, both := out[canon]; both && !nullThreshold(canon, any(cv)) {
			if s == nil {
				s = &keySpellings{}
			}
			s.addDup(canon, s.writtenOf(k))
		}
	}
	return out, s
}

// spellingWinner picks which of ks — spellings of canonical key c in m —
// the layer serves (see normalizeKeys).
func spellingWinner[V any](c string, ks []string, m map[string]V) string {
	win := ""
	winWrites := false
	for _, k := range ks {
		writes := !nullThreshold(c, any(m[k]))
		switch {
		case win == "":
		case writes != winWrites:
			if !writes {
				continue
			}
		case win == c:
			continue
		case k != c && k > win:
			continue
		}
		win, winWrites = k, writes
	}
	return win
}

func (s *keySpellings) addDup(served, loser string) {
	if s.dups == nil {
		s.dups = make(map[string][]string)
	}
	d := append(s.dups[served], loser)
	sort.Strings(d)
	s.dups[served] = d
}

// writtenAs is key spelled with written's label text: the base stays key's
// (an output may spell the base canonically, #1231), the `{…}` part is the
// one the layer wrote. key when written is empty or either is not
// dimensional.
func writtenAs(key, written string) string {
	if written == "" || written == key {
		return key
	}
	i, j := strings.IndexByte(key, '{'), strings.IndexByte(written, '{')
	if i <= 0 || j <= 0 {
		return key
	}
	return key[:i] + written[j:]
}

// layerSpellings is, for one tenant's resolve, each layer's keySpellings
// (#2031) — the lookup keySources' attribution is read through.
type layerSpellings struct {
	tenant   *keySpellings
	chain    []*keySpellings          // per chain level; nil: no level re-spelled
	platform map[string]*keySpellings // root platform file → its entry for the tenant
	profile  map[string]*keySpellings // root platform file → its body of the bound profile
}

func (l *layerSpellings) addChain(i, n int, s *keySpellings) {
	if s == nil {
		return
	}
	if l.chain == nil {
		l.chain = make([]*keySpellings, n)
	}
	l.chain[i] = s
}

func (l *layerSpellings) addPlatform(overlay []PlatformBlock) {
	for _, pb := range overlay {
		if pb.spell == nil {
			continue
		}
		if l.platform == nil {
			l.platform = make(map[string]*keySpellings)
		}
		l.platform[pb.File] = pb.spell
	}
}

func (l *layerSpellings) addProfile(pp *PlatformProfiles, name string) {
	if pp != nil && name != "" {
		l.profile = pp.spell[name]
	}
}

// any reports whether some layer of the tenant re-spelled a key or wrote a
// threshold twice.
func (l layerSpellings) any() bool {
	return l.tenant != nil || l.chain != nil || l.platform != nil || l.profile != nil
}

// of is the spellings of the layer src names.
func (l layerSpellings) of(src KeySource) *keySpellings {
	switch src.Layer {
	case KeyLayerTenant:
		return l.tenant
	case KeyLayerPlatform:
		return l.platform[src.File]
	case KeyLayerProfile:
		return l.profile[src.File]
	case KeyLayerDefaults:
		if src.Level != nil && *src.Level < len(l.chain) {
			return l.chain[*src.Level]
		}
	}
	return nil
}

// forView is, for each key of view, the spelling its winning layer (ks)
// wrote, where that is not the key (EffectiveConfig.KeySpellings), and the
// spellings that layer wrote beside the one it serves, each a
// NotServedSpellingDuplicate in that layer's file. A layer is asked under
// every #1231 spelling of the key: the profile layer is laid canonically.
func (l layerSpellings) forView(view map[string]any, ks map[string]KeySource) (written map[string]string, dups map[string]NotServedKey) {
	if !l.any() {
		return nil, nil
	}
	var buf [2]string
	for k := range view {
		src, ok := ks[k]
		if !ok {
			continue
		}
		s := l.of(src)
		if s == nil {
			continue
		}
		for _, sk := range append([]string{k}, otherSpellings(k, &buf)...) {
			if w, ok := s.written[sk]; ok && writtenAs(k, w) != k {
				if written == nil {
					written = make(map[string]string)
				}
				written[k] = writtenAs(k, w)
			}
			for _, loser := range s.dups[sk] {
				if dups == nil {
					dups = make(map[string]NotServedKey)
				}
				dups[loser] = NotServedKey{Reason: NotServedSpellingDuplicate, File: src.File}
			}
		}
	}
	return written, dups
}

// WrittenKey is key as spellings (an EffectiveConfig.KeySpellings) spells
// it, asked under every #1231 spelling of key: a reader may hold the
// canonical base (an overlay's Keys, served-values) where EffectiveConfig
// keeps the layer's. Only the `{…}` part is taken from spellings; key itself
// when spellings has none of them (#2031).
func WrittenKey(spellings map[string]string, key string) string {
	if w, ok := spellings[key]; ok {
		return writtenAs(key, w)
	}
	var buf [2]string
	for _, s := range otherSpellings(key, &buf) {
		if w, ok := spellings[s]; ok {
			return writtenAs(key, w)
		}
	}
	return key
}

func (ec *EffectiveConfig) writtenKey(key string) string { return WrittenKey(ec.KeySpellings, key) }

// AsWritten is ec with the keys of EffectiveConfig, KeySources, NotServed
// and the overlays' Keys spelled as the layer that supplied each value wrote
// them (#2031) — what /effective and `da-guard effective` print. ec itself
// when no key is re-spelled. The other maps (MergedConfig,
// TenantOverridesRaw, MergedDefaults) stay canonical: da-guard compares
// them with each other.
func (ec *EffectiveConfig) AsWritten() *EffectiveConfig {
	if ec == nil || len(ec.KeySpellings) == 0 {
		return ec
	}
	out := *ec
	out.EffectiveConfig = renamedKeys(ec.EffectiveConfig, ec.writtenKey)
	out.KeySources = renamedKeys(ec.KeySources, ec.writtenKey)
	out.NotServed = renamedKeys(ec.NotServed, ec.writtenKey)
	out.PlatformOverlay = make([]PlatformOverlaySource, len(ec.PlatformOverlay))
	for i, ps := range ec.PlatformOverlay {
		out.PlatformOverlay[i] = PlatformOverlaySource{File: ps.File, Keys: renamedList(ps.Keys, ec.writtenKey)}
	}
	out.ProfileOverlay = make([]ProfileOverlaySource, len(ec.ProfileOverlay))
	for i, ps := range ec.ProfileOverlay {
		out.ProfileOverlay[i] = ProfileOverlaySource{Profile: ps.Profile, File: ps.File, Keys: renamedList(ps.Keys, ec.writtenKey)}
	}
	if ec.PlatformOverlay == nil {
		out.PlatformOverlay = nil
	}
	if ec.ProfileOverlay == nil {
		out.ProfileOverlay = nil
	}
	return &out
}

func renamedKeys[V any](m map[string]V, name func(string) string) map[string]V {
	if m == nil {
		return nil
	}
	out := make(map[string]V, len(m))
	for k, v := range m {
		out[name(k)] = v
	}
	return out
}

func renamedList(keys []string, name func(string) string) []string {
	out := make([]string, len(keys))
	for i, k := range keys {
		out[i] = name(k)
	}
	sort.Strings(out)
	return out
}
