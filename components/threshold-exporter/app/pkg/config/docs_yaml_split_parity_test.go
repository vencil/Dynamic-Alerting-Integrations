package config

// docs_yaml_split_parity_test.go — Go half of the Go↔Python parity pin for
// splitting a documented ```yaml block into the files it describes.
//
// Two gates read docs/**/*.md and must cut blocks identically:
//
//   - this package's docs_defaults_sample_test.go (splitConcatenatedFiles) —
//     the LOADER oracle, which catches value-type errors the schema leaves
//     loose;
//   - scripts/tools/lint/check_md_yaml_drift.py (_split_documented_files) —
//     the SCHEMA oracle, which catches unknown/typo'd keys the loader decodes
//     non-strictly and therefore cannot see.
//
// The two catch DISJOINT defect classes, so neither can be deleted in favour of
// the other — which is exactly why their shared extraction rule needs a pin.
// They are independent hand-written implementations of one rule over one
// corpus; this repo has been burned by that shape before (four hand-copied
// contracts drifting). So neither side asserts the other: both assert
// tests/shared/docs_yaml_split_matrix.json, making their agreement transitive.
//
// Deliberately a CASE MATRIX rather than a snapshot of the live docs tree — a
// line-numbered snapshot would need regenerating on every documentation edit,
// which teaches people to regenerate without reading.

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

type splitCase struct {
	Name    string   `json:"name"`
	Block   []string `json:"block"`
	Offsets []int    `json:"offsets"`
	Why     string   `json:"why"`
}

func loadSplitMatrix(t *testing.T) []splitCase {
	t.Helper()
	root := findRepoRootForRegistry(t)
	path := filepath.Join(root, "tests", "shared", "docs_yaml_split_matrix.json")
	raw, err := os.ReadFile(path) // #nosec G304 -- repo-relative test fixture
	if err != nil {
		t.Fatalf("read split matrix: %v", err)
	}
	var doc struct {
		Cases []splitCase `json:"cases"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal split matrix: %v", err)
	}
	if len(doc.Cases) < 8 {
		t.Fatalf("matrix shrank to %d cases — a parity pin that measures nothing "+
			"must fail loudly, not pass quietly", len(doc.Cases))
	}
	return doc.Cases
}

// TestDocsYamlSplitMatrix asserts splitConcatenatedFiles reproduces the shared
// matrix, case for case.
func TestDocsYamlSplitMatrix(t *testing.T) {
	t.Parallel()
	for _, c := range loadSplitMatrix(t) {
		got := splitConcatenatedFiles(strings.Join(c.Block, "\n"))
		offsets := make([]int, 0, len(got))
		for _, s := range got {
			offsets = append(offsets, s.offset)
		}
		if len(offsets) != len(c.Offsets) {
			t.Errorf("%s: expected %d segment(s) at %v, got %d at %v\n  why this "+
				"case exists: %s", c.Name, len(c.Offsets), c.Offsets, len(offsets),
				offsets, c.Why)
			continue
		}
		for i := range offsets {
			if offsets[i] != c.Offsets[i] {
				t.Errorf("%s: expected offsets %v, got %v\n  why this case exists: %s",
					c.Name, c.Offsets, offsets, c.Why)
				break
			}
		}
	}
}

// TestDocsYamlSplitPartitions asserts the split is a PARTITION — every line of
// the block lands in exactly one segment. A splitter that silently dropped a
// line would still satisfy the offsets above while quietly removing config
// from the gate's view.
func TestDocsYamlSplitPartitions(t *testing.T) {
	t.Parallel()
	for _, c := range loadSplitMatrix(t) {
		var rebuilt []string
		for _, s := range splitConcatenatedFiles(strings.Join(c.Block, "\n")) {
			rebuilt = append(rebuilt, strings.Split(s.text, "\n")...)
		}
		if strings.Join(rebuilt, "\n") != strings.Join(c.Block, "\n") {
			t.Errorf("%s: segments do not re-form the block\n  want: %q\n  got:  %q",
				c.Name, c.Block, rebuilt)
		}
	}
}
