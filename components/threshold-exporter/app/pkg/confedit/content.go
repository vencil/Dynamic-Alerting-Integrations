package confedit

// The read-back's other side (ADR-036 §2): a file's parse result
// (parsetree.Content) with an edit applied, computed without yaml.v3 — the
// writer then reads the text it would write with parse-tree and requires the
// two to be equal. An edit on the node tree that a `<<` merge or an alias
// carries elsewhere shows up here as a difference.

import (
	"encoding/json"
	"fmt"
	"reflect"
	"sort"

	"github.com/vencil/threshold-exporter/pkg/parsetree"
)

// CloneContent is a deep copy of c (mappings and sequences; a Scalar is a
// value), so the Content* edits on it leave c as it was.
func CloneContent(c parsetree.Content) parsetree.Content {
	out, _ := cloneValue(map[string]any(c)).(map[string]any)
	return out
}

func cloneValue(v any) any {
	switch x := v.(type) {
	case map[string]any:
		m := make(map[string]any, len(x))
		for k, e := range x {
			m[k] = cloneValue(e)
		}
		return m
	case []any:
		s := make([]any, len(x))
		for i, e := range x {
			s[i] = cloneValue(e)
		}
		return s
	}
	return v
}

// ContentEnsureMap makes path a mapping in c, creating every missing one.
func ContentEnsureMap(c parsetree.Content, path []string) error {
	_, err := contentMapping(c, path, true)
	return err
}

// ContentSet sets the key at path to v in c; the mapping at the rest of path
// must be there. An empty path makes c, in place, the mapping v (Doc.Set's
// whole top level).
func ContentSet(c parsetree.Content, path []string, v any) error {
	if len(path) == 0 {
		m, ok := v.(map[string]any)
		if !ok {
			return &PathError{Reason: "the top level is set to a mapping only"}
		}
		for k := range c {
			delete(c, k)
		}
		for k, e := range m {
			c[k] = e
		}
		return nil
	}
	m, err := contentMapping(c, path[:len(path)-1], false)
	if err != nil {
		return err
	}
	m[path[len(path)-1]] = v
	return nil
}

// ContentUnset removes the key at path from c; a missing key or parent is a
// no-op.
func ContentUnset(c parsetree.Content, path []string) error {
	if len(path) == 0 {
		return &PathError{Reason: "unset needs a key"}
	}
	m, err := contentMapping(c, path[:len(path)-1], false)
	if pe, ok := err.(*PathError); ok && pe.Reason == "is not in the parse result" {
		return nil
	}
	if err != nil {
		return err
	}
	delete(m, path[len(path)-1])
	return nil
}

func contentMapping(c parsetree.Content, path []string, create bool) (map[string]any, error) {
	cur := map[string]any(c)
	for i, k := range path {
		next, ok := cur[k]
		if !ok {
			if !create {
				return nil, &PathError{At: path[:i+1], Reason: "is not in the parse result"}
			}
			m := map[string]any{}
			cur[k] = m
			cur = m
			continue
		}
		m, ok := next.(map[string]any)
		if !ok {
			return nil, &PathError{At: path[:i+1], Reason: "is not a mapping in the parse result"}
		}
		cur = m
	}
	return cur, nil
}

// ContentDifference is the first path (in sorted key order) where want and
// got differ, with the two values there (nil: absent); ok when they are
// equal. Both are compared as the JSON parse-tree writes.
func ContentDifference(want, got any) (path []string, w, g any, ok bool) {
	wn, gn := normal(want), normal(got)
	if reflect.DeepEqual(wn, gn) {
		return nil, nil, nil, true
	}
	p, w, g := firstDiff(wn, gn, []string{})
	return p, w, g, false
}

// normal is v as JSON reads it back (map[string]any, []any, string, bool,
// nil): a parsetree.Scalar and its raw value compared by meaning, not bytes.
func normal(v any) any {
	b, err := json.Marshal(v)
	if err != nil {
		return fmt.Sprintf("<unencodable: %v>", err)
	}
	var out any
	if err := json.Unmarshal(b, &out); err != nil {
		return fmt.Sprintf("<undecodable: %v>", err)
	}
	return out
}

func isScalar(v any) bool {
	m, ok := v.(map[string]any)
	if !ok {
		return false
	}
	_, text := m["text"].(string)
	_, tag := m["tag"]
	return text && tag
}

func firstDiff(w, g any, path []string) ([]string, any, any) {
	wm, wok := w.(map[string]any)
	gm, gok := g.(map[string]any)
	if wok && gok && !isScalar(w) && !isScalar(g) {
		keys := map[string]bool{}
		for k := range wm {
			keys[k] = true
		}
		for k := range gm {
			keys[k] = true
		}
		sorted := make([]string, 0, len(keys))
		for k := range keys {
			sorted = append(sorted, k)
		}
		sort.Strings(sorted)
		for _, k := range sorted {
			if !reflect.DeepEqual(wm[k], gm[k]) {
				return firstDiff(wm[k], gm[k], append(append([]string{}, path...), k))
			}
		}
	}
	ws, wok := w.([]any)
	gs, gok := g.([]any)
	if wok && gok && len(ws) == len(gs) {
		for i := range ws {
			if !reflect.DeepEqual(ws[i], gs[i]) {
				return firstDiff(ws[i], gs[i], append(append([]string{}, path...), fmt.Sprintf("[%d]", i)))
			}
		}
	}
	return path, w, g
}
