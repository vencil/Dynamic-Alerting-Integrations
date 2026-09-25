package config

// init_declaration_parity_test.go — Go's half of
// tests/shared/init_declaration_parity_matrix.json (#1942 blind review F1):
// which conf.d file declares which tenant, and which files the exporter's
// decode rejects. The Python half is tests/shared/test_init_declaration_parity.py
// (`da-tools init`'s `_possible_mentions`, which must cover every tenant
// this walker reads from a file). Neither side reads the other's source;
// both assert the table, so a row written from a guess about the exporter
// goes red HERE rather than shipping as init's rule.
//
// Seams: none — t.TempDir() trees, ScanDirTree with a discarding logger.

import (
	"bytes"
	"encoding/base64"
	"encoding/json"
	"io"
	"log"
	"os"
	"path/filepath"
	"reflect"
	"runtime"
	"sort"
	"strings"
	"testing"
)

type initDeclParityMatrix struct {
	Comment []string `json:"_comment"`
	Trees   []struct {
		Name  string            `json:"name"`
		Files map[string]string `json:"files"`
		// Bytes a JSON string cannot carry (a UTF-16 body), base64-encoded.
		FilesBase64  map[string]string   `json:"files_base64"`
		Declarations map[string][]string `json:"declarations"`
		Rejected     []string            `json:"rejected"`
		Conflict     *string             `json:"conflict"`
	} `json:"trees"`
}

func TestInitDeclarationParityMatrix(t *testing.T) {
	t.Parallel()
	_, thisFile, _, _ := runtime.Caller(0)
	path := filepath.Join(filepath.Dir(thisFile), "..", "..", "..", "..", "..",
		"tests", "shared", "init_declaration_parity_matrix.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read matrix: %v", err)
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	var m initDeclParityMatrix
	if err := dec.Decode(&m); err != nil {
		t.Fatalf("parse matrix: %v", err)
	}
	if len(m.Trees) == 0 {
		t.Fatal("matrix has no trees — a vacuous table passes nothing")
	}
	sawRejected, sawConflict := false, false
	for _, tree := range m.Trees {
		sawRejected = sawRejected || len(tree.Rejected) > 0
		sawConflict = sawConflict || tree.Conflict != nil
		t.Run(tree.Name, func(t *testing.T) {
			t.Parallel()
			root := t.TempDir()
			for rel, content := range tree.Files {
				rootWrite(t, filepath.Join(root, filepath.FromSlash(rel)), content)
			}
			for rel, b64 := range tree.FilesBase64 {
				data, err := base64.StdEncoding.DecodeString(b64)
				if err != nil {
					t.Fatalf("%s: bad base64: %v", rel, err)
				}
				rootWrite(t, filepath.Join(root, filepath.FromSlash(rel)), string(data))
			}
			scan, err := ScanDirTree(root, nil, nil, log.New(io.Discard, "", 0))
			if err != nil {
				t.Fatalf("ScanDirTree: %v", err)
			}
			gotDecl := map[string][]string{}
			var gotRejected []string
			for rel, f := range scan.Files {
				if strings.HasPrefix(filepath.Base(rel), "_") {
					continue
				}
				if f.ParseFailed {
					gotRejected = append(gotRejected, rel)
					continue
				}
				ids := append([]string{}, f.TenantIDs...)
				sort.Strings(ids)
				gotDecl[rel] = ids
			}
			sort.Strings(gotRejected)
			wantDecl := map[string][]string{}
			for rel, ids := range tree.Declarations {
				w := append([]string{}, ids...)
				sort.Strings(w)
				wantDecl[rel] = w
			}
			if !reflect.DeepEqual(gotDecl, wantDecl) {
				t.Errorf("declarations %v, want %v", gotDecl, wantDecl)
			}
			wantRejected := append([]string{}, tree.Rejected...)
			sort.Strings(wantRejected)
			if len(gotRejected) == 0 {
				gotRejected = []string{}
			}
			if !reflect.DeepEqual(gotRejected, wantRejected) {
				t.Errorf("rejected %v, want %v", gotRejected, wantRejected)
			}
			var gotConflict *string
			if scan.Conflict != nil {
				id := scan.Conflict.TenantID
				gotConflict = &id
			}
			if (gotConflict == nil) != (tree.Conflict == nil) ||
				(gotConflict != nil && *gotConflict != *tree.Conflict) {
				t.Errorf("conflict %v, want %v", deref(gotConflict), deref(tree.Conflict))
			}
		})
	}
	// Anti-vacuity: the two verdicts init has to reproduce must each have
	// at least one row, or the table stops testing the defect it exists for.
	if !sawRejected || !sawConflict {
		t.Errorf("matrix lost its rejected (%v) or conflict (%v) rows", sawRejected, sawConflict)
	}
}

func deref(s *string) string {
	if s == nil {
		return "<nil>"
	}
	return *s
}
