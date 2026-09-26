package config

// defaults_symlink_parity_test.go — Go's half of
// tests/shared/defaults_symlink_parity_matrix.json (#1674 round 2): defaults
// carriers that are symlinks; since #2054 also which entries the walker does
// NOT read at all (hidden files, hidden directories, a ConfigMap mount's
// `..data` payload), via `"absent": true` rows; since #2049 also tenants
// declared by more than one carrier, via `"error": "duplicate"` rows. The Python half is
// tests/shared/test_defaults_symlink_parity.py (describe_tenant). Neither side
// reads the other's source; both assert the table.
//
// Seams: none — t.TempDir() trees.

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"strings"
	"testing"
)

// ⛔ Decoded with DisallowUnknownFields (#1967 blind review): a misspelt
// optional key (`confd` for `conf_d`) was silently dropped, the tree fell
// back to the tree root as conf.d, and the row stopped testing what its name
// says while staying green. `_comment` is therefore declared, not ignored.
type symlinkParityMatrix struct {
	Comment []string `json:"_comment"`
	Trees   []struct {
		Name string `json:"name"`
		// ConfD (optional, #1967): the tree-root-relative directory handed
		// to ResolveEffective, so a tree can hold link targets outside
		// conf.d. Empty = the tree root.
		ConfD    string                  `json:"conf_d"`
		Files    map[string]string       `json:"files"`
		Symlinks map[string]string       `json:"symlinks"`
		Expect   map[string]parityExpect `json:"expect"`
	} `json:"trees"`
}

// parityExpect is one tenant's row. Exactly ONE shape per row, told apart by
// which keys are PRESENT (hence the pointers): "resolved" (all of chain_len,
// effective_config, merged_hash) or "absent" (`"absent": true` alone — the
// walker must not see this tenant at all, #2054). ⛔ Mixing shapes is a
// broken table, not a looser one: `absent` beside a merged_hash would let
// either half go unchecked while the row stays green. "error" (#2049) is
// `"error": "duplicate"` alone: ResolveEffective must return
// *DuplicateTenantError for this tenant — the exporter serves no effective
// config for a tenant two carriers declare.
type parityExpect struct {
	Absent          *bool          `json:"absent"`
	Error           *string        `json:"error"`
	ChainLen        *int           `json:"chain_len"`
	EffectiveConfig map[string]any `json:"effective_config"`
	MergedHash      *string        `json:"merged_hash"`
}

// UnmarshalJSON rejects an explicit JSON null. ⛔ Without it `null` decodes
// to a nil pointer / nil map — indistinguishable from an ABSENT key — so
// `{"absent": true, "effective_config": null}` read as a clean absent row
// here while the Python half (exact key sets) rejected it: the two halves
// disagreed about which rows are mixed. It also re-applies
// DisallowUnknownFields, which the outer decoder does NOT propagate into a
// custom UnmarshalJSON.
func (e *parityExpect) UnmarshalJSON(raw []byte) error {
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(raw, &fields); err != nil {
		return err
	}
	for key, val := range fields {
		if string(bytes.TrimSpace(val)) == "null" {
			return fmt.Errorf("expect key %q is null — omit the key instead; null reads as absent and hides a mixed row", key)
		}
	}
	type plain parityExpect // no methods: no recursion into this function
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	return dec.Decode((*plain)(e))
}

const (
	shapeResolved = "resolved"
	shapeAbsent   = "absent"
	shapeError    = "error"

	// errorDuplicate is the only `error` value the matrix knows.
	errorDuplicate = "duplicate"
)

func (e parityExpect) shape() (string, error) {
	resolvedKeys := 0
	for _, set := range []bool{e.ChainLen != nil, e.EffectiveConfig != nil, e.MergedHash != nil} {
		if set {
			resolvedKeys++
		}
	}
	switch {
	case e.Error != nil && *e.Error != errorDuplicate:
		return "", fmt.Errorf(`"error" must be %q, got %q`, errorDuplicate, *e.Error)
	case e.Error != nil && (e.Absent != nil || resolvedKeys > 0):
		return "", errors.New(`"error" is exclusive with absent / chain_len / effective_config / merged_hash`)
	case e.Error != nil:
		return shapeError, nil
	case e.Absent != nil && !*e.Absent:
		return "", errors.New(`"absent" must be true when present — omit it for a tenant that resolves`)
	case e.Absent != nil && resolvedKeys > 0:
		return "", errors.New(`"absent" is exclusive with chain_len / effective_config / merged_hash`)
	case e.Absent != nil:
		return shapeAbsent, nil
	case resolvedKeys == 3:
		return shapeResolved, nil
	default:
		return "", errors.New("a resolving tenant needs all of chain_len, effective_config, merged_hash")
	}
}

// TestParityExpectShapeRejectsMixedRows: the shape check itself must bite,
// or a mixed row would be read as whichever half happened to match.
func TestParityExpectShapeRejectsMixedRows(t *testing.T) {
	t.Parallel()
	yes, no, n, h, dup, other := true, false, 1, "x", errorDuplicate, "conflict"
	for name, e := range map[string]parityExpect{
		"absent-false":          {Absent: &no},
		"absent-with-hash":      {Absent: &yes, MergedHash: &h},
		"absent-with-chain":     {Absent: &yes, ChainLen: &n},
		"absent-with-effective": {Absent: &yes, EffectiveConfig: map[string]any{}},
		"resolved-missing-hash": {ChainLen: &n, EffectiveConfig: map[string]any{}},
		"empty":                 {},
		"error-unknown-value":   {Error: &other},
		"error-with-absent":     {Error: &dup, Absent: &yes},
		"error-with-hash":       {Error: &dup, MergedHash: &h},
		"error-with-chain":      {Error: &dup, ChainLen: &n},
		"error-with-effective":  {Error: &dup, EffectiveConfig: map[string]any{}},
	} {
		if got, err := e.shape(); err == nil {
			t.Errorf("%s: accepted as %q, want an error", name, got)
		}
	}
}

// TestParityExpectDecodeRejectsNullAndUnknownKeys: the rows as the MATRIX
// spells them, through the real decoder. A null must not pass as a missing
// key, and an unknown key must not be dropped, or a mixed row stays green on
// the Go side while Python rejects it.
func TestParityExpectDecodeRejectsNullAndUnknownKeys(t *testing.T) {
	t.Parallel()
	for name, tc := range map[string]struct {
		raw     string
		wantErr bool
	}{
		"absent-with-null-effective": {`{"absent":true,"effective_config":null}`, true},
		"absent-with-null-hash":      {`{"absent":true,"merged_hash":null}`, true},
		"resolved-with-null-chain":   {`{"chain_len":null,"effective_config":{},"merged_hash":"x"}`, true},
		"unknown-key":                {`{"absent":true,"absnet":true}`, true},
		"error-with-null-hash":       {`{"error":"duplicate","merged_hash":null}`, true},
		"valid-absent":               {`{"absent":true}`, false},
		"valid-error":                {`{"error":"duplicate"}`, false},
		"valid-resolved":             {`{"chain_len":1,"effective_config":{"cpu_pct":50},"merged_hash":"x"}`, false},
	} {
		var e parityExpect
		err := json.Unmarshal([]byte(tc.raw), &e)
		if err == nil {
			_, err = e.shape()
		}
		if (err != nil) != tc.wantErr {
			t.Errorf("%s: %s → err %v, wantErr %v", name, tc.raw, err, tc.wantErr)
		}
	}
}

func TestDefaultsSymlinkParityMatrix(t *testing.T) {
	t.Parallel()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "defaults_symlink_parity_matrix.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	var m symlinkParityMatrix
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Trees) == 0 {
		t.Fatal("matrix has no trees — a vacuous table passes nothing")
	}
	for _, tree := range m.Trees {
		for tenant, want := range tree.Expect {
			if _, err := want.shape(); err != nil {
				t.Fatalf("%s/%s: %v", tree.Name, tenant, err)
			}
		}
	}
	for _, tree := range m.Trees {
		t.Run(tree.Name, func(t *testing.T) {
			t.Parallel()
			root := t.TempDir()
			for rel, content := range tree.Files {
				rootWrite(t, filepath.Join(root, filepath.FromSlash(rel)), content)
			}
			for link, target := range tree.Symlinks {
				if err := os.Symlink(filepath.FromSlash(target), filepath.Join(root, filepath.FromSlash(link))); err != nil {
					t.Skipf("symlinks unavailable here (%v) — measured on Linux/macOS CI", err)
				}
			}
			confD := filepath.Join(root, filepath.FromSlash(tree.ConfD))
			if tree.ConfD != "" {
				assertSomeLinkEscapesConfD(t, confD, root, tree.Symlinks)
			}
			for tenant, want := range tree.Expect {
				ec, err := ResolveEffective(confD, tenant)
				switch shape, _ := want.shape(); shape {
				case shapeAbsent:
					if !errors.Is(err, ErrTenantNotFound) {
						t.Errorf("%s: pinned absent (the walker must not read it), got %+v, err %v", tenant, ec, err)
					}
					continue
				case shapeError:
					var dup *DuplicateTenantError
					if !errors.As(err, &dup) || dup.TenantID != tenant {
						t.Errorf("%s: pinned duplicate (two carriers declare it), got %+v, err %v", tenant, ec, err)
					}
					continue
				}
				if err != nil {
					t.Fatalf("%s: %v", tenant, err)
				}
				if len(ec.DefaultsChain) != *want.ChainLen {
					t.Errorf("%s: chain %v, want %d levels", tenant, ec.DefaultsChain, *want.ChainLen)
				}
				gotJSON, _ := json.Marshal(ec.EffectiveConfig)
				var got map[string]any
				_ = json.Unmarshal(gotJSON, &got)
				if !reflect.DeepEqual(got, want.EffectiveConfig) {
					t.Errorf("%s: effective %v, want %v", tenant, got, want.EffectiveConfig)
				}
				if ec.MergedHash != *want.MergedHash {
					t.Errorf("%s: merged_hash %s, pinned %s", tenant, ec.MergedHash, *want.MergedHash)
				}
			}
		})
	}
}

// assertSomeLinkEscapesConfD fails the row unless at least one of its links
// resolves OUTSIDE confD. `conf_d` exists only so a row can hold such a
// target (#1967 rows h / k); a row that declares it with every target inside
// conf.d no longer tests what it was added for, and would still pass.
func assertSomeLinkEscapesConfD(t *testing.T, confD, root string, links map[string]string) {
	t.Helper()
	realConfD, err := filepath.EvalSymlinks(confD)
	if err != nil {
		t.Fatalf("conf_d %s: %v", confD, err)
	}
	for link := range links {
		target, err := filepath.EvalSymlinks(filepath.Join(root, filepath.FromSlash(link)))
		if err != nil {
			t.Fatalf("resolve %s: %v", link, err)
		}
		rel, err := filepath.Rel(realConfD, target)
		if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
			return
		}
	}
	t.Fatalf("row declares conf_d %q but no symlink target resolves outside it — "+
		"the row no longer tests a target outside conf.d", confD)
}
